"""通过真实写入流程验证任务故障处理。"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from math import nan
from unittest.mock import patch

import pytest

from mnemotrace.application.operations import ClaimedOperation, OperationRunner
from mnemotrace.application.remember import (
    MessageInput,
    Remember,
    RememberResult,
    RememberStage,
)
from mnemotrace.application.task_policy import ClaimOutcome
from mnemotrace.domain.ids import SessionId, SpaceId
from mnemotrace.domain.space import Space
from mnemotrace.ports.inference import LLM, Embedder
from mnemotrace.ports.queries import FactFilter, FactQuery
from mnemotrace.ports.storage import CommitOutcomeUnknown, SpaceSession, Storage

from ..ports.fakes import FakeClock, FakeEmbedder, FakeIdGenerator, FakeLLM, FakeStorage

_SPACE = Space(SpaceId("space"), "owner", "测试空间", 10.0)
_JOB = ClaimedOperation(
    operation_id="operation",
    space_id=_SPACE.id,
    owner_id=_SPACE.owner_id,
    session_id=SessionId("session"),
    messages=(MessageInput("assistant", "合成回复"),),
    received_at=20.0,
    attempt=1,
    lease_token="lease",
)


class _RecordingQueue:
    def __init__(self, job: ClaimedOperation, *, active_lease: bool = False) -> None:
        self._pending: ClaimedOperation | ClaimOutcome = job
        self._active_lease = active_lease
        self.completions: list[tuple[ClaimedOperation, RememberResult]] = []
        self.lease_checks: list[ClaimedOperation] = []
        self.failures: list[
            tuple[ClaimedOperation, str, RememberStage | None, timedelta | None]
        ] = []

    async def claim(self) -> ClaimedOperation | ClaimOutcome:
        job, self._pending = self._pending, ClaimOutcome.EMPTY
        return job

    async def complete(
        self, job: ClaimedOperation, writer: SpaceSession, result: RememberResult
    ) -> None:
        (message_id,) = result.message_ids
        assert (await writer.get_messages((message_id,)))[message_id].session_id == (
            job.session_id
        )
        self.completions.append((job, result))

    async def has_active_lease(self, job: ClaimedOperation) -> bool:
        self.lease_checks.append(job)
        return self._active_lease

    async def record_attempt_failure(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        retry_delay: timedelta | None,
    ) -> None:
        self.failures.append((job, code, stage, retry_delay))


class _CommitUnknownStorage(FakeStorage):
    fail_commit = False

    @asynccontextmanager
    async def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AsyncIterator[SpaceSession]:
        async with super().session(space_id, write=write) as session:
            yield session
        if write and self.fail_commit:
            raise CommitOutcomeUnknown("合成提交结果未知")


class _ExtractionModel(FakeLLM):
    def __init__(self, fact_count: int) -> None:
        self._fact_count = fact_count

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        return {
            "facts": [
                {
                    "subject": {"kind": "user", "value": None},
                    "predicate": "preference",
                    "object": {"kind": "literal", "value": f"选项 {index}"},
                    "text": f"用户偏好选项 {index}",
                    "valid_at_evidence": None,
                    "invalid_at_evidence": None,
                    "source_numbers": [1],
                    "profile_groups": [],
                    "labels": [],
                    "sensitive": False,
                }
                for index in range(self._fact_count)
            ]
        }


class _EmbeddingResult(FakeEmbedder):
    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        self._vectors = vectors
        self.requests: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.requests.append(tuple(texts))
        return self._vectors


def _runner(
    queue: _RecordingQueue,
    storage: Storage,
    *,
    llm: LLM | None = None,
    embedder: Embedder | None = None,
) -> OperationRunner:
    return OperationRunner(
        queue,
        lambda job: Remember(
            space_id=job.space_id,
            storage=storage,
            llm=FakeLLM() if llm is None else llm,
            embedder=FakeEmbedder() if embedder is None else embedder,
            clock=FakeClock(),
            ids=FakeIdGenerator(),
        ),
        transient=lambda _: False,
    )


@pytest.mark.parametrize("active_lease", (True, False))
def test_commit_outcome_unknown_rechecks_lease(active_lease: bool) -> None:
    """发布提交结果未知时，只有仍持有租约才登记重试。"""

    async def exercise() -> None:
        queue = _RecordingQueue(_JOB, active_lease=active_lease)
        storage = _CommitUnknownStorage()
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.register_session(_JOB.session_id)
        storage.fail_commit = True

        assert await _runner(queue, storage).run_once()
        ((completed_job, result),) = queue.completions
        assert completed_job == _JOB and result.fact_changes == ()
        assert queue.lease_checks == [_JOB]
        assert queue.failures == (
            [(_JOB, "commit_outcome_unknown", "publish", timedelta(seconds=5))]
            if active_lease
            else []
        )

    asyncio.run(exercise())


def test_missing_space_invalidates_operation_without_retry() -> None:
    """目标空间缺失时，任务记录写入失效并终止。"""

    async def exercise() -> None:
        queue = _RecordingQueue(_JOB)

        assert await _runner(queue, FakeStorage()).run_once()
        assert queue.completions == []
        assert queue.lease_checks == []
        assert queue.failures == [(_JOB, "write_invalidated", None, None)]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("attempt", "retry_delay"),
    [(1, timedelta(seconds=5)), (2, timedelta(seconds=10)), (3, None)],
)
def test_attempt_deadline_cancels_extraction_before_publish(
    attempt: int, retry_delay: timedelta | None
) -> None:
    """执行截止取消阻塞的抽取，按当前尝试次数处理后续状态。"""

    async def exercise() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        loop = asyncio.get_running_loop()
        real_time = loop.time
        clock_offset = 0.0

        def loop_time() -> float:
            return real_time() + clock_offset

        class BlockingModel(FakeLLM):
            async def _structured_response(
                self, *, system: str, prompt: str, schema: Mapping[str, object]
            ) -> Mapping[str, object]:
                started.set()
                try:
                    await asyncio.Future[None]()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return {"facts": []}

        job = replace(
            _JOB, attempt=attempt, messages=(MessageInput("user", "合成偏好"),)
        )
        queue, storage = _RecordingQueue(job), FakeStorage()
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.register_session(job.session_id)

        runner = _runner(queue, storage, llm=BlockingModel())
        with (
            patch.object(storage, "session", wraps=storage.session) as sessions,
            patch.object(loop, "time", loop_time),
        ):
            async with asyncio.TaskGroup() as tasks:
                running = tasks.create_task(runner.run_once())
                await asyncio.wait_for(started.wait(), timeout=5)
                # 让模型先进入等待，再推进事件循环时钟触发 worker 的执行截止。
                clock_offset = 181.0
                assert await asyncio.wait_for(running, timeout=5)
            assert not any(call.kwargs.get("write") for call in sessions.call_args_list)

        assert cancelled.is_set()
        assert queue.completions == []
        assert queue.failures == [(job, "operation_timeout", "extraction", retry_delay)]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("fact_count", "vectors"),
    (
        pytest.param(1, ((0.0, 0.0),), id="zero-vector"),
        pytest.param(2, ((1.0, 0.0), (nan, 0.0)), id="nonfinite-last-vector"),
        pytest.param(2, ((1.0, 0.0),), id="wrong-count"),
        pytest.param(1, ((1.0, 0.0, 1.0),), id="wrong-dimension"),
    ),
)
def test_invalid_embedding_fails_before_publishing(
    fact_count: int, vectors: Sequence[Sequence[float]]
) -> None:
    """嵌入结果非法时整批终止，任务记录嵌入阶段的推理故障。"""

    async def exercise() -> None:
        job = replace(_JOB, messages=(MessageInput("user", "合成偏好"),))
        queue = _RecordingQueue(job)
        storage = FakeStorage()
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.register_session(job.session_id)

        embedder = _EmbeddingResult(vectors)
        runner = _runner(
            queue, storage, llm=_ExtractionModel(fact_count), embedder=embedder
        )
        assert await runner.run_once()
        assert embedder.requests == [
            tuple(f"用户偏好选项 {index}" for index in range(fact_count))
        ]
        assert queue.completions == []
        assert queue.failures == [(job, "inference_failed", "embedding", None)]
        async with storage.session(_SPACE.id) as reader:
            assert not await reader.last_messages(job.session_id, limit=1)
            assert not await reader.query_facts(
                FactQuery(filters=FactFilter(state="all")),
                reference_time=job.received_at,
            )

    asyncio.run(exercise())
