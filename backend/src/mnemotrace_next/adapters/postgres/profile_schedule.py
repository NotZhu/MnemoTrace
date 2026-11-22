"""维护与空间事实、开关在同一事务内提交的画像调度。"""

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from ...domain.ids import SpaceId

TABLE = "mnemotrace_next.profile_refreshes"
AUTO_REFRESH_SECONDS = 60


async def schedule_auto_refresh(
    conn: AsyncConnection[TupleRow], space_id: SpaceId
) -> None:
    """持有空间写锁时登记一批相关变化，保留已有等待期限。"""
    await conn.execute(
        f"""
        INSERT INTO {TABLE} AS current (space_id, trigger_kind, auto_started)
        SELECT space_id, 'auto', true FROM mnemotrace_next.spaces
        WHERE space_id = %(space)s AND profile_auto_refresh
        ON CONFLICT (space_id) DO UPDATE SET
            auto_started = true,
            followup_at = CASE WHEN current.status = 'running' THEN
                COALESCE(current.followup_at, CASE WHEN current.auto_started
                    THEN statement_timestamp() + %(window)s * interval '1 second'
                    ELSE statement_timestamp() END)
                ELSE current.followup_at END,
            request_version = current.request_version
                + CASE WHEN current.status IN ('queued', 'running') THEN 0 ELSE 1 END,
            trigger_kind = CASE WHEN current.status IN ('queued', 'running')
                THEN current.trigger_kind ELSE 'auto' END,
            attempts = CASE WHEN current.status IN ('queued', 'running')
                THEN current.attempts ELSE 0 END,
            available_at = CASE
                WHEN current.status IN ('queued', 'running') THEN current.available_at
                WHEN current.auto_started
                    THEN statement_timestamp() + %(window)s * interval '1 second'
                ELSE statement_timestamp() END,
            status = CASE WHEN current.status = 'running' THEN 'running' ELSE 'queued' END,
            error_code = NULL
        WHERE current.status <> 'queued' OR NOT current.auto_started
        """,
        {"space": space_id.value, "window": AUTO_REFRESH_SECONDS},
    )


async def cancel_auto_refresh(
    conn: AsyncConnection[TupleRow], space_id: SpaceId
) -> None:
    """持有空间写锁时取消自动任务，保留手动任务及成功游标。"""
    params = (space_id.value,)
    await conn.execute(
        f"""
        UPDATE {TABLE} SET status = 'idle', available_at = NULL, attempts = 0,
            lease_token = NULL, lease_until = NULL, error_code = NULL,
            auto_started = false, followup_at = NULL
        WHERE space_id = %s AND trigger_kind = 'auto' AND status IN ('queued', 'running')
        """,
        params,
    )
    await conn.execute(
        f"""
        UPDATE {TABLE} SET auto_started = false, followup_at = NULL
        WHERE space_id = %s AND (auto_started OR followup_at IS NOT NULL)
        """,
        params,
    )
