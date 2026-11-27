"""维护与事实事务一同提交的画像分组和调度。"""

from collections.abc import Collection, Mapping
from typing import cast

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow

from ...domain.entity import Entity
from ...domain.enums import ProfileGroup
from ...domain.ids import SpaceId

TABLE = "mnemotrace.profile_refreshes"
GROUP_TABLE = "mnemotrace.profile_group_refreshes"
AUTO_REFRESH_SECONDS = 60


async def _sync_next_transition(
    conn: AsyncConnection[TupleRow], space: SpaceId
) -> None:
    await conn.execute(
        f"""
        WITH earliest AS (
            SELECT min(transition_at) AS transition_at FROM {GROUP_TABLE}
            WHERE space_id = %(space)s
        )
        UPDATE mnemotrace.spaces AS s
        SET profile_next_transition_at = earliest.transition_at
        FROM earliest
        WHERE s.space_id = %(space)s
          AND s.profile_next_transition_at IS DISTINCT FROM earliest.transition_at
        """,
        {"space": space.value},
    )


def profile_fact_scope(group: ProfileGroup) -> sql.Composed:
    """绑定空间和主体，使用固定分组谓词寻址 PostgreSQL 部分索引。"""
    return sql.SQL("""
        space_id = %(space)s AND subject_id = %(subject)s
          AND source = 'extracted' AND NOT sensitive
          AND profile_groups @> ARRAY[{}]::text[]
        """).format(sql.Literal(group.value))


async def unprocessed_profile_expiry_groups(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    expiries: Collection[tuple[frozenset[ProfileGroup], float]],
) -> set[ProfileGroup]:
    """持空间写锁查询已到期事实涉及的分组是否仍需处理。"""
    if not expiries:
        return set()
    cursor = await conn.execute(
        f"""
        SELECT group_name, transition_at FROM {GROUP_TABLE}
        WHERE space_id = %s AND transition_at IS NOT NULL
        """,
        (space.value,),
    )
    transitions: dict[ProfileGroup, float] = {
        ProfileGroup(name): at for name, at in await cursor.fetchall()
    }
    # 各组登记的是最早的时间边界。任一组尚未越过截止点，就一并登记共享该事实的组。
    return {
        affected
        for groups, invalid_at in expiries
        if any(
            group in transitions and transitions[group] <= invalid_at
            for group in groups
        )
        for affected in groups
    }


async def mark_profile_groups(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    groups: Collection[ProfileGroup],
) -> None:
    """推进受影响组的目标版本。"""
    if not groups:
        return
    await conn.execute(
        f"""
        INSERT INTO {GROUP_TABLE} AS current (space_id, group_name, target_revision)
        SELECT %s, unnest(%s::text[]), 1
        ON CONFLICT (space_id, group_name) DO UPDATE
        SET target_revision = current.target_revision + 1
        """,
        (space.value, sorted({group.value for group in groups})),
    )


async def register_profile_transitions(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    transitions: Mapping[ProfileGroup, float],
) -> None:
    """持空间写锁登记组边界，保留尚未处理的唤醒。"""
    if not transitions:
        return
    async with conn.cursor() as cursor:
        await cursor.executemany(
            f"""
            INSERT INTO {GROUP_TABLE} AS current (space_id, group_name, transition_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (space_id, group_name) DO UPDATE SET
                transition_at = LEAST(current.transition_at, EXCLUDED.transition_at),
                target_revision = current.target_revision
                    + CASE WHEN current.target_revision > current.published_revision THEN 1 ELSE 0 END
            """,
            [
                (space.value, group.value, at)
                for group, at in sorted(transitions.items())
            ],
        )
    await _sync_next_transition(conn, space)


async def next_profile_transition(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    group: ProfileGroup,
    after: float,
) -> float | None:
    """读取该组下一个可能改变有效证据集合的时刻。"""
    scope = profile_fact_scope(group)
    cursor = await conn.execute(
        sql.SQL("""
            SELECT min(at) FROM (
                (SELECT valid_at AS at FROM mnemotrace.facts
                    WHERE {scope} AND valid_at > %(after)s ORDER BY valid_at LIMIT 1)
                UNION ALL
                (SELECT invalid_at AS at FROM mnemotrace.facts
                    WHERE {scope} AND invalid_at > %(after)s ORDER BY invalid_at LIMIT 1)
            ) AS transitions
            """).format(scope=scope),
        {
            "space": space.value,
            "subject": Entity.space_user(space).id.value,
            "after": after,
        },
    )
    row = await cursor.fetchone()
    assert row is not None
    return cast(float | None, row[0])


