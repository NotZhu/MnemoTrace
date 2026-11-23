"""领取会话摘要任务，并从已提交原文更新摘要。"""

import logging
from asyncio import timeout
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from ..domain.ids import SessionId, SpaceId
from ..domain.message import Message
from ..ports.inference import LLM
from ..ports.storage import (
    CommitOutcomeUnknown,
    SpaceReader,
    SpaceSession,
    SpaceUnavailable,
    Storage,
)
from .operations import (
    ATTEMPT_TIMEOUT,
    LeaseLost,
    classify_worker_failure,
    retry_delay_for,
)
from .session_summary import generate_session_summary, prepare_summary_batch

_LOG = logging.getLogger(__name__)
_PAGE_SIZE = 16


@dataclass(frozen=True)
class ClaimedSessionSummary:
    """保存本次领取的会话、原覆盖位置、固定目标和租约。"""

    space_id: SpaceId
    session_id: SessionId
    owner_id: str
    covered_through_seq: int | None
    target_seq: int
    attempt: int
    lease_token: str


class SessionSummaryQueue(Protocol):
    """供摘要消费者领取任务、读取上版正文及结算发布。"""

    async def claim(self) -> ClaimedSessionSummary | None: ...

    async def read_previous(
        self, job: ClaimedSessionSummary, reader: SpaceReader
    ) -> str | None: ...

    async def has_active_lease(self, job: ClaimedSessionSummary) -> bool: ...

    async def complete(
        self,
        job: ClaimedSessionSummary,
        writer: SpaceSession,
        through: int,
        summary: str,
    ) -> None: ...

    async def record_attempt_failure(
        self, job: ClaimedSessionSummary, code: str, retry_delay: timedelta | None
    ) -> None: ...


class _SnapshotChanged(RuntimeError):
    """摘要所用的快照在推理期间发生擦除。"""


class SessionSummaryRunner:
    """在读快照准备批次、事务外生成，再凭租约原子发布。"""

    def __init__(
        self,
        queue: SessionSummaryQueue,
        storage_for: Callable[[str], Storage],
        llm: LLM,
        transient: Callable[[Exception], bool],
    ) -> None:
        self._queue = queue
        self._storage_for = storage_for
        self._llm = llm
        self._transient = transient

    async def run_once(self) -> bool:
        """处理一项可领取任务，返回是否领到任务。"""
        job = await self._queue.claim()
        if job is None:
            return False
        try:
            async with timeout(ATTEMPT_TIMEOUT):
                await self._refresh(job)
        except (LeaseLost, SpaceUnavailable):
            return True
        except CommitOutcomeUnknown:
            if await self._queue.has_active_lease(job):
                await self._fail(job, "commit_outcome_unknown", retry=True)
        except Exception as exc:  # noqa: BLE001
            # 单个任务失败不能阻塞同进程的其他摘要。
            code, retry = self._classify_failure(exc)
            await self._fail(job, code, retry=retry)
        return True

    async def _refresh(self, job: ClaimedSessionSummary) -> None:
        storage = self._storage_for(job.owner_id)
        async with storage.session(job.space_id) as reader:
            previous = await self._queue.read_previous(job, reader)
            erasure = await reader.get_erasure_token()
            batch = await prepare_summary_batch(
                previous_summary=previous, messages=_pending_messages(reader, job)
            )
        if batch is None:
            raise RuntimeError("摘要任务缺少待处理原文")
        summary = await generate_session_summary(self._llm, batch)

        async with storage.session(job.space_id, write=True) as writer:
            if await writer.get_erasure_token() != erasure:
                raise _SnapshotChanged
            await self._queue.complete(job, writer, batch.covered_through_seq, summary)

    def _classify_failure(self, error: Exception) -> tuple[str, bool]:
        if isinstance(error, _SnapshotChanged):
            return "summary_snapshot_changed", True
        return classify_worker_failure(error, self._transient)

    async def _fail(
        self, job: ClaimedSessionSummary, code: str, *, retry: bool
    ) -> None:
        _LOG.warning(
            "会话摘要失败 space_id=%s session_id=%s attempt=%d code=%s",
            job.space_id.value,
            job.session_id.value,
            job.attempt,
            code,
        )
        await self._queue.record_attempt_failure(
            job, code, retry_delay_for(job.attempt, retry=retry)
        )


async def _pending_messages(
    reader: SpaceReader, job: ClaimedSessionSummary
) -> AsyncIterator[tuple[int, Message]]:
    after = job.covered_through_seq
    while after != job.target_seq:
        page = await reader.list_session_messages(
            job.session_id, after=after, limit=_PAGE_SIZE, through=job.target_seq
        )
        if not page:
            return
        for row in page:
            yield row
        after = page[-1][0]
