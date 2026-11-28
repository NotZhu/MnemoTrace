"""验证写入 worker 的启动和停机边界。"""

import asyncio
import logging
import signal
import sys
from collections import Counter
from collections.abc import Callable
from contextlib import AsyncExitStack, nullcontext
from types import FrameType
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from mnemotrace import worker
from mnemotrace.adapters.inference import InferenceClients
from mnemotrace.adapters.postgres.storage import _return_committed_connection
from mnemotrace.application.operations import (
    ClaimedOperation,
    OperationQueue,
    OperationRunner,
)
from mnemotrace.application.profile_refresh import ProfileRefreshRunner
from mnemotrace.application.remember import Remember
from mnemotrace.application.session_summary_refresh import SessionSummaryRunner
from mnemotrace.application.task_policy import ClaimOutcome
from mnemotrace.domain.ids import SessionId, SpaceId
from mnemotrace.runtime_settings import RuntimeSettings
from mnemotrace.server.__main__ import main as server_main


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), (2, 1, 1)),
        (("--profile-concurrency", "3"), (2, 3, 1)),
        (("--concurrency", "1", "--summary-concurrency", "16"), (1, 1, 16)),
    ],
)
def test_worker_passes_concurrency_to_runtime(
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    expected: tuple[int, int, int],
) -> None:
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", "dbname=unused")
    monkeypatch.delenv("MNEMOTRACE_EXTRACTION_TIMEZONE", raising=False)
    monkeypatch.setattr(sys, "argv", ["mnemotrace.worker", *flags])
    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)

    assert worker.main() == 0
    run_worker.assert_awaited_once()
    concurrency = run_worker.await_args_list[0].args[1]
    assert (concurrency.write, concurrency.profile, concurrency.summary) == expected


@pytest.mark.parametrize(
    "option", ["--concurrency", "--profile-concurrency", "--summary-concurrency"]
)
@pytest.mark.parametrize("value", ["0", "17", "invalid"])
def test_worker_rejects_invalid_concurrency_before_startup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: str,
    value: str,
) -> None:
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", "dbname=unused")
    monkeypatch.setattr(sys, "argv", ["mnemotrace.worker", option, value])
    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)

    with pytest.raises(SystemExit) as exit_info:
        worker.main()
    assert exit_info.value.code == 2
    assert option in capsys.readouterr().err.splitlines()[-1]
    run_worker.assert_not_awaited()


def test_worker_starts_configured_consumers_with_matching_pool_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checksums = {"001": "checksum"}
    cursor = Mock(fetchall=AsyncMock(return_value=list(checksums.items())))
    connection = Mock(execute=AsyncMock(return_value=cursor))
    pool = MagicMock()
    pool.connection.return_value = nullcontext(connection)
    make_pool = Mock(return_value=pool)
    clients = InferenceClients(llm=Mock(), embedder=Mock())
    monkeypatch.setattr(worker, "runtime_pool", make_pool)
    monkeypatch.setattr(
        worker, "open_inference", Mock(return_value=nullcontext(clients))
    )
    monkeypatch.setattr(worker, "migration_checksums", Mock(return_value=checksums))
    caplog.set_level(logging.INFO, logger=worker.__name__)

    async def exercise() -> None:
        stopping = asyncio.Event()
        started: Counter[type[worker._Runner]] = Counter()

        async def count_consumer(runner: worker._Runner, stop: asyncio.Event) -> None:
            started[type(runner)] += 1
            if started.total() == 6:
                stop.set()

        monkeypatch.setattr(worker, "_consume", count_consumer)
        settings = RuntimeSettings("dbname=unused")
        concurrency = worker._WorkerConcurrency(write=2, profile=3, summary=1)
        await asyncio.wait_for(worker._serve(settings, concurrency, stopping, None), 2)

        make_pool.assert_called_once_with(settings, max_size=6)
        assert started == {
            OperationRunner: 2,
            ProfileRefreshRunner: 3,
            SessionSummaryRunner: 1,
        }

    asyncio.run(exercise())
    assert (
        "write_concurrency=2 profile_concurrency=3 summary_concurrency=1 max_db_connections=6"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("configured", "expected"), [(None, "UTC"), ("Asia/Shanghai", "Asia/Shanghai")]
)
def test_worker_loads_extraction_timezone(
    monkeypatch: pytest.MonkeyPatch, configured: str | None, expected: str
) -> None:
    """命令行按部署配置选择时区，省略时保持 UTC。"""
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", "dbname=unused")
    monkeypatch.setattr(sys, "argv", ["mnemotrace.worker"])
    if configured is None:
        monkeypatch.delenv("MNEMOTRACE_EXTRACTION_TIMEZONE", raising=False)
    else:
        monkeypatch.setenv("MNEMOTRACE_EXTRACTION_TIMEZONE", configured)
    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)

    assert worker.main() == 0
    run_worker.assert_awaited_once()
    settings = run_worker.await_args_list[0].args[0]
    assert isinstance(settings, RuntimeSettings)
    assert str(settings.extraction_timezone) == expected


