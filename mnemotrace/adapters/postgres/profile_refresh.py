"""持久化画像分组进度、时间唤醒和空间任务租约。"""

from collections.abc import Mapping
from datetime import timedelta
from typing import cast
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.errors import LockNotAvailable
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...application.operations import LeaseLost, WriteModelsUnavailable
from ...application.profile import MAX_PROFILE_FACTS, ProfileEvidence
from ...application.profile_refresh import (
    ClaimedProfileRefresh,
    PendingRefreshStatus,
    ProfileRefreshState,
    ProfileSnapshot,
    SnapshotChanged,
)
from ...application.task_policy import LEASE_DURATION
from ...domain.entity import Entity
from ...domain.enums import ProfileGroup
from ...domain.ids import FactId, SpaceId
from ...domain.profile import Profile, ProfileEntry
from ...ports.storage import SpaceReader, SpaceSession, SpaceUnavailable
from .profile_schedule import (
    GROUP_TABLE,
    TABLE,
    advance_due_profile_transitions,
    mark_profile_groups,
    next_profile_transition,
    profile_fact_scope,
    publish_profile_group_revisions,
)
from .reader import PostgresReader
from .session import PostgresSession
from .storage import PostgresStorage, _session_transaction

_ACTIVE_LEASE = """
    space_id = %(space)s AND request_version = %(version)s AND status = 'running'
    AND lease_token = %(token)s AND lease_until > clock_timestamp()
    AND (trigger_kind = 'manual' OR EXISTS (
        SELECT 1 FROM mnemotrace.spaces AS space
        WHERE space.space_id = %(space)s AND space.profile_auto_refresh
    ))
    """

_NEXT_DUE_SPACE = """
    SELECT space_id FROM mnemotrace.spaces
    WHERE profile_auto_refresh AND profile_next_transition_at IS NOT NULL
      AND profile_next_transition_at <= %s
    ORDER BY profile_next_transition_at, space_id
    LIMIT 1 FOR UPDATE SKIP LOCKED
    """


def _lease(job: ClaimedProfileRefresh) -> dict[str, str | int]:
    return {
        "space": job.space_id.value,
        "version": job.version,
        "token": job.lease_token,
    }


