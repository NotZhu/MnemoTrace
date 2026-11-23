"""实现 PostgreSQL 会话摘要队列和受租约保护的发布。"""

from datetime import timedelta
from typing import cast
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...application.operations import LEASE_SECONDS, MAX_ATTEMPTS, LeaseLost
from ...application.session_summary_refresh import ClaimedSessionSummary
from ...domain.ids import SessionId, SpaceId
from ...ports.storage import SpaceReader, SpaceSession
from .reader import PostgresReader
from .session_summary_schedule import TABLE as _TABLE

_ACTIVE_LEASE = """
    space_id = %(space)s AND session_id = %(session)s AND status = 'running'
    AND lease_token = %(token)s AND lease_until > clock_timestamp()
    AND covered_through_seq IS NOT DISTINCT FROM %(covered)s::bigint
    AND EXISTS (
        SELECT 1 FROM mnemotrace.spaces AS space
        WHERE space.space_id = %(space)s AND space.session_summary_enabled
    )
    """


def _lease(job: ClaimedSessionSummary) -> dict[str, str | int | None]:
    return {
        "space": job.space_id.value,
        "session": job.session_id.value,
        "covered": job.covered_through_seq,
        "token": job.lease_token,
    }


class PostgresSessionSummaryRefresh:
    """持久领取会话摘要，并按覆盖游标和租约更新当前版本。"""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[TupleRow]]) -> None:
        self._pool = pool

    async def claim(self) -> ClaimedSessionSummary | None:
        """领取一项就绪摘要，或结算已耗尽尝试次数的租约。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT job.space_id, job.session_id, space.owner_id, job.covered_through_seq,
                    COALESCE(job.target_seq, job.eligible_through_seq), job.attempts
                FROM {_TABLE} AS job JOIN mnemotrace.spaces AS space USING (space_id)
                WHERE job.status IN ('queued', 'running') AND space.session_summary_enabled
                  AND COALESCE(job.available_at, job.lease_until) <= statement_timestamp()
                ORDER BY COALESCE(job.available_at, job.lease_until), job.space_id, job.session_id
                LIMIT 1 FOR UPDATE OF job SKIP LOCKED
                """
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            space, session, owner, covered, target, attempts = row
            if attempts >= MAX_ATTEMPTS:
                await conn.execute(
                    f"""
                    UPDATE {_TABLE} SET status = 'failed', error_code = 'worker_lease_expired',
                        target_seq = NULL, followup_at = NULL, available_at = NULL,
                        lease_token = NULL, lease_until = NULL
                    WHERE space_id = %s AND session_id = %s
                    """,
                    (space, session),
                )
                return None
            token = uuid4().hex
            await conn.execute(
                f"""
                UPDATE {_TABLE} SET target_seq = %s, status = 'running', available_at = NULL,
                    attempts = attempts + 1, lease_token = %s, lease_until = clock_timestamp() + %s
                WHERE space_id = %s AND session_id = %s
                """,
                (target, token, timedelta(seconds=LEASE_SECONDS), space, session),
            )
        return ClaimedSessionSummary(
            SpaceId(space),
            SessionId(session),
            owner,
            covered,
            target,
            attempts + 1,
            token,
        )

    async def read_previous(
        self, job: ClaimedSessionSummary, reader: SpaceReader
    ) -> str | None:
        """在已授权快照内读取上版正文，领取失效时终止本次尝试。"""
        conn = cast(PostgresReader, reader)._conn
        cursor = await conn.execute(
            f"SELECT summary_text FROM {_TABLE} WHERE {_ACTIVE_LEASE}", _lease(job)
        )
        row = await cursor.fetchone()
        if row is None:
            raise LeaseLost
        summary: str | None = row[0]
        return summary

    async def has_active_lease(self, job: ClaimedSessionSummary) -> bool:
        """查询最新状态，判断提交确认丢失后是否仍持有原租约。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE {_ACTIVE_LEASE}", _lease(job)
            )
            return await cursor.fetchone() is not None

    async def complete(
        self,
        job: ClaimedSessionSummary,
        writer: SpaceSession,
        through: int,
        summary: str,
    ) -> None:
        """在空间写事务内原子提交正文、进度及下一批状态。"""
        conn = cast(PostgresReader, writer)._conn
        cursor = await conn.execute(
            f"""
            UPDATE {_TABLE} SET summary_text = %(summary)s, updated_at = clock_timestamp(),
                covered_through_seq = %(through)s,
                status = CASE WHEN %(through)s < target_seq OR followup_at IS NOT NULL
                    THEN 'queued' ELSE 'idle' END,
                available_at = CASE WHEN %(through)s < target_seq
                    THEN clock_timestamp() ELSE followup_at END,
                target_seq = CASE WHEN %(through)s < target_seq THEN target_seq END,
                followup_at = CASE WHEN %(through)s < target_seq THEN followup_at END,
                attempts = 0, lease_token = NULL, lease_until = NULL
            WHERE {_ACTIVE_LEASE}
                AND %(through)s > COALESCE(covered_through_seq, 0) AND %(through)s <= target_seq
            """,
            {**_lease(job), "through": through, "summary": summary},
        )
        if cursor.rowcount != 1:
            raise LeaseLost

    async def record_attempt_failure(
        self, job: ClaimedSessionSummary, code: str, retry_delay: timedelta | None
    ) -> None:
        """记录失败并按原目标退避，终态等待该会话下一次追加。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {_TABLE} SET
                    status = CASE WHEN %(delay)s::interval IS NULL THEN 'failed' ELSE 'queued' END,
                    available_at = clock_timestamp() + %(delay)s::interval,
                    target_seq = CASE WHEN %(delay)s::interval IS NOT NULL THEN target_seq END,
                    followup_at = CASE WHEN %(delay)s::interval IS NOT NULL THEN followup_at END,
                    lease_token = NULL, lease_until = NULL,
                    error_code = CASE WHEN %(delay)s::interval IS NULL THEN %(code)s END
                WHERE {_ACTIVE_LEASE}
                """,
                {**_lease(job), "code": code, "delay": retry_delay},
            )
