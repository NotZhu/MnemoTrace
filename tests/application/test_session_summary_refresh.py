"""离线验证摘要执行器的尝试时限与重试结算。"""

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from mnemotrace.application.session_summary_refresh import (
    ClaimedSessionSummary,
    SessionSummaryQueue,
    SessionSummaryRunner,
)
from mnemotrace.application.task_policy import ATTEMPT_TIMEOUT_SECONDS
from mnemotrace.domain.enums import Role
from mnemotrace.domain.ids import MessageId, SessionId, SpaceId
from mnemotrace.domain.message import Message
from mnemotrace.domain.space import Space

from ..ports.fakes import FakeLLM, FakeStorage


@pytest.mark.parametrize(
    ("attempt", "retry_delay"),
    [(1, timedelta(seconds=5)), (2, timedelta(seconds=10)), (3, None)],
)
def test_summary_attempt_deadline_cancels_inference_before_publish(
    attempt: int, retry_delay: timedelta | None
) -> None:
    async def exercise() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0.0

        def loop_time() -> float:
            return real_time() + elapsed

        class BlockingModel(FakeLLM):
            async def _structured_response(
                self, *, system: str, prompt: str, schema: Mapping[str, object]
            ) -> Mapping[str, object]:
                entered.set()
                try:
                    await asyncio.Future[None]()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return {"summary": "合成摘要"}

        space = Space(SpaceId("space"), "owner", "测试空间", 10.0)
        session = SessionId("session")
        job = ClaimedSessionSummary(
            space.id, session, space.owner_id, None, 1, attempt, "lease"
        )
        message = Message(
            MessageId("message"), space.id, session, Role.USER, "合成原文", None, 20.0
        )
        storage = FakeStorage()
        await storage.create_space(space)
        async with storage.session(space.id, write=True) as writer:
            await writer.register_session(session)
            await writer.append_messages((message,))

        queue = Mock(spec=SessionSummaryQueue)
        queue.claim.return_value = job
        queue.read_previous.return_value = None
        runner = SessionSummaryRunner(
            queue, lambda _: storage, BlockingModel(), lambda _: False
        )
        with (
            patch.object(storage, "session", wraps=storage.session) as sessions,
            patch.object(loop, "time", loop_time),
        ):
            async with asyncio.TaskGroup() as tasks:
                running = tasks.create_task(runner.run_once())
                await asyncio.wait_for(entered.wait(), timeout=5)
                elapsed = ATTEMPT_TIMEOUT_SECONDS + 1
                assert await asyncio.wait_for(running, timeout=5)
            assert not any(call.kwargs.get("write") for call in sessions.call_args_list)

        assert cancelled.is_set()
        queue.complete.assert_not_awaited()
        queue.record_attempt_failure.assert_awaited_once_with(
            job, "operation_timeout", retry_delay
        )

    asyncio.run(exercise())