async def advance_due_profile_transitions(
    conn: AsyncConnection[TupleRow], space: SpaceId, at: float
) -> None:
    """持空间写锁推进到期边界，并为实际发生的证据变化入队。"""
    cursor = await conn.execute(
        f"""
        SELECT group_name, transition_at FROM {GROUP_TABLE}
        WHERE space_id = %s AND transition_at <= %s
        """,
        (space.value, at),
    )
    due = {ProfileGroup(name): since for name, since in await cursor.fetchall()}
    if not due:
        return
    changes = await _due_groups(conn, space, due, at)
    for group in due:
        boundary = await next_profile_transition(conn, space, group, at)
        await conn.execute(
            f"""
            UPDATE {GROUP_TABLE} SET transition_at = %s
            WHERE space_id = %s AND group_name = %s
            """,
            (boundary, space.value, group.value),
        )
    await _sync_next_transition(conn, space)
    if changes:
        await mark_profile_groups(conn, space, changes)
        await schedule_auto_refresh(conn, space)


async def _due_groups(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    due: Mapping[ProfileGroup, float],
    at: float,
) -> set[ProfileGroup]:
    changes: set[ProfileGroup] = set()
    for group, since in due.items():
        cursor = await conn.execute(
            sql.SQL("""
                SELECT EXISTS (
                    SELECT 1 FROM mnemotrace.facts WHERE {scope}
                      AND valid_at BETWEEN %(since)s AND %(at)s
                    UNION ALL
                    SELECT 1 FROM mnemotrace.facts WHERE {scope}
                      AND invalid_at BETWEEN %(since)s AND %(at)s
                )
                """).format(scope=profile_fact_scope(group)),
            {
                "space": space.value,
                "subject": Entity.space_user(space).id.value,
                "since": since,
                "at": at,
            },
        )
        row = await cursor.fetchone()
        assert row is not None
        if row[0]:
            changes.add(group)
    return changes


async def publish_profile_group_revisions(
    conn: AsyncConnection[TupleRow],
    space: SpaceId,
    transitions: Mapping[ProfileGroup, float | None],
) -> None:
    """持空间写锁结算已发布的组版本及其后续边界。"""
    if not transitions:
        return
    async with conn.cursor() as cursor:
        await cursor.executemany(
            f"""
            UPDATE {GROUP_TABLE} SET
                published_revision = target_revision, transition_at = %s
            WHERE space_id = %s AND group_name = %s
            """,
            [
                (transition, space.value, group.value)
                for group, transition in transitions.items()
            ],
        )
    await _sync_next_transition(conn, space)


async def clear_profile_schedule(
    conn: AsyncConnection[TupleRow], space: SpaceId
) -> None:
    """持空间写锁清除刷新任务和全部分组进度。"""
    params = (space.value,)
    await conn.execute(f"DELETE FROM {TABLE} WHERE space_id = %s", params)
    await conn.execute(f"DELETE FROM {GROUP_TABLE} WHERE space_id = %s", params)
    await _sync_next_transition(conn, space)


async def schedule_pending_profile(
    conn: AsyncConnection[TupleRow], space: SpaceId
) -> None:
    """在开启自动刷新时安排已经累积的分组变化。"""
    cursor = await conn.execute(
        f"""
        SELECT 1 FROM {GROUP_TABLE} WHERE space_id = %s
          AND target_revision > published_revision LIMIT 1
        """,
        (space.value,),
    )
    if await cursor.fetchone() is not None:
        await schedule_auto_refresh(conn, space)


async def schedule_auto_refresh(
    conn: AsyncConnection[TupleRow], space_id: SpaceId
) -> None:
    """持有空间写锁时登记一批相关变化，保留已有等待期限。"""
    await conn.execute(
        f"""
        INSERT INTO {TABLE} AS current (space_id, trigger_kind, auto_started)
        SELECT space_id, 'auto', true FROM mnemotrace.spaces
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
    """持有空间写锁时取消自动任务，保留手动任务及已发布版本。"""
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
