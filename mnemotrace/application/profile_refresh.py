"""从已提交的空间事实生成并安全发布画像快照。"""

import logging
from asyncio import timeout
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from ..domain.entity import Entity
from ..domain.enums import Source
from ..domain.fact import Fact
from ..domain.ids import FactId, SpaceId
from ..domain.profile import Profile
from ..domain.values import EntityRef
from ..ports.inference import LLM
from ..ports.queries import FactCursor, FactFilter, FactQuery
from ..ports.runtime import Clock
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
from .profile import ProfileInputTooLarge, generate_profile

type PendingRefreshStatus = Literal["queued", "running"]
type RefreshStatus = Literal["idle", "queued", "running", "succeeded", "failed"]
_LOG = logging.getLogger(__name__)
_PAGE_SIZE = 1000


@dataclass(frozen=True)
class ProfileRefreshState:
    """描述同一空间最近一次刷新及其下一步。"""

    status: RefreshStatus
    next_retry_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ClaimedProfileRefresh:
    """保存一次领取的版本、租约及上一版事实游标。"""

    space_id: SpaceId
    owner_id: str
    version: int
    attempt: int
    lease_token: str
    last_success_fact_cursor: int | None


@dataclass(frozen=True)
class ProfileEvidenceVersion:
    """标记读快照中的擦除代际和画像证据版本。"""

    erasure_generation: int
    revision: int


class ProfileRefreshQueue(Protocol):
    """画像刷新执行端使用的持久队列。"""

    async def claim(self) -> ClaimedProfileRefresh | None: ...

    async def changes_since(
        self, reader: SpaceReader, after: int | None
    ) -> tuple[int, tuple[FactId, ...]]: ...

    async def evidence_version(self, reader: SpaceReader) -> ProfileEvidenceVersion: ...

    async def next_activation(
        self, reader: SpaceReader, after: float
    ) -> float | None: ...

    async def has_active_lease(
        self, job: ClaimedProfileRefresh, reader: SpaceReader | None = None
    ) -> bool: ...

    async def complete(
        self,
        job: ClaimedProfileRefresh,
        writer: SpaceSession,
        fact_cursor: int,
        profile: Profile,
    ) -> None: ...

    async def record_attempt_failure(
        self, job: ClaimedProfileRefresh, code: str, retry_delay: timedelta | None
    ) -> None: ...


class _SnapshotChanged(RuntimeError):
    """推理期间影响归纳的证据快照发生了变化。"""


class ProfileRefreshRunner:
    """领取空间刷新，锁外生成，锁内复核并发布。"""

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
        """尝试处理一次画像刷新，返回是否领到任务。"""
        job = await self._queue.claim()
        if job is None:
            return False
        try:
            async with timeout(ATTEMPT_TIMEOUT):
                await self._refresh(job)
        except (LeaseLost, SpaceUnavailable):
            _LOG.info(
                "画像刷新已失效 space_id=%s version=%d", job.space_id.value, job.version
            )
        except CommitOutcomeUnknown:
            if await self._queue.has_active_lease(job):
                await self._fail(job, "commit_outcome_unknown", retry=True)
        except Exception as exc:  # noqa: BLE001
            # 单个空间的未知错误不能让 worker 放弃其他已持久接收的任务。
            code, retry = self._classify_failure(exc)
            await self._fail(job, code, retry=retry)
        return True

    async def _refresh(self, job: ClaimedProfileRefresh) -> None:
        storage = self._storage_for(job.owner_id)
        async with storage.session(job.space_id) as reader:
            if not await self._queue.has_active_lease(job, reader):
                raise LeaseLost
            reference_time = self._clock.now()
            version = await self._queue.evidence_version(reader)
            previous = await reader.get_profile()
            facts = await _eligible_facts(reader, reference_time)
            deadline = await self._next_evidence_change(reader, facts, reference_time)
            cursor, changed_ids = await self._queue.changes_since(
                reader, job.last_success_fact_cursor
            )
        data = await generate_profile(
            self._llm,
            space_id=job.space_id,
            facts=tuple(facts.values()),
            reference_time=reference_time,
            changed_fact_ids=changed_ids,
            previous_fact_ids=previous.source_fact_ids if previous is not None else (),
        )
        async with storage.session(job.space_id) as reader:
            checked_at = max(reference_time, self._clock.now())
            checked_version = await self._queue.evidence_version(reader)
            if checked_version.erasure_generation != version.erasure_generation:
                raise _SnapshotChanged
            if checked_version != version or (
                deadline is not None and checked_at >= deadline
            ):
                # 版本可能记录了相互抵消的事实变更，锁外比对可复用仍有效的结果。
                current = await _eligible_facts(reader, checked_at)
                if current.keys() != facts.keys() or any(
                    not fact.same_except_provenance(current[fact_id])
                    or current[fact_id].provenance[: len(fact.provenance)]
                    != fact.provenance
                    for fact_id, fact in facts.items()
                ):
                    raise _SnapshotChanged
                deadline = await self._next_evidence_change(reader, current, checked_at)

        async with storage.session(job.space_id, write=True) as writer:
            if await self._queue.evidence_version(writer) != checked_version:
                raise _SnapshotChanged
            published_at = self._clock.now()
            if deadline is not None and max(checked_at, published_at) >= deadline:
                raise _SnapshotChanged
            await self._queue.complete(
                job, writer, cursor, Profile(job.space_id, data, published_at)
            )

    async def _next_evidence_change(
        self, reader: SpaceReader, facts: dict[FactId, Fact], at: float
    ) -> float | None:
        activation = await self._queue.next_activation(reader, at)
        expiry = min(
            (fact.invalid_at for fact in facts.values() if fact.invalid_at is not None),
            default=None,
        )
        return min(
            (point for point in (activation, expiry) if point is not None), default=None
        )

    def _classify_failure(self, error: Exception) -> tuple[str, bool]:
        if isinstance(error, _SnapshotChanged):
            return "profile_snapshot_changed", True
        if isinstance(error, ProfileInputTooLarge):
            return "profile_input_too_large", False
        return classify_worker_failure(error, self._transient)

    async def _fail(
        self, job: ClaimedProfileRefresh, code: str, *, retry: bool
    ) -> None:
        delay = retry_delay_for(job.attempt, retry=retry)
        _LOG.warning(
            "画像刷新失败 space_id=%s version=%d attempt=%d code=%s",
            job.space_id.value,
            job.version,
            job.attempt,
            code,
        )
        await self._queue.record_attempt_failure(job, code, delay)


async def _eligible_facts(reader: SpaceReader, at: float) -> dict[FactId, Fact]:
    filters = FactFilter(
        subject=EntityRef(Entity.space_user(reader.space_id).id),
        source=Source.EXTRACTED,
        exclude_sensitive=True,
    )
    evidence: dict[FactId, Fact] = {}
    after: FactCursor | None = None
    while True:
        page = await reader.query_facts(
            FactQuery(filters, after, _PAGE_SIZE), reference_time=at
        )
        evidence.update((fact.id, fact) for fact in page)
        if len(page) < _PAGE_SIZE:
            return evidence
        after = FactCursor(page[-1].recorded_at, page[-1].id)
