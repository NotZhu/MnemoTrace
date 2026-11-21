"""验证停机信号如何结束后台任务领取。"""

import asyncio
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack
from types import FrameType
from unittest.mock import AsyncMock, Mock

import pytest

from mnemotrace_next import worker
from mnemotrace_next.adapters.postgres.storage import _return_committed_connection
from mnemotrace_next.application.operations import OperationRunner
from mnemotrace_next.server.settings import Settings


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
            settings: Settings,
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
            supervisor = asyncio.create_task(worker._run(Settings("dbname=unused"), 1))
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
