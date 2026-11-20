"""保存事实快照、现行引用和变更记录。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ...domain.fact import Fact
from ...domain.ids import FactId
from ...ports.storage import CutoffTrigger, FactChange, SupportGroups
from .rows import fact_to_row
from .search import lexical_terms

SELECT_FACTS = sql.SQL("""
    SELECT f.space_id, f.fact_id, f.subject_id, f.predicate, f.object_entity_id,
           f.object_value, f.object_kind, f.text, f.valid_at, f.invalid_at,
           f.recorded_at, f.expired_at, f.labels, f.sensitive, f.source,
           ARRAY(
               SELECT p.message_id FROM mnemotrace_next.fact_provenance AS p
               WHERE p.space_id = f.space_id AND p.fact_id = f.fact_id
               ORDER BY p.position
           ) AS provenance
    FROM mnemotrace_next.facts AS f
    """)

_INSERT_FACT = """
    INSERT INTO mnemotrace_next.facts
        (space_id, fact_id, subject_id, predicate, object_entity_id, object_value,
         object_kind, text, valid_at, invalid_at, recorded_at, expired_at, labels,
         sensitive, source, lexemes)
    VALUES (%(space_id)s, %(fact_id)s, %(subject_id)s, %(predicate)s,
            %(object_entity_id)s, %(object_value)s, %(object_kind)s, %(text)s,
            %(valid_at)s, %(invalid_at)s, %(recorded_at)s, %(expired_at)s,
            %(labels)s, %(sensitive)s, %(source)s,
            to_tsvector('pg_catalog.simple', %(lexemes)s))
    """

_UPDATE_FACT = """
    UPDATE mnemotrace_next.facts
    SET invalid_at = %(invalid_at)s, expired_at = %(expired_at)s,
        labels = %(labels)s, sensitive = %(sensitive)s
    WHERE space_id = %(space_id)s AND fact_id = %(fact_id)s
    """


@dataclass(frozen=True)
class FactWrite:
    """保存适配器已验证的事实快照、待插入证据组和截止依据。

    append_at 是追加证据组的起始组号和位置。为 None 时从零分配，
    并在修改旧事实的分组前替换旧证据关系。
    """

    before: Fact | None
    after: Fact
    groups: SupportGroups | None
    triggers: tuple[CutoffTrigger, ...]
    append_at: tuple[int, int] | None = None


def _fact_row_changes(before: Fact, after: Fact) -> bool:
    return (
        before.invalid_at != after.invalid_at
        or before.expired_at != after.expired_at
        or before.labels != after.labels
        or before.sensitive != after.sensitive
    )


def validate_fact_update(before: Fact, after: Fact) -> None:
    """校验同一标识的事实更新。

    Args:
        before: 已保存的事实快照。
        after: 待保存的事实快照。

    Raises:
        ValueError: 更新违反有效截止约束或改变了固定字段。
    """
    expected = before
    if after.invalid_at != before.invalid_at:
        if after.invalid_at is None:
            raise ValueError("已截止的事实不能重开")
        if before.invalid_at is None:
            assert after.expired_at is not None
            expected = before.close(after.invalid_at, after.expired_at)
        else:
            expected = before.shorten_validity(after.invalid_at)
    expected = replace(
        expected,
        labels=after.labels,
        sensitive=after.sensitive,
        provenance=after.provenance,
    )
    if expected != after:
        raise ValueError("同标识的事实不能改变固定字段或首次获知时刻")


def validate_cutoff_triggers(
    before: Fact | None,
    after: Fact,
    triggers: tuple[CutoffTrigger, ...],
    causes: Mapping[FactId, Fact],
    *,
    existing_causes: Mapping[FactId, Fact],
) -> None:
    """核对实际改变截止点的触发顺序及来源。

    Args:
        before: 保存前的事实，新事实为 None。
        after: 保存后的事实。
        triggers: 按实际裁决顺序排列的触发项。
        causes: 可引用的触发事实，以本批保存后的快照为准。
        existing_causes: 已有触发事实在本批保存前的快照。

    Raises:
        ValueError: 本次未设置或提前截止，或触发事实及来源无效。
    """
    if not triggers:
        return
    if after.invalid_at is None or triggers[-1].valid_end != after.invalid_at:
        raise ValueError("最后一个触发时点须等于事实的最终截止")
    previous_end = before.invalid_at if before is not None else None
    for trigger in triggers:
        if previous_end is not None and trigger.valid_end >= previous_end:
            raise ValueError("触发时点须依次设置或提前截止")
        cause = causes.get(trigger.fact_id)
        if cause is None or cause.id == after.id:
            raise ValueError("截止须引用绑定空间内另一条已存在或同批保存的事实")
        if trigger.valid_end != cause.valid_at:
            raise ValueError("触发时点须等于触发事实的生效点")
        sources = trigger.source_message_ids
        cause_before = existing_causes.get(cause.id, cause)
        if (
            sources is None
            or (not sources and cause.provenance and cause_before.provenance)
            or not set(sources).issubset(cause.provenance)
        ):
            raise ValueError("截止触发来源须是本次裁决采用的触发事实原文")
        previous_end = trigger.valid_end


async def write_fact_batch(
    conn: AsyncConnection[TupleRow],
    changes: Sequence[FactWrite],
    changed_at: float,
) -> tuple[FactChange, ...]:
    """写入已校验的事实变化及其来源和历史记录。

    Args:
        conn: 已取得空间写入权的事务连接。
        changes: 变更前后快照及来源和触发信息。
        changed_at: 保存到变化记录的 UTC epoch 秒。

    Returns:
        按变更游标升序排列的已写入记录。

    Raises:
        ValueError: 事实引用违反外键约束，整个批次回滚。
    """
    if not changes:
        return ()
    created = [change.after for change in changes if change.before is None]
    updated = [
        change.after
        for change in changes
        if change.before is not None and _fact_row_changes(change.before, change.after)
    ]
    relinked = [
        change.after
        for change in changes
        if change.before is not None
        and change.groups is not None
        and change.append_at is None
    ]
    try:
        async with conn.transaction(), conn.cursor() as cursor:
            if created:
                await cursor.executemany(
                    _INSERT_FACT,
                    [
                        {
                            **fact_to_row(fact),
                            "lexemes": " ".join(lexical_terms(fact.text)),
                        }
                        for fact in created
                    ],
                )
            if updated:
                await cursor.executemany(
                    _UPDATE_FACT, [fact_to_row(fact) for fact in updated]
                )
            if relinked:
                await cursor.execute(
                    """
                    DELETE FROM mnemotrace_next.fact_provenance
                    WHERE space_id = %s AND fact_id = ANY(%s::text[])
                    """,
                    (relinked[0].space_id.value, [fact.id.value for fact in relinked]),
                )
            support_rows = [
                row
                for change in changes
                if change.groups is not None
                for row in _support_rows(
                    change.after, change.groups, start=change.append_at or (0, 0)
                )
            ]
            if support_rows:
                await cursor.executemany(
                    """
                    INSERT INTO mnemotrace_next.fact_provenance
                        (space_id, fact_id, position, support_group, message_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    support_rows,
                )
            await cursor.executemany(
                """
                INSERT INTO mnemotrace_next.fact_changes
                    (space_id, fact_id, changed_at, before, after, source_message_ids)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING cursor
                """,
                [_change_row(change, changed_at) for change in changes],
                returning=True,
            )
            records: list[FactChange] = []
            for change in changes:
                row = await cursor.fetchone()
                assert row is not None
                records.append(
                    FactChange(
                        row[0], changed_at, change.before, change.after, change.triggers
                    )
                )
                cursor.nextset()
            triggers = [
                (
                    record.after.space_id.value,
                    record.after.id.value,
                    record.cursor,
                    position,
                    trigger.fact_id.value,
                    trigger.valid_end,
                    [source.value for source in trigger.source_message_ids or ()],
                )
                for record in records
                for position, trigger in enumerate(record.cutoff_triggers)
            ]
            if triggers:
                await cursor.executemany(
                    """
                    INSERT INTO mnemotrace_next.fact_change_triggers
                        (space_id, fact_id, cursor, position, trigger_fact_id, valid_end,
                         source_message_ids)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    triggers,
                )
    except errors.ForeignKeyViolation:
        raise ValueError(
            "事实引用的实体、来源及截止触发事实须在绑定空间内存在"
        ) from None
    return tuple(records)


def _support_rows(
    fact: Fact, groups: SupportGroups, *, start: tuple[int, int]
) -> list[tuple[str, str, int, int, str]]:
    first_group, first_position = start
    members = (
        (group_number, message_id)
        for group_number, group in enumerate(groups, start=first_group)
        for message_id in group
    )
    return [
        (fact.space_id.value, fact.id.value, position, group_number, message_id.value)
        for position, (group_number, message_id) in enumerate(
            members, start=first_position
        )
    ]


def _change_row(
    change: FactWrite, changed_at: float
) -> tuple[str, str, float, Jsonb | None, Jsonb, list[str]]:
    previous = fact_to_row(change.before) if change.before is not None else None
    current = fact_to_row(change.after)
    sources = dict.fromkeys(current["provenance"])
    if previous is not None:
        sources.update(dict.fromkeys(previous["provenance"]))
    for trigger in change.triggers:
        sources.update(
            dict.fromkeys(item.value for item in trigger.source_message_ids or ())
        )
    return (
        change.after.space_id.value,
        change.after.id.value,
        changed_at,
        Jsonb(previous) if previous is not None else None,
        Jsonb(current),
        list(sources),
    )
