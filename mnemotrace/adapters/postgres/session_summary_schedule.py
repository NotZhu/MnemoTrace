"""在追加原文和空间设置事务中维护滚动摘要调度。"""

from datetime import timedelta

from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from ...application.session_summary import RECENT_MESSAGE_WINDOW
from ...domain.ids import SessionId, SpaceId

TABLE = "mnemotrace.session_summaries"
_BATCH_TRIGGER = 8
_MAX_WAIT = timedelta(seconds=60)


async def schedule_summaries(
    conn: AsyncConnection[TupleRow], space_id: SpaceId, sessions: set[SessionId]
) -> None:
    """持有已启用摘要的空间写锁时，为新增原文的会话登记摘要。"""
    # 只看窗口外最近的八条，第八条仍在游标之后就足以提前触发。
    await conn.execute(
        f"""
        WITH eligible AS (
            SELECT requested.session_id, max(tail.append_seq) AS through_seq,
                CASE WHEN count(*) = %(trigger)s THEN min(tail.append_seq) END AS eighth_seq
            FROM unnest(%(sessions)s::text[]) AS requested(session_id)
            CROSS JOIN LATERAL (
                SELECT append_seq FROM mnemotrace.messages
                WHERE space_id = %(space)s AND session_id = requested.session_id
                ORDER BY append_seq DESC LIMIT %(trigger)s OFFSET %(window)s
            ) AS tail
            GROUP BY requested.session_id
        )
        INSERT INTO {TABLE} AS current
            (space_id, session_id, eligible_through_seq, status, available_at)
        SELECT %(space)s, session_id, through_seq, 'queued',
            CASE WHEN eighth_seq IS NOT NULL THEN statement_timestamp()
                ELSE statement_timestamp() + %(wait)s END
        FROM eligible
        ON CONFLICT (space_id, session_id) DO UPDATE SET
            eligible_through_seq = EXCLUDED.eligible_through_seq,
            status = CASE WHEN current.target_seq IS NULL THEN 'queued' ELSE current.status END,
            attempts = CASE WHEN current.target_seq IS NULL THEN 0 ELSE current.attempts END,
            error_code = NULL,
            (available_at, followup_at) = (
                SELECT
                    CASE WHEN current.target_seq IS NULL THEN LEAST(current.available_at, due.at)
                        ELSE current.available_at END,
                    CASE WHEN current.target_seq IS NOT NULL THEN LEAST(current.followup_at, due.at) END
                FROM (
                    SELECT CASE WHEN eligible.eighth_seq > COALESCE(
                        current.target_seq, current.covered_through_seq, 0
                    ) THEN statement_timestamp() ELSE statement_timestamp() + %(wait)s END AS at
                    FROM eligible WHERE eligible.session_id = current.session_id
                ) AS due
            )
        WHERE EXCLUDED.eligible_through_seq > COALESCE(current.target_seq, current.covered_through_seq, 0)
        """,
        {
            "space": space_id.value,
            "sessions": sorted(item.value for item in sessions),
            "trigger": _BATCH_TRIGGER,
            "window": RECENT_MESSAGE_WINDOW,
            "wait": _MAX_WAIT,
        },
    )


async def cancel_summaries(conn: AsyncConnection[TupleRow], space_id: SpaceId) -> None:
    """持有空间写锁时撤销排队任务和租约，保留最新摘要。"""
    await conn.execute(
        f"""
        UPDATE {TABLE} SET status = 'idle', available_at = NULL, attempts = 0,
            target_seq = NULL, followup_at = NULL, lease_token = NULL, lease_until = NULL
        WHERE space_id = %s AND status IN ('queued', 'running')
        """,
        (space_id.value,),
    )
