"""在线服务指标（AGENTS.md Bet D 的线上部分）。关键点：承载 SLO 的路径有计时（p50/p95），
token 节省比来自真实的 recall 调用，异步积压可见，且载荷只含聚合值——没有命名空间名、
没有用户内容。全部离线运行。"""
from __future__ import annotations



from mnemotrace.metrics import Metrics

_SECRET_NS = "alice-secret-ns"
_SECRET_TEXT = "I live in Shenzhen"


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
