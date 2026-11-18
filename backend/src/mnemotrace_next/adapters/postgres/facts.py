"""保存事实快照、现行引用和变更记录。"""

from collections.abc import Sequence
from dataclasses import replace

from psycopg import AsyncConnection, errors, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from ...domain.fact import Fact
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
           ) AS provenance,
           ARRAY(
               SELECT s.predecessor_id FROM mnemotrace_next.fact_supersedes AS s
               WHERE s.space_id = f.space_id AND s.fact_id = f.fact_id
               ORDER BY s.position
           ) AS supersedes
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


async def write_fact_batch(
    conn: AsyncConnection[TupleRow],
    changes: Sequence[tuple[Fact | None, Fact]],
    changed_at: float,
) -> None:
    """写入已校验的事实变化及其来源和历史记录。

    Args:
        conn: 已取得空间写入权的事务连接。
        changes: 变更前后快照对，创建时前快照为 None。
        changed_at: 保存到变化记录的 UTC epoch 秒。

    Raises:
        ValueError: 事实引用违反外键约束，整个批次回滚。
    """
    if not changes:
        return
    created = [after for before, after in changes if before is None]
    updated = [after for before, after in changes if before is not None]
    relinked = [
        after
        for before, after in changes
        if before and before.provenance != after.provenance
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
            await cursor.executemany(
                """
                INSERT INTO mnemotrace_next.fact_provenance
                    (space_id, fact_id, position, message_id)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (fact.space_id.value, fact.id.value, position, message_id.value)
                    for fact in (*created, *relinked)
                    for position, message_id in enumerate(fact.provenance)
                ],
            )
            await cursor.executemany(
                """
                INSERT INTO mnemotrace_next.fact_supersedes
                    (space_id, fact_id, position, predecessor_id)
                VALUES (%s, %s, %s, %s)
                """,
                [
                    (fact.space_id.value, fact.id.value, position, predecessor.value)
                    for fact in created
                    for position, predecessor in enumerate(fact.supersedes)
                ],
            )
            await cursor.executemany(
                """
                INSERT INTO mnemotrace_next.fact_changes
                    (space_id, fact_id, changed_at, before, after, source_message_ids)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [_change_row(before, after, changed_at) for before, after in changes],
            )
    except errors.ForeignKeyViolation:
        raise ValueError("事实引用的实体或来源须在绑定空间内存在") from None


def _change_row(
    before: Fact | None, after: Fact, changed_at: float
) -> tuple[str, str, float, Jsonb | None, Jsonb, list[str]]:
    previous = fact_to_row(before) if before is not None else None
    current = fact_to_row(after)
    sources = dict.fromkeys(current["provenance"])
    if previous is not None:
        sources.update(dict.fromkeys(previous["provenance"]))
    return (
        after.space_id.value,
        after.id.value,
        changed_at,
        Jsonb(previous) if previous is not None else None,
        Jsonb(current),
        list(sources),
    )
