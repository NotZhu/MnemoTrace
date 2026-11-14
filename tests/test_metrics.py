"""在线服务指标（AGENTS.md Bet D 的线上部分）。关键点：承载 SLO 的路径有计时（p50/p95），
token 节省比来自真实的 recall 调用，异步积压可见，且载荷只含聚合值——没有命名空间名、
没有用户内容。全部离线运行。"""
from __future__ import annotations



from mnemotrace.metrics import Metrics

_SECRET_NS = "alice-secret-ns"
_SECRET_TEXT = "I live in Shenzhen"


def test_worker_rate_limits_repeated_database_warnings(monkeypatch, caplog):
    from types import SimpleNamespace

    from mnemotrace import worker
    from mnemotrace.store.postgres.errors import DatabaseUnavailable

    attempts = []
    ticks = iter((0, 1, 60, 62))

    def unavailable(limit):
        attempts.append(limit)
        if len(attempts) == 4:
            return ()
        raise DatabaseUnavailable('Synthetic outage')

    monkeypatch.setattr(worker, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(worker, 'sleep', lambda _: None)
    worker.run_worker(SimpleNamespace(discover_processing=unavailable), None, None, object(), None, None,
                      should_stop=lambda: len(attempts) == 5)
    assert len(attempts) == 5
    assert [record.message for record in caplog.records if record.name == worker.__name__] == [
        'Postgres processing is temporarily unavailable',
        'Postgres processing is temporarily unavailable',
    ]


def test_successful_worker_tasks_do_not_reset_database_warning_interval(monkeypatch, caplog):
    from concurrent.futures import wait
    from types import SimpleNamespace
    from unittest.mock import Mock

    from mnemotrace import worker
    from mnemotrace.store.contracts import SpaceContext
    from mnemotrace.store.postgres.errors import DatabaseUnavailable

    def run_stage(operation_id):
        if operation_id == 'unavailable':
            raise DatabaseUnavailable('Synthetic outage')
        return True

    def record_warning_count(_):
        warning_counts.append(sum(record.name == worker.__name__ for record in caplog.records))

    context = SpaceContext('user', 'space')
    candidates = ((context, 'healthy'), (context, 'unavailable'))
    discovery = SimpleNamespace(discover_processing=Mock(side_effect=[candidates] * 3))
    ticks = iter((0, 1, 60))
    warning_counts = []
    monkeypatch.setattr(worker, 'PostgresPipeline', Mock(return_value=SimpleNamespace(run_next_stage=run_stage)))
    monkeypatch.setattr(worker, 'wait', lambda futures, **_: wait(futures, timeout=5))
    monkeypatch.setattr(worker, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(worker, 'sleep', record_warning_count)
    worker.run_worker(discovery, None, None, object(), None, None,
                      should_stop=lambda: len(warning_counts) == 3)

    assert warning_counts == [1, 1, 2]


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
