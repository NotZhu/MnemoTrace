"""将空间画像的刷新请求与发布租约持久化到 PostgreSQL。"""

from datetime import datetime, timedelta
from typing import cast
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...application.operations import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    LeaseLost,
    WriteModelsUnavailable,
)
from ...application.profile_refresh import (
    ClaimedProfileRefresh,
    PendingRefreshStatus,
    ProfileEvidenceVersion,
    ProfileRefreshState,
)
from ...domain.entity import Entity
from ...domain.ids import FactId, SpaceId
from ...domain.profile import Profile
from ...ports.storage import SpaceReader, SpaceSession
from .profile_schedule import TABLE as _TABLE
from .reader import PostgresReader
from .session import PostgresSession
from .storage import PostgresStorage

_ACTIVE_LEASE = """
    space_id = %(space)s AND request_version = %(version)s AND status = 'running'
    AND lease_token = %(token)s AND lease_until > clock_timestamp()
    AND (trigger_kind = 'manual' OR EXISTS (
        SELECT 1 FROM mnemotrace_next.spaces AS space
        WHERE space.space_id = %(space)s AND space.profile_auto_refresh
    ))
    """


def _lease(job: ClaimedProfileRefresh) -> dict[str, str | int]:
    return {
        "space": job.space_id.value,
        "version": job.version,
        "token": job.lease_token,
    }