@pytest.mark.parametrize("configured", ["Invalid/TimeZone", ""])
@pytest.mark.parametrize(
    "entrypoint", [worker.main, server_main], ids=["worker", "http"]
)
def test_process_rejects_invalid_extraction_timezone_before_starting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: str,
    entrypoint: Callable[[], int],
) -> None:
    """HTTP 与 worker 在启动前拒绝无效的抽取时区。"""
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", "dbname=unused")
    monkeypatch.setenv("MNEMOTRACE_EXTRACTION_TIMEZONE", configured)
    monkeypatch.setattr(sys, "argv", ["mnemotrace"])
    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)

    with pytest.raises(SystemExit) as exit_info:
        entrypoint()
    assert exit_info.value.code == 2
    assert "MNEMOTRACE_EXTRACTION_TIMEZONE" in capsys.readouterr().err
    run_worker.assert_not_awaited()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MNEMOTRACE_MAX_BODY_BYTES", "0"),
        ("MNEMOTRACE_QUEUE_LIMIT", "invalid"),
    ],
)
def test_http_limits_only_apply_to_http_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    """API 拒绝非法接收限制，worker 命令仍能进入运行流程。"""
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", "dbname=unused")
    monkeypatch.delenv("MNEMOTRACE_EXTRACTION_TIMEZONE", raising=False)
    monkeypatch.delenv("MNEMOTRACE_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("MNEMOTRACE_QUEUE_LIMIT", raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["mnemotrace.server"])
    with pytest.raises(SystemExit) as exit_info:
        server_main()
    assert exit_info.value.code == 2
    assert f"{name} 须为正整数" in capsys.readouterr().err

    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)
    monkeypatch.setattr(sys, "argv", ["mnemotrace.worker"])
    assert worker.main() == 0
    run_worker.assert_awaited_once()
    settings = run_worker.await_args_list[0].args[0]
    assert isinstance(settings, RuntimeSettings)
    assert settings.postgres_dsn == "dbname=unused"


def test_worker_polls_ready_work_before_sleeping_after_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        job = ClaimedOperation(
            operation_id="ready",
            space_id=SpaceId("space"),
            owner_id="owner",
            session_id=SessionId("session"),
            messages=(),
            received_at=1.0,
            attempt=1,
            lease_token="lease",
        )
        queue = Mock(spec=OperationQueue)
        queue.claim.side_effect = [ClaimOutcome.SETTLED, job, ClaimOutcome.EMPTY]
        remember = Mock(spec=Remember)
        stopping = asyncio.Event()
        runner = OperationRunner(queue, Mock(return_value=remember), lambda _: False)

        async def idle(delay: float) -> None:
            assert queue.claim.await_count == 3
            remember.remember.assert_awaited_once()
            assert delay == 1
            stopping.set()

        monkeypatch.setattr(asyncio, "sleep", idle)
        await worker._consume(runner, stopping)

    asyncio.run(exercise())


@pytest.mark.parametrize("stop_signal", (signal.SIGINT, signal.SIGTERM))
def test_worker_stops_after_cancel_is_consumed_returning_a_connection(
    monkeypatch: pytest.MonkeyPatch, stop_signal: signal.Signals
) -> None:
    """模拟一次停机信号，已提交连接归还后禁止再次领取。"""

    async def exercise() -> None:
        type Handler = Callable[[int, FrameType | None], None]
        handlers: dict[signal.Signals, Handler] = {}
        returning = asyncio.Event()
        release = asyncio.Event()

        async def return_connection() -> None:
            returning.set()
            await release.wait()

        async def run_once() -> bool:
            assert runner.run_once.await_count == 1, "停机后又尝试领取"
            async with AsyncExitStack() as connection:
                connection.push_async_callback(return_connection)
                await _return_committed_connection(connection.pop_all())
            return True

        runner = Mock(spec=OperationRunner)
        runner.run_once = AsyncMock(side_effect=run_once)

        def capture(signum: signal.Signals, handler: Handler) -> None:
            handlers[signum] = handler

        async def serve(
            settings: RuntimeSettings,
            concurrency: worker._WorkerConcurrency,
            stopping: asyncio.Event,
            readiness_port: int | None,
        ) -> None:
            async with asyncio.TaskGroup() as group:
                group.create_task(worker._consume(runner, stopping))
                await asyncio.Event().wait()

        with monkeypatch.context() as patch:
            patch.setattr(worker, "_serve", serve)
            patch.setattr(signal, "signal", capture)
            supervisor = asyncio.create_task(
                worker._run(
                    RuntimeSettings("dbname=unused"), worker._WorkerConcurrency(write=1)
                )
            )
            try:
                await asyncio.wait_for(returning.wait(), 2)
                handlers[stop_signal](stop_signal, None)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                release.set()
            await asyncio.wait_for(supervisor, 2)

        runner.run_once.assert_awaited_once()

    asyncio.run(exercise())
