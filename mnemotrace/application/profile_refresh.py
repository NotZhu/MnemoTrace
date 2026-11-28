"""领取空间刷新任务，按组读取证据并一次提交画像。"""

import logging
from asyncio import timeout
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from ..domain.enums import ProfileGroup
from ..domain.ids import SpaceId
from ..domain.profile import ProfileEntry
from ..ports.inference import LLM
from ..ports.runtime import Clock
from ..ports.storage import (
    CommitOutcomeUnknown,
    SpaceReader,
    SpaceSession,
    SpaceUnavailable,
    Storage,
)
from .profile import ProfileEvidence, ProfileInputTooLarge, generate_profile
from .task_policy import (
    ATTEMPT_TIMEOUT_SECONDS,
    ClaimOutcome,
    LeaseLost,
    classify_worker_failure,
    retry_delay_for,
)

type PendingRefreshStatus = Literal["queued", "running"]
type RefreshStatus = Literal["idle", "queued", "running", "succeeded", "failed"]
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileRefreshState:
    """描述空间画像的刷新状态。"""

    status: RefreshStatus
    next_retry_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ClaimedProfileRefresh:
    """保存一次空间任务的版本和租约。"""

    space_id: SpaceId
    owner_id: str
    version: int
    attempt: int
    lease_token: str


@dataclass(frozen=True)
class ProfileSnapshot:
    """同一读快照里的分组版本、时间边界及有界证据。"""

    revisions: Mapping[ProfileGroup, int]
    evidence: Mapping[ProfileGroup, tuple[ProfileEvidence, ...]]
    erasure_token: str
    reference_time: float
    transitions: Mapping[ProfileGroup, float | None]


class ProfileRefreshQueue(Protocol):
    """画像刷新执行端使用的持久队列。"""

    async def wake_due(self, reference_time: float) -> None: ...

    async def claim(self) -> ClaimedProfileRefresh | ClaimOutcome: ...

    async def snapshot(
        self, reader: SpaceReader, reference_time: float
    ) -> ProfileSnapshot: ...

    async def has_active_lease(
        self, job: ClaimedProfileRefresh, reader: SpaceReader | None = None
    ) -> bool: ...

    async def complete(
        self,
        job: ClaimedProfileRefresh,
        writer: SpaceSession,
        snapshot: ProfileSnapshot,
        data: Mapping[ProfileGroup, tuple[ProfileEntry, ...]],
        *,
        at: float,
    ) -> None: ...

    async def record_attempt_failure(
        self,
        job: ClaimedProfileRefresh,
        code: str,
        retry_delay: timedelta | None,
        *,
        snapshot_changed: bool,
        snapshot: ProfileSnapshot | None,
        at: float,
    ) -> None: ...


class SnapshotChanged(RuntimeError):
    """本次刷新所需的分组或证据已变化。"""


class ProfileRefreshRunner:
    """在空间写锁外生成画像，在锁内复核并提交结果。"""

    def __init__(
        self,
        queue: ProfileRefreshQueue,
        storage_for: Callable[[str], Storage],
        llm: LLM,
        clock: Clock,
        transient: Callable[[Exception], bool],
    ) -> None:
        self._queue = queue
        self._storage_for = storage_for
        self._llm = llm
        self._clock = clock
        self._transient = transient

    async def run_once(self) -> bool:
        """唤醒到期分组，返回是否领取或结算了一项任务。"""
        try:
            await self._queue.wake_due(self._clock.now())
        except CommitOutcomeUnknown:
            _LOG.warning("画像时间边界入队结果未确认")
        job = await self._queue.claim()
        if isinstance(job, ClaimOutcome):
            return job is ClaimOutcome.SETTLED
        snapshot = None
        try:
            async with timeout(ATTEMPT_TIMEOUT_SECONDS):
                storage = self._storage_for(job.owner_id)
                async with storage.session(job.space_id) as reader:
                    if not await self._queue.has_active_lease(job, reader):
                        raise LeaseLost
                    snapshot = await self._queue.snapshot(reader, self._clock.now())
                data = await generate_profile(self._llm, snapshot.evidence)
                async with storage.session(job.space_id, write=True) as writer:
                    await self._queue.complete(
                        job, writer, snapshot, data, at=self._clock.now()
                    )
        except (LeaseLost, SpaceUnavailable):
            _LOG.info(
                "画像刷新租约失效 space_id=%s version=%d",
                job.space_id.value,
                job.version,
            )
        except CommitOutcomeUnknown:
            if await self._queue.has_active_lease(job):
                await self._fail(
                    job, "commit_outcome_unknown", retry=True, snapshot=snapshot
                )
        except SnapshotChanged:
            await self._fail(
                job,
                "profile_snapshot_changed",
                retry=True,
                snapshot_changed=True,
                snapshot=snapshot,
            )
        except Exception as error:  # noqa: BLE001
            code, retry = self._classify_failure(error)
            await self._fail(job, code, retry=retry, snapshot=snapshot)
        return True

    def _classify_failure(self, error: Exception) -> tuple[str, bool]:
        if isinstance(error, ProfileInputTooLarge):
            return "profile_input_too_large", False
        return classify_worker_failure(error, self._transient)

    async def _fail(
        self,
        job: ClaimedProfileRefresh,
        code: str,
        *,
        retry: bool,
        snapshot_changed: bool = False,
        snapshot: ProfileSnapshot | None = None,
    ) -> None:
        _LOG.warning(
            "画像刷新失败 space_id=%s version=%d attempt=%d code=%s",
            job.space_id.value,
            job.version,
            job.attempt,
            code,
        )
        try:
            await self._queue.record_attempt_failure(
                job,
                code,
                retry_delay_for(job.attempt, retry=retry),
                snapshot_changed=snapshot_changed,
                snapshot=snapshot,
                at=self._clock.now(),
            )
        except CommitOutcomeUnknown:
            _LOG.warning("画像失败结算结果未确认 space_id=%s", job.space_id.value)
