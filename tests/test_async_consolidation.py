"""架构：异步 System-2 整合（CLAUDE.md Bet F），通过 MNEMOTRACE_ASYNC_CONSOLIDATION=1 开启。
默认的同步路径保持不变（这里也验证了这一点）。完全离线（hashing 嵌入器、规则提取器、无 LLM）。"""
from __future__ import annotations

import shutil
import tempfile

from mnemotrace.service import MemoryService

_SENT = "My name is Wei and I live in Shenzhen."  # 规则提取器能从这句抽出 >=1 条事实


def test_consolidation_is_synchronous_by_default(monkeypatch):
    monkeypatch.delenv("MNEMOTRACE_ASYNC_CONSOLIDATION", raising=False)
    d = tempfile.mkdtemp(prefix="mnemotrace_sync_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        assert svc._worker is None  # 没有后台线程
        r = svc.remember("u", _SENT)
        assert "queued" not in r and r.get("scope") == "long"  # 内联完成整合
        assert r.get("extracted", 0) >= 1  # 事实立即可用（写完即可查）
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_async_remember_returns_fast_then_flush_consolidates(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_ASYNC_CONSOLIDATION", "1")
    d = tempfile.mkdtemp(prefix="mnemotrace_async_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        assert svc._worker is not None  # 后台 worker 已启动

        r = svc.remember("u", _SENT)
        assert r.get("queued") is True  # System-1 返回时没有内联整合
        # 在整合运行之前，episode 已经持久化（System-1 已保存）
        assert svc.memories("u")["counts"]["episodes"] >= 1

        svc.flush()  # 等 System-2 清空队列
        # flush 之后整合已运行：规则提取器抽出了至少一条事实
        assert svc.memories("u")["counts"]["facts_live"] >= 1

        svc.close()
        assert svc._worker is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_async_coalesces_multiple_writes_then_flush(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_ASYNC_CONSOLIDATION", "1")
    d = tempfile.mkdtemp(prefix="mnemotrace_async2_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.remember("u", "My name is Wei.")
        svc.remember("u", "I live in Shenzhen.")
        svc.remember("u", "I work at Moonshot AI.")
        svc.flush()  # 所有排队任务完成（已合并）；不丢失任何写入
        assert svc.memories("u")["counts"]["episodes"] == 3
        assert svc.memories("u")["counts"]["facts_live"] >= 1
        svc.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
