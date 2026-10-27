"""导出 -> 导入的可移植性往返测试（忆痕 快照应当被"还原"，而不是被重新写入）：
事实保留双时间轴时间戳 + 取代链，episode 以已整合状态重新进入，整体是幂等的，
且 /v1/import 会把解析失败转成 400 而不是 500。与所有其他测试一样离线运行
（hashing 嵌入器 + 规则抽取器）。"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import asdict

import pytest

from mnemotrace.memory import Memory


def _mk_memory() -> Memory:
    return Memory()


class TestSnapshotRoundtrip:
    def _service(self, tmpdir: str):
        from mnemotrace.service import MemoryService
        return MemoryService(data_dir=tmpdir, embedder_name="hashing", llm_name="")

    def test_full_roundtrip_restores_facts_episodes_and_chains(self):
        d = tempfile.mkdtemp(prefix="mnemotrace_snap_")
        try:
            svc = self._service(d)
            # 种子数据：一条取代链（Beijing -> Shanghai）+ 一条手工权威事实
            svc.remember("alice", "I live in Beijing.")
            svc.remember("alice", "I moved, I now live in Shanghai.")
            svc.add_fact("alice", "alice", "favorite_editor", "vim")
            snap = svc.export("alice", include_sensitive=True)
            assert "mnemotrace_export_version" not in snap and snap["episodes"], "need a full export"
            assert "engram_export_version" not in snap

            out = svc.import_(user="bob", data=snap, format="auto")
            assert out["ok"] is True and out["format"] == "mnemotrace"
            assert out["facts_restored"] >= 3  # 存活 x2（Shanghai、vim）+ 已失效的 Beijing
            assert out["episodes_restored"] == len(snap["episodes"])

            mem = svc.get("bob")
            facts = [f for f in mem._all_facts() if f.user_id == "bob"]
            by_obj = {f.object: f for f in facts}
            # 双时间轴存续：被取代的那条仍处于失效状态，存活的那条仍然有效
            assert "Beijing" in by_obj and by_obj["Beijing"].invalid_at is not None
            assert "Shanghai" in by_obj and by_obj["Shanghai"].is_live()
            # 取代链要重新链接到 Beijing 事实的新 id（不能留下悬空的外部 id）
            assert by_obj["Shanghai"].supersedes == by_obj["Beijing"].id
            # 手工断言的事实保留其权威的 provenance 类别
            assert by_obj["vim"].source == "user"
            # 还原的 episode 绝不能再次进入 System-2（它们的事实已随快照一起到来）
            eps = [ep for ep in mem.episodes_doc.values() if ep.user_id == "bob"]
            assert eps and all(ep.consolidated for ep in eps)
            # 且记忆确实能从还原后的命名空间中回答问题
            ctx = svc.recall("bob", "where do I live?")["context"]
            assert "Shanghai" in ctx
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_reimport_is_idempotent(self):
        d = tempfile.mkdtemp(prefix="mnemotrace_snap_")
        try:
            svc = self._service(d)
            svc.remember("alice", "My favorite drink is oolong tea.")
            snap = svc.export("alice", include_sensitive=True)
            first = svc.import_(user="bob", data=snap)
            again = svc.import_(user="bob", data=snap)
            assert first["facts_restored"] >= 1
            assert again["facts_restored"] == 0 and again["episodes_restored"] == 0
            assert again["facts_skipped"] == first["facts_restored"]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_safe_export_restores_facts_without_episodes(self):
        # 默认的分享安全导出不含 episode；只还原事实也必须可行。
        d = tempfile.mkdtemp(prefix="mnemotrace_snap_")
        try:
            svc = self._service(d)
            svc.remember("alice", "I work at Acme Corp.")
            snap = svc.export("alice")  # include_sensitive=False
            assert snap["episodes"] == []
            out = svc.import_(user="carol", data=snap)
            assert out["ok"] is True and out["facts_restored"] >= 1
            assert "Acme" in svc.recall("carol", "where do I work?")["context"]
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestSnifferAndMemoryLevel:
    def test_sniff_recognizes_snapshot_and_parse_gives_actionable_error(self):
        from mnemotrace.connectors import parse, sniff
        snap = {"facts": [], "episodes": []}
        assert sniff(snap) == "mnemotrace"
        with pytest.raises(ValueError, match="import_snapshot"):
            parse(snap)

    def test_memory_import_snapshot_direct(self):
        src = _mk_memory()
        src.add("I love hiking in the Alps.", user_id="u1")
        src.consolidate()
        facts = [asdict(f) for f in src._all_facts()]
        dst = _mk_memory()
        stats = dst.import_snapshot({"facts": facts, "episodes": []},
                                    user_id="u2")
        assert stats["facts_restored"] == len(facts) > 0
        assert stats["malformed"] == 0

    def test_malformed_rows_reject_the_entire_snapshot(self):
        dst = _mk_memory()
        with pytest.raises(ValueError, match="snapshot.facts"):
            dst.import_snapshot({
                "facts": [{"subject": "a"}, "not-a-dict"],
                "episodes": [{"no_content": True}, "junk"],
            })
        assert dst.fact_store.values() == dst.episodes_doc.values() == []


pytest_fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    d = tempfile.mkdtemp(prefix="mnemotrace_snap_srv_")
    os.environ.update(MNEMOTRACE_DATA_DIR=d, MNEMOTRACE_EMBEDDER="hashing", MNEMOTRACE_OPEN="1")
    # 显式置空而不是 pop：MemoryService 初始化会 load_dotenv()，pop 掉的变量会被本地 .env 重新注入
    os.environ["MNEMOTRACE_LLM"] = ""
    os.environ["MNEMOTRACE_ANSWERER"] = ""
    os.environ["MNEMOTRACE_API_KEYS"] = ""
    os.environ["MNEMOTRACE_ALLOW_ANONYMOUS"] = ""
    from mnemotrace.server import app as appmod
    appmod._svc = None
    with TestClient(appmod.app) as c:
        yield c
    shutil.rmtree(d, ignore_errors=True)


def _hdr(ns: str) -> dict:
    return {"Authorization": f"Bearer {ns}"}


def test_http_unparseable_import_is_400_not_500(client):
    # 任何不匹配已知导出结构的 JSON 对象，过去会让解析器崩溃 -> 500。
    r = client.post("/v1/import", json={"data": {"foo": "bar"}}, headers=_hdr("qa"))
    assert r.status_code == 400
    assert "could not parse" in r.json()["detail"]


def test_http_export_import_roundtrip(client):
    r = client.post("/v1/remember", json={"content": "My cat is named Miso."}, headers=_hdr("src"))
    assert r.status_code == 200
    snap = client.get("/v1/export", params={"include_sensitive": "true"}, headers=_hdr("src")).json()
    r = client.post("/v1/import", json={"data": snap}, headers=_hdr("dst"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["format"] == "mnemotrace" and body["facts_restored"] >= 1
    ctx = client.post("/v1/recall", json={"query": "what is my cat's name?"},
                      headers=_hdr("dst")).json()["context"]
    assert "Miso" in ctx


def test_http_invalid_snapshot_is_400_and_does_not_partially_import(client):
    client.post("/v1/remember", json={"content": "My cat is named Miso."}, headers=_hdr("src"))
    snap = client.get("/v1/export", params={"include_sensitive": "true"}, headers=_hdr("src")).json()
    snap["facts"][-1]["provenance"] = 42
    response = client.post("/v1/import", json={"data": snap}, headers=_hdr("dst"))
    assert response.status_code == 400
    assert "provenance" in response.json()["detail"]
    restored = client.get("/v1/export", params={"include_sensitive": "true"}, headers=_hdr("dst")).json()
    assert restored["facts"] == restored["episodes"] == []
