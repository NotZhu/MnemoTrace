"""重新嵌入迁移（即 store 嵌入空间守卫所指向的那条逃生通道）。守卫本身（open 时的
DimensionMismatchError/EmbedderMismatchError）在 test_persist_safety 中覆盖；这里验证的是迁移：
open allow_mismatch -> reembed() -> 所有向量都进入新空间、检索可用，且保存后的 manifest 让下次
open 干净通过。全部离线运行。"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from mnemotrace.embed import HashingEmbedder
from tests.model_fixtures import Memory, seed_summaries
from tests.model_fixtures import MemoryService
from mnemotrace.store import DimensionMismatchError

_SENT = "My name is Wei and I live in Shenzhen."


def _store(path: str, dim: int = 64) -> None:
    m = Memory.open(path, embedder=HashingEmbedder(dim))
    m.add(_SENT, user_id="u", consolidate=True)
    seed_summaries(m, list(m.episodes_doc.values()))  # 顺便填充 summary 索引
    m.save()


def test_reembed_migrates_every_vector_then_reopens_clean():
    d = tempfile.mkdtemp(prefix="mnemotrace_reembed_")
    try:
        p = os.path.join(d, "u")
        _store(p, dim=64)
        with pytest.raises(DimensionMismatchError):
            Memory.open(p, embedder=HashingEmbedder(128))  # 守卫依然拒绝维度不匹配的普通 open

        m = Memory.open(p, allow_mismatch=True, embedder=HashingEmbedder(128))
        counts = m.reembed()
        assert counts["facts"] >= 1 and counts["episodes"] >= 1 and counts["summaries"] >= 1
        # 所有索引现在都持有 128 维向量，检索依然可用
        assert all(len(f.embedding) == 128 for f in m.fact_store.values())
        assert all(len(ep.embedding) == 128 for ep in m.episodes_vec.values())
        assert all(len(ep.summary_embedding) == 128 for ep in m.summary_vec.values())
        assert m.search("where do I live", user_id="u").facts
        m.save()  # manifest 现在记录了新的嵌入器身份/维度

        m2 = Memory.open(p, embedder=HashingEmbedder(128))  # 干净 open，无需 allow_mismatch
        assert m2.search("where do I live", user_id="u").facts
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_service_refuses_then_migrates_with_flag(monkeypatch):
    d = tempfile.mkdtemp(prefix="mnemotrace_reembed_svc_")
    try:
        # 一个以 64 维 hashing 存储的命名空间；而 service 按配置默认维度构建
        svc0 = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        default_dim = svc0.embedder.dim
        assert default_dim != 64
        store_dir = svc0._path("u")
        _store(store_dir, dim=64)

        monkeypatch.delenv("MNEMOTRACE_REEMBED_ON_MISMATCH", raising=False)
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        with pytest.raises(DimensionMismatchError):
            svc.memories("u")  # 拒绝：不做静默损坏

        monkeypatch.setenv("MNEMOTRACE_REEMBED_ON_MISMATCH", "1")
        svc2 = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        dump = svc2.memories("u")  # 首次访问时自动迁移
        assert dump["counts"]["facts_live"] >= 1
        mem = svc2.get("u")
        assert all(len(f.embedding) == default_dim for f in mem.fact_store.values())

        # 迁移已持久化：第三个 service（关闭开关）能干净打开
        monkeypatch.delenv("MNEMOTRACE_REEMBED_ON_MISMATCH", raising=False)
        svc3 = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        assert svc3.memories("u")["counts"]["facts_live"] >= 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
