"""在线服务指标（AGENTS.md Bet D 的线上部分）。关键点：承载 SLO 的路径有计时（p50/p95），
token 节省比来自真实的 recall 调用，异步积压可见，且载荷只含聚合值——没有命名空间名、
没有用户内容。全部离线运行。"""
from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from mnemotrace.metrics import Metrics
from tests.model_fixtures import MemoryService

_SECRET_NS = "alice-secret-ns"
_SECRET_TEXT = "I live in Shenzhen"


@pytest.fixture()
def svc():
    d = tempfile.mkdtemp(prefix="mnemotrace_metrics_")
    try:
        yield MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- Metrics 原语 -------------------------------------------------------------


def test_percentiles_and_counts():
    m = Metrics()
    for ms in (10, 20, 30, 40, 100):
        m.observe("recall", ms / 1000)
    snap = m.snapshot()
    op = snap["ops"]["recall"]
    assert op["n"] == 5 and op["window"] == 5
    assert op["p50_ms"] == 30.0
    assert op["p95_ms"] == 100.0  # 5 个样本窗口的 nearest-rank -> 取最大值
    assert op["max_ms"] == 100.0


def test_savings_ratio_needs_both_sides():
    m = Metrics()
    m.tokens(100)  # 只有 context -> 还没有比率
    assert m.snapshot()["tokens"]["savings_ratio"] is None
    m.tokens(100, full=1600)
    snap = m.snapshot()["tokens"]
    assert snap["recalls_with_baseline"] == 1
    assert snap["savings_ratio"] == 8.0  # 1600 / (100 + 100)


# --- service 接线 ----------------------------------------------------------------


def test_service_paths_are_timed_and_tokens_recorded(svc):
    svc.remember(_SECRET_NS, f"My name is Wei and {_SECRET_TEXT}.")
    svc.recall(_SECRET_NS, "where do I live")                 # 只记 context tokens
    svc.recall(_SECRET_NS, "where do I live", answer=True)    # + full 基线，固定响应的 answer 为空
    snap = svc.metrics_snapshot()

    assert snap["ops"]["remember"]["n"] == 1
    assert snap["ops"]["recall"]["n"] == 2
    assert snap["ops"]["recall"]["p95_ms"] >= snap["ops"]["recall"]["p50_ms"] >= 0
    t = snap["tokens"]
    assert t["context_total"] > 0 and t["full_total"] > 0 and t["recalls_with_baseline"] == 1
    assert snap["buffered"] == {"enabled": False, "recovery_error": False}
    assert snap["users_hot"] == 1


def test_payload_is_aggregate_only(svc):
    svc.remember(_SECRET_NS, f"My name is Wei and {_SECRET_TEXT}.")
    svc.recall(_SECRET_NS, "where do I live", answer=True)
    raw = json.dumps(svc.metrics_snapshot(), ensure_ascii=False)
    assert _SECRET_NS not in raw      # 没有命名空间名
    assert "Shenzhen" not in raw      # 没有用户内容
    assert "where do I live" not in raw  # 没有查询文本


def test_async_backlog_is_visible(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_BUFFERED_EXTRACTION", "1")
    d = tempfile.mkdtemp(prefix="mnemotrace_metrics_async_")
    try:
        s = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        s.remember("u", "My name is Wei and I live in Shenzhen.", processing="buffered")
        assert s.metrics_snapshot()["buffered"]["enabled"] is True
        s.flush()
        assert s.processing_status("u")["counts"]["completed"] == 1
        s.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_http_metrics_endpoint(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    d = tempfile.mkdtemp(prefix="mnemotrace_metrics_http_")
    monkeypatch.setenv("MNEMOTRACE_DATA_DIR", d)
    monkeypatch.setenv("MNEMOTRACE_EMBEDDER", "hashing")
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    # 置空而不是 delenv：load_dotenv 只补缺失的变量，delenv 会让本地 .env 的值重新生效
    for var in ("MNEMOTRACE_API_KEYS", "MNEMOTRACE_SPACES", "MNEMOTRACE_LLM", "MNEMOTRACE_ANSWERER"):
        monkeypatch.setenv(var, "")
    from mnemotrace.server import app as appmod
    appmod._svc = None
    try:
        with TestClient(appmod.app) as c:
            h = {"Authorization": "Bearer metricsuser"}
            c.post("/v1/remember", json={"content": "I live in Shenzhen."}, headers=h)
            c.post("/v1/recall", json={"query": "where do I live"}, headers=h)
            r = c.get("/metrics")  # 和 /health 一样开放访问
            assert r.status_code == 200
            body = r.json()
            assert body["ops"]["remember"]["n"] >= 1 and body["ops"]["recall"]["n"] >= 1
            assert body["tokens"]["recalls_with_baseline"] >= 1  # HTTP recall 会计算 full 基线
            assert "metricsuser" not in r.text
    finally:
        appmod._svc = None
        shutil.rmtree(d, ignore_errors=True)