class PostgresProfileRefresh:
    """在持空间写锁的事务中发布画像并结算任务。"""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[TupleRow]]) -> None:
        self._pool = pool

    def _storage(self, owner_id: str) -> PostgresStorage:
        return PostgresStorage(self._pool, owner_id=owner_id)

    async def submit(
        self, owner_id: str, space_id: SpaceId, *, configured: bool
    ) -> PendingRefreshStatus:
        """为四组发起手动刷新，合并已有手动请求或提升自动任务。"""
        async with self._storage(owner_id).session(space_id, write=True) as writer:
            conn = cast(PostgresSession, writer)._conn
            cursor = await conn.execute(
                f"SELECT status, trigger_kind FROM {TABLE} WHERE space_id = %s FOR UPDATE",
                (space_id.value,),
            )
            row = await cursor.fetchone()
            if row is not None and row[0] in ("queued", "running"):
                status: PendingRefreshStatus = (
                    "running" if row[0] == "running" else "queued"
                )
                if row[1] == "auto":
                    pending = await self._pending(writer)
                    await mark_profile_groups(
                        conn,
                        space_id,
                        set(ProfileGroup).difference(pending),
                    )
                    await conn.execute(
                        f"""
                        UPDATE {TABLE} SET trigger_kind = 'manual',
                            available_at = CASE WHEN status = 'queued' AND attempts = 0
                                THEN LEAST(available_at, clock_timestamp()) ELSE available_at END
                        WHERE space_id = %s
                        """,
                        (space_id.value,),
                    )
                return status
            if not configured:
                raise WriteModelsUnavailable
            await mark_profile_groups(conn, space_id, tuple(ProfileGroup))
            await conn.execute(
                f"""
                INSERT INTO {TABLE} AS current (space_id) VALUES (%s)
                ON CONFLICT (space_id) DO UPDATE SET
                    request_version = current.request_version + 1,
                    status = 'queued', available_at = statement_timestamp(), attempts = 0,
                    error_code = NULL, trigger_kind = 'manual'
                """,
                (space_id.value,),
            )
            return "queued"

    @staticmethod
    async def get(reader: SpaceReader) -> ProfileRefreshState:
        """在同一授权快照内读取空间刷新状态。"""
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            f"SELECT status, CASE WHEN attempts > 0 THEN available_at END, error_code FROM {TABLE} WHERE space_id = %s",
            (reader.space_id.value,),
        )
        row = await cursor.fetchone()
        return (
            ProfileRefreshState(*row)
            if row is not None
            else ProfileRefreshState("idle")
        )

    async def wake_due(self, reference_time: float) -> None:
        """持有选中空间的写锁，将到期证据边界转换为持久刷新请求。"""
        try:
            async with (
                self._pool.connection() as conn,
                _session_transaction(conn, write=True),
            ):
                await conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE"
                )
                cursor = await conn.execute(_NEXT_DUE_SPACE, (reference_time,))
                row = await cursor.fetchone()
                if row is not None:
                    await advance_due_profile_transitions(
                        conn, SpaceId(row[0]), reference_time
                    )
        except LockNotAvailable:
            return

    async def claim(self) -> ClaimedProfileRefresh | None:
        """领取就绪任务或接管过期租约，由执行端结算耗尽的尝试。"""
        token = uuid4().hex
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT job.space_id, space.owner_id, job.request_version, job.attempts
                FROM {TABLE} AS job JOIN mnemotrace.spaces AS space USING (space_id)
                WHERE job.status IN ('queued', 'running')
                  AND (job.trigger_kind = 'manual' OR space.profile_auto_refresh)
                  AND COALESCE(job.available_at, job.lease_until) <= statement_timestamp()
                ORDER BY COALESCE(job.available_at, job.lease_until), job.space_id
                LIMIT 1 FOR UPDATE OF job SKIP LOCKED
                """
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            space, owner, version, attempts = row
            await conn.execute(
                f"""
                UPDATE {TABLE} SET status = 'running', available_at = NULL,
                    attempts = attempts + 1, lease_token = %s, lease_until = clock_timestamp() + %s
                WHERE space_id = %s
                """,
                (token, LEASE_DURATION, space),
            )
        return ClaimedProfileRefresh(
            SpaceId(space), owner, version, attempts + 1, token
        )

    @staticmethod
    async def _pending(reader: SpaceReader) -> dict[ProfileGroup, int]:
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            f"""
            SELECT group_name, target_revision FROM {GROUP_TABLE}
            WHERE space_id = %s AND target_revision > published_revision
            """,
            (reader.space_id.value,),
        )
        return {
            ProfileGroup(name): revision for name, revision in await cursor.fetchall()
        }

    async def snapshot(
        self, reader: SpaceReader, reference_time: float
    ) -> ProfileSnapshot:
        """在同一读快照内读取待发布组及其证据。"""
        pending = await self._pending(reader)
        conn = cast(PostgresReader, reader)._conn
        evidence: dict[ProfileGroup, tuple[ProfileEvidence, ...]] = {}
        transitions: dict[ProfileGroup, float | None] = {}
        oversized = False
        for group in ProfileGroup:
            if group not in pending:
                continue
            if not oversized:
                evidence[group] = await _read_group_evidence(
                    conn, reader.space_id, group, reference_time
                )
                oversized = len(evidence[group]) > MAX_PROFILE_FACTS
            transitions[group] = await next_profile_transition(
                conn, reader.space_id, group, reference_time
            )
        return ProfileSnapshot(
            pending,
            evidence,
            await reader.get_erasure_token(),
            reference_time,
            transitions,
        )

    async def has_active_lease(
        self, job: ClaimedProfileRefresh, reader: SpaceReader | None = None
    ) -> bool:
        """查询当前租约，传入读会话时复用其快照。"""
        if reader is not None:
            return await self._active(cast(PostgresReader, reader)._conn, job)
        async with self._pool.connection() as conn:
            return await self._active(conn, job)

    @staticmethod
    async def _active(
        conn: AsyncConnection[TupleRow],
        job: ClaimedProfileRefresh,
        *,
        lock: bool = False,
    ) -> bool:
        tail = " FOR UPDATE" if lock else ""
        cursor = await conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE {_ACTIVE_LEASE}{tail}", _lease(job)
        )
        return await cursor.fetchone() is not None

    async def _snapshot_matches(
        self, reader: SpaceReader, snapshot: ProfileSnapshot, at: float
    ) -> bool:
        checked_at = max(at, snapshot.reference_time)
        return (
            await reader.get_erasure_token() == snapshot.erasure_token
            and await self._pending(reader) == snapshot.revisions
            and all(
                boundary is None or checked_at < boundary
                for boundary in snapshot.transitions.values()
            )
        )

    async def complete(
        self,
        job: ClaimedProfileRefresh,
        writer: SpaceSession,
        snapshot: ProfileSnapshot,
        data: Mapping[ProfileGroup, tuple[ProfileEntry, ...]],
        *,
        at: float,
    ) -> None:
        """复核租约和证据后一起提交画像、分组进度和任务结果。"""
        session = cast(PostgresSession, writer)
        conn = session._conn
        if not await self._active(conn, job, lock=True):
            raise LeaseLost
        if not await self._snapshot_matches(writer, snapshot, at):
            raise SnapshotChanged
        old = await writer.get_profile()
        previous: dict[str, tuple[ProfileEntry, ...]] = dict.fromkeys(
            (group.value for group in ProfileGroup), ()
        )
        if old is not None:
            previous.update(old.data)
        merged = {
            **previous,
            **{group.value: entries for group, entries in data.items()},
        }
        if old is None or merged != previous:
            await session._store_profile(
                Profile(job.space_id, merged, at), require_monotonic=False
            )
        await publish_profile_group_revisions(conn, job.space_id, snapshot.transitions)
        await _settle(conn, job.space_id, None)

    async def record_attempt_failure(
        self,
        job: ClaimedProfileRefresh,
        code: str,
        retry_delay: timedelta | None,
        *,
        snapshot: ProfileSnapshot | None,
        at: float,
    ) -> None:
        """为整个空间任务退避、重新入队或登记失败。"""
        try:
            async with self._storage(job.owner_id).session(
                job.space_id, write=True
            ) as writer:
                conn = cast(PostgresSession, writer)._conn
                if not await self._active(conn, job, lock=True):
                    return
                cursor = await conn.execute(
                    f"SELECT followup_at FROM {TABLE} WHERE space_id = %s",
                    (job.space_id.value,),
                )
                row = await cursor.fetchone()
                assert row is not None
                (followup_at,) = row
                if code == "profile_snapshot_changed" and (
                    retry_delay is None or followup_at is not None
                ):
                    await _requeue(conn, job.space_id)
                elif retry_delay is not None:
                    await conn.execute(
                        f"""
                        UPDATE {TABLE} SET status = 'queued', available_at = clock_timestamp() + %s,
                            lease_token = NULL, lease_until = NULL
                        WHERE space_id = %s
                        """,
                        (retry_delay, job.space_id.value),
                    )
                elif followup_at is not None or (
                    snapshot is not None
                    and not await self._snapshot_matches(writer, snapshot, at)
                ):
                    await _requeue(conn, job.space_id)
                else:
                    await _settle(conn, job.space_id, code)
        except SpaceUnavailable:
            return


async def _read_group_evidence(
    conn: AsyncConnection[TupleRow], space: SpaceId, group: ProfileGroup, at: float
) -> tuple[ProfileEvidence, ...]:
    cursor = await conn.execute(
        _group_evidence_query(group),
        {
            "space": space.value,
            "subject": Entity.space_user(space).id.value,
            "at": at,
            "limit": MAX_PROFILE_FACTS + 1,
        },
    )
    return tuple(
        ProfileEvidence(FactId(fact_id), predicate, text, start, end)
        for fact_id, predicate, text, start, end in await cursor.fetchall()
    )


def _group_evidence_query(group: ProfileGroup) -> sql.Composed:
    return sql.SQL("""
        SELECT fact_id, predicate, text, valid_at, invalid_at
        FROM (
            SELECT fact_id, predicate, text, valid_at, invalid_at, recorded_at
            FROM mnemotrace.facts
            WHERE {} AND mnemotrace.epoch_range(valid_at, invalid_at, '[)') @> %(at)s::double precision
            LIMIT %(limit)s
        ) AS evidence
        ORDER BY valid_at DESC NULLS LAST, recorded_at DESC, fact_id
        """).format(profile_fact_scope(group))


async def _requeue(conn: AsyncConnection[TupleRow], space: SpaceId) -> None:
    await conn.execute(
        f"""
        UPDATE {TABLE} SET status = 'queued', available_at = COALESCE(followup_at, clock_timestamp()),
            lease_token = NULL, lease_until = NULL, request_version = request_version + 1,
            error_code = NULL, attempts = 0, followup_at = NULL
        WHERE space_id = %s
        """,
        (space.value,),
    )


async def _settle(
    conn: AsyncConnection[TupleRow], space: SpaceId, error: str | None
) -> None:
    await conn.execute(
        f"""
        UPDATE {TABLE} SET status = %s, available_at = NULL, error_code = %s,
            lease_token = NULL, lease_until = NULL, followup_at = NULL
        WHERE space_id = %s
        """,
        ("failed" if error else "succeeded", error, space.value),
    )
