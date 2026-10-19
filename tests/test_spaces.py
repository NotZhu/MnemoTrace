"""多空间（多 agent / 团队共享）记忆测试（CLAUDE.md §6）。覆盖 MNEMOTRACE_SPACES 的 ACL 解析、service
层的跨空间读融合，以及 HTTP 层的写入/读取授权。单空间鉴权（MNEMOTRACE_API_KEYS / open 模式）刻意
不做改动，仍由 test_server 覆盖。全部离线（hashing 嵌入器、无 LLM）；环境变量用 monkeypatch，
会自动还原。"""
from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from mnemotrace.service import MemoryService
from mnemotrace.spaces import Principal, principal_for_token


# --- ACL 解析 ---------------------------------------------------------------


def test_spaces_config_defaults_write_to_home(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({"sk": {"home": "h", "read": ["h", "t"]}}))
    p = principal_for_token("sk")
    assert p is not None and p.home == "h"
    assert p.can_read("h") and p.can_read("t")
    assert p.can_write("h") and not p.can_write("t")  # 未写 write -> 默认为 [home]
    assert principal_for_token("other") is None  # 未知 token -> 不是 spaces 主体


def test_malformed_spaces_is_ignored(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SPACES", "{not valid json")
    assert principal_for_token("whatever") is None  # 配置损坏时绝不能授予访问权


def test_single_principal_shape():
    p = Principal.single("alice")
    assert p.home == "alice" and p.can_read("alice") and p.can_write("alice")
    assert not p.can_read("bob")


# --- service 层的跨空间融合 ----------------------------------------------------


def test_recall_multi_fuses_spaces():
    d = tempfile.mkdtemp(prefix="mnemotrace_sp_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.remember("agentA", "The deploy key rotates every 90 days.")
        svc.remember("team", "Standup is at 10am daily.")
        out = svc.recall_multi(["agentA", "team"], "standup")
        assert out["spaces"] == ["agentA", "team"]
        # 两个空间都有贡献，且各自带有来源标记
        assert "Space: agentA" in out["context"] and "Space: team" in out["context"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- HTTP 写入/读取授权 --------------------------------------------------------


@pytest.fixture()
def spaces_client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    d = tempfile.mkdtemp(prefix="mnemotrace_spsrv_")
    cfg = {
        "sk-agent": {"home": "agentA", "read": ["agentA", "team"], "write": ["agentA", "team"]},
        "sk-alice": {"home": "alice"},  # 读写默认都只有 [home]
    }
    monkeypatch.setenv("MNEMOTRACE_DATA_DIR", d)
    monkeypatch.setenv("MNEMOTRACE_EMBEDDER", "hashing")
    monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps(cfg))
    # 置空而不是 delenv：load_dotenv 只补缺失的变量，delenv 会让本地 .env 的值重新生效
    for var in ("MNEMOTRACE_API_KEYS", "MNEMOTRACE_OPEN", "MNEMOTRACE_ALLOW_ANONYMOUS", "MNEMOTRACE_LLM", "MNEMOTRACE_ANSWERER"):
        monkeypatch.setenv(var, "")
    from mnemotrace.server import app as appmod
    appmod._svc = None
    with TestClient(appmod.app) as c:
        yield c
    appmod._svc = None
    shutil.rmtree(d, ignore_errors=True)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_write_home_and_shared_then_read_fuses(spaces_client):
    c = spaces_client
    assert c.post("/v1/remember", json={"content": "Agent A note: the API base is api.acme.dev."},
                  headers=_h("sk-agent")).json()["ok"] is True
    assert c.post("/v1/remember", json={"content": "Team standup is daily at 10am.", "space": "team"},
                  headers=_h("sk-agent")).json()["ok"] is True
    r = c.post("/v1/recall", json={"query": "standup", "spaces": ["agentA", "team"]},
               headers=_h("sk-agent")).json()
    assert r["spaces"] == ["agentA", "team"]
    assert "Space: agentA" in r["context"] and "Space: team" in r["context"]


def test_write_to_unwritable_space_is_forbidden(spaces_client):
    r = spaces_client.post("/v1/remember", json={"content": "x", "space": "team"}, headers=_h("sk-alice"))
    assert r.status_code == 403


def test_read_unreadable_space_is_forbidden(spaces_client):
    r = spaces_client.post("/v1/recall", json={"query": "x", "spaces": ["team"]}, headers=_h("sk-alice"))
    assert r.status_code == 403


def test_unknown_token_is_unauthorized(spaces_client):
    # 不是 spaces key、没有 MNEMOTRACE_API_KEYS、也不是 open 模式 -> 回落到单空间鉴权并拒绝
    assert spaces_client.post("/v1/recall", json={"query": "x"}, headers=_h("sk-nope")).status_code == 401


def test_home_default_single_space_answer_shape(spaces_client):
    c = spaces_client
    c.post("/v1/remember", json={"content": "Alice lives in Berlin."}, headers=_h("sk-alice"))
    r = c.post("/v1/recall", json={"query": "Where does the user live?"}, headers=_h("sk-alice")).json()
    # 单空间路径保持既有的 recall(answer=True) 返回结构（context + token 估算）
    assert "context" in r and "tokens_est" in r