class PostgresProfileRefresh:
    """持久接收画像刷新并用有效租约约束状态变更。"""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[TupleRow]]) -> None:
        self._pool = pool

    async def submit(
        self, owner_id: str, space_id: SpaceId, *, configured: bool
    ) -> PendingRefreshStatus:
        """授权并入队，将在途自动任务提升为手动刷新。"""
        storage = PostgresStorage(self._pool, owner_id=owner_id)
        async with storage.session(space_id, write=True) as writer:
            conn = cast(PostgresReader, writer)._conn
            cursor = await conn.execute(
                f"""
                SELECT status, trigger_kind FROM {_TABLE}
                WHERE space_id = %s FOR UPDATE
                """,
                (space_id.value,),
            )
            row = await cursor.fetchone()
            if row is not None:
                status, trigger = row
                if status in ("queued", "running"):
                    if trigger == "auto":
                        await conn.execute(
                            f"""
                            UPDATE {_TABLE} SET trigger_kind = 'manual',
                                available_at = CASE WHEN status = 'queued' AND attempts = 0
                                    THEN LEAST(available_at, clock_timestamp())
                                    ELSE available_at END
                            WHERE space_id = %s
                            """,
                            (space_id.value,),
                        )
                    return "queued" if status == "queued" else "running"
            if not configured:
                raise WriteModelsUnavailable
            await conn.execute(
                f"""
                INSERT INTO {_TABLE} AS current (space_id)
                VALUES (%s)
                ON CONFLICT (space_id) DO UPDATE SET
                    request_version = current.request_version + 1,
                    status = 'queued', available_at = statement_timestamp(),
                    attempts = 0, error_code = NULL, trigger_kind = 'manual'
                """,
                (space_id.value,),
            )
            return "queued"

    @staticmethod
    async def get(reader: SpaceReader) -> ProfileRefreshState:
        """在调用方的已授权快照内读取公开刷新状态。"""
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            f"""
            SELECT status, CASE WHEN attempts > 0 THEN available_at END, error_code
            FROM {_TABLE} WHERE space_id = %s
            """,
            (reader.space_id.value,),
        )
        row = await cursor.fetchone()
        return (
            ProfileRefreshState(*row)
            if row is not None
            else ProfileRefreshState("idle")
        )

    async def claim(self) -> ClaimedProfileRefresh | None:
        """领取一项就绪刷新，或结算重试耗尽的过期租约。"""
        token = uuid4().hex
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT job.space_id, space.owner_id, job.request_version, job.attempts,
                    job.last_success_fact_cursor, job.followup_at
                FROM {_TABLE} AS job JOIN mnemotrace_next.spaces AS space USING (space_id)
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
            space_id, owner, version, attempts, fact_cursor, followup_at = row
            if attempts >= MAX_ATTEMPTS:
                await _finish_attempt(
                    conn, space_id, "worker_lease_expired", None, followup_at
                )
                return None
            await conn.execute(
                f"""
                UPDATE {_TABLE} SET status = 'running', available_at = NULL,
                    attempts = attempts + 1, lease_token = %s,
                    lease_until = clock_timestamp() + %s
                WHERE space_id = %s
                """,
                (token, timedelta(seconds=LEASE_SECONDS), space_id),
            )
        return ClaimedProfileRefresh(
            SpaceId(space_id), owner, version, attempts + 1, token, fact_cursor
        )

    async def changes_since(
        self, reader: SpaceReader, after: int | None
    ) -> tuple[int, tuple[FactId, ...]]:
        """读取同一快照的事实进度与非单纯追加证据的变更标识。"""
        conn = cast(PostgresReader, reader)._conn
        space = reader.space_id.value
        cursor = await conn.execute(
            """
            SELECT COALESCE(max(cursor), 0) FROM mnemotrace_next.fact_changes
            WHERE space_id = %s
            """,
            (space,),
        )
        row = await cursor.fetchone()
        assert row is not None
        current = row[0]
        if after is None or current <= after:
            return max(current, after or 0), ()
        cursor = await conn.execute(
            """
            SELECT DISTINCT fact_id FROM mnemotrace_next.fact_changes
            WHERE space_id = %s AND cursor > %s
              AND ("before" - 'provenance') IS DISTINCT FROM ("after" - 'provenance')
            """,
            (space, after),
        )
        return current, tuple(FactId(fact_id) for (fact_id,) in await cursor.fetchall())

    async def has_active_lease(
        self, job: ClaimedProfileRefresh, reader: SpaceReader | None = None
    ) -> bool:
        """检查版本和租约，读会话未传入时查询最新状态。"""
        if reader is not None:
            return await self._active(cast(PostgresReader, reader)._conn, job)
        async with self._pool.connection() as conn:
            return await self._active(conn, job)

    @staticmethod
    async def evidence_version(reader: SpaceReader) -> ProfileEvidenceVersion:
        """读取绑定空间在当前快照中的擦除代际与证据版本。"""
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            """
            SELECT purge_generation, profile_evidence_revision
            FROM mnemotrace_next.spaces WHERE space_id = %s
            """,
            (reader.space_id.value,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return ProfileEvidenceVersion(*row)

    @staticmethod
    async def next_activation(reader: SpaceReader, after: float) -> float | None:
        """取得未来可能进入画像的最早事实生效时刻。"""
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            """
            SELECT valid_at FROM mnemotrace_next.facts
            WHERE space_id = %s AND subject_id = %s
              AND source = 'extracted' AND NOT sensitive AND valid_at > %s
            ORDER BY valid_at LIMIT 1
            """,
            (reader.space_id.value, Entity.space_user(reader.space_id).id.value, after),
        )
        row = await cursor.fetchone()
        return row[0] if row is not None else None

    @staticmethod
    async def _active(
        conn: AsyncConnection[TupleRow], job: ClaimedProfileRefresh
    ) -> bool:
        cursor = await conn.execute(
            f"SELECT 1 FROM {_TABLE} WHERE {_ACTIVE_LEASE}", _lease(job)
        )
        return await cursor.fetchone() is not None

    async def complete(
        self,
        job: ClaimedProfileRefresh,
        writer: SpaceSession,
        fact_cursor: int,
        profile: Profile,
    ) -> None:
        """凭有效租约替换画像并在同一事务内结算成功。"""
        session = cast(PostgresSession, writer)
        conn = session._conn
        cursor = await conn.execute(
            f"""
            UPDATE {_TABLE} SET status = 'succeeded', last_success_fact_cursor = %(cursor)s,
                lease_token = NULL, lease_until = NULL, followup_at = NULL
            WHERE {_ACTIVE_LEASE}
            """,
            {**_lease(job), "cursor": fact_cursor},
        )
        if cursor.rowcount != 1:
            raise LeaseLost
        # 刷新顺序由租约保证，旧快照的时间戳可能超出可表示范围。
        await session._store_profile(profile, require_monotonic=False)

    async def record_attempt_failure(
        self, job: ClaimedProfileRefresh, code: str, retry_delay: timedelta | None
    ) -> None:
        """用有效租约安排重试或公开稳定失败码。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT trigger_kind, followup_at FROM {_TABLE} WHERE {_ACTIVE_LEASE} FOR UPDATE
                """,
                _lease(job),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            trigger, followup_at = row
            if (
                trigger == "auto"
                and followup_at is not None
                and code == "profile_snapshot_changed"
            ):
                retry_delay = None
            await _finish_attempt(
                conn, job.space_id.value, code, retry_delay, followup_at
            )


async def _finish_attempt(
    conn: AsyncConnection[TupleRow],
    space_id: str,
    code: str,
    retry_delay: timedelta | None,
    followup_at: datetime | None,
) -> None:
    promote = retry_delay is None and followup_at is not None
    queued = retry_delay is not None or promote
    await conn.execute(
        f"""
        UPDATE {_TABLE} SET
            status = %(status)s,
            available_at = COALESCE(%(next)s::timestamptz, clock_timestamp() + %(delay)s::interval),
            lease_token = NULL, lease_until = NULL,
            error_code = %(error)s,
            request_version = request_version + %(version_step)s,
            attempts = CASE WHEN %(promote)s THEN 0 ELSE attempts END,
            trigger_kind = CASE WHEN %(promote)s THEN 'auto' ELSE trigger_kind END,
            followup_at = CASE WHEN %(promote)s THEN NULL ELSE followup_at END
        WHERE space_id = %(space)s
        """,
        {
            "space": space_id,
            "status": "queued" if queued else "failed",
            "next": followup_at if promote else None,
            "delay": retry_delay,
            "error": None if queued else code,
            "promote": promote,
            "version_step": int(promote),
        },
    )
