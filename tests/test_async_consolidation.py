"""架构：异步 System-2 整合（AGENTS.md Bet F），通过 MNEMOTRACE_BUFFERED_EXTRACTION=1 开启。
默认的同步路径保持不变（这里也验证了这一点）。使用 hashing 嵌入器和固定合成模型响应离线验证。"""
from __future__ import annotations

import shutil
import tempfile

from tests.model_fixtures import MemoryService

_SENT = "My name is Wei and I live in Shenzhen."  # 固定模型响应包含至少一条事实


def _drain(svc):
    """Finish any window the background worker has not picked up yet.

    flush() may return before the worker thread wakes on a slow host; drain the
    same ledger deterministically so the assertion is not a thread race.
    """
    from mnemotrace._pipeline import run_one
    while run_one(svc, 'u'):
        pass


def test_raw_note_is_synchronous_with_default_worker(monkeypatch):
    monkeypatch.delenv("MNEMOTRACE_BUFFERED_EXTRACTION", raising=False)
    d = tempfile.mkdtemp(prefix="mnemotrace_sync_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        assert svc._pipeline_worker is not None  # 对话 worker 默认开启，原文入口仍可即时处理
        r = svc.remember("u", _SENT)
        assert not r.get("queued") and r.get("scope") == "long"  # 内联完成整合
        assert r.get("extracted", 0) >= 1  # 事实立即可用（写完即可查）
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_async_remember_returns_fast_then_flush_consolidates(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_BUFFERED_EXTRACTION", "1")
    d = tempfile.mkdtemp(prefix="mnemotrace_async_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        assert svc._pipeline_worker is not None  # 后台 worker 已启动

        r = svc.remember("u", _SENT, processing="buffered")
        assert r.get("queued") is True  # System-1 返回时没有内联整合
        # 在整合运行之前，episode 已经持久化（System-1 已保存）
        assert svc.memories("u")["counts"]["episodes"] >= 1

        svc.flush()  # 等 System-2 清空队列
        _drain(svc)
        # flush 之后整合已运行，固定模型响应中的事实已落库
        assert svc.memories("u")["counts"]["facts_live"] >= 1

        svc.close()
        assert not svc._pipeline_worker.thread.is_alive()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_async_coalesces_multiple_writes_then_flush(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_BUFFERED_EXTRACTION", "1")
    d = tempfile.mkdtemp(prefix="mnemotrace_async2_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.remember("u", "My name is Wei.", processing="buffered")
        svc.remember("u", "I live in Shenzhen.", processing="buffered")
        svc.remember("u", "I work at Moonshot AI.", processing="buffered")
        svc.flush()  # 所有排队任务完成（已合并）；不丢失任何写入
        _drain(svc)
        assert svc.memories("u")["counts"]["episodes"] == 3
        assert svc.memories("u")["counts"]["facts_live"] >= 1
        svc.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
