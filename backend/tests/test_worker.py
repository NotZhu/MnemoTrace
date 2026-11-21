"""验证写入 worker 的启动和停机边界。"""

import asyncio
import signal
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from types import FrameType
from unittest.mock import AsyncMock, Mock

import pytest

from mnemotrace_next import worker
from mnemotrace_next.adapters.postgres.storage import _return_committed_connection
from mnemotrace_next.application.operations import OperationRunner
from mnemotrace_next.runtime_settings import RuntimeSettings
from mnemotrace_next.server.__main__ import main as server_main


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MNEMOTRACE_NEXT_MAX_BODY_BYTES", "0"),
        ("MNEMOTRACE_NEXT_QUEUE_LIMIT", "invalid"),
    ],
)
def test_http_limits_only_apply_to_http_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    """API 拒绝非法接收限制，worker 命令仍能进入运行流程。"""
    monkeypatch.setenv("MNEMOTRACE_NEXT_POSTGRES_DSN", "dbname=unused")
    monkeypatch.delenv("MNEMOTRACE_NEXT_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("MNEMOTRACE_NEXT_QUEUE_LIMIT", raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["mnemotrace_next.server"])
    with pytest.raises(SystemExit) as exit_info:
        server_main()
    assert exit_info.value.code == 2
    assert f"{name} 须为正整数" in capsys.readouterr().err

    run_worker = AsyncMock()
    monkeypatch.setattr(worker, "_run", run_worker)
    monkeypatch.setattr(sys, "argv", ["mnemotrace_next.worker"])
    assert worker.main() == 0
    run_worker.assert_awaited_once()
    settings = run_worker.await_args_list[0].args[0]
    assert isinstance(settings, RuntimeSettings)
    assert settings.postgres_dsn == "dbname=unused"


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
            concurrency: int,
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
                worker._run(RuntimeSettings("dbname=unused"), 1)
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
