"""验证事实生命周期、强引用、筛选、分页和一跳边。"""

from dataclasses import replace
from typing import cast

import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres.session import PostgresSession
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Source
from mnemotrace.domain.ids import FactId, MessageId, SessionId
from mnemotrace.domain.values import EntityRef, Label
from mnemotrace.ports.queries import FactCursor, FactFilter, FactQuery
from mnemotrace.ports.storage import CutoffTrigger

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_fact_updates_keep_fixed_fields_and_first_expiration(
    database: PgDatabase,
) -> None:
    """修改游标按写入顺序分页，幂等重放保留原索引和历史。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "小夏")
            original = message(scope, "a")
            correction = message(scope, "b")
            old = replace(
                fact(subject, "old", provenance=(original.id,)), valid_at=10.0
            )
            closed = old.close(30.0, 35.0)
            shortened = replace(
                closed.shorten_validity(25.0),
                labels=(Label("food"),),
                sensitive=True,
                provenance=(correction.id, original.id),
            )
            newer = replace(fact(subject, "new"), valid_at=25.0)
            intermediate = replace(fact(subject, "intermediate"), valid_at=28.0)
            triggers = (
                CutoffTrigger(intermediate.id, 28.0),
                CutoffTrigger(newer.id, 25.0),
            )
            batch = (shortened, newer, intermediate)
            support = {old.id: ((correction.id, original.id),)}
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.register_session(original.session_id)
                await writer.append_messages((original, correction))
                await writer.save_facts((old,), changed_at=11.0)
                await writer.save_facts((closed,), changed_at=35.0)
                with pytest.raises(ValueError, match="完整证据组"):
                    await writer.save_facts((shortened,), changed_at=45.0)
                with pytest.raises(ValueError, match="依次设置或提前"):
                    await writer.save_facts(
                        batch,
                        changed_at=45.0,
                        support_groups=support,
                        cutoff_triggers={old.id: (triggers[0], *triggers)},
                    )
                assert await writer.get_fact(old.id) == closed
                assert await writer.get_fact(intermediate.id) is None
                saved = await writer.save_facts(
                    batch,
                    changed_at=45.0,
                    support_groups=support,
                    cutoff_triggers={old.id: triggers},
                )
                assert tuple(change.after for change in saved) == batch
                assert [change.cursor for change in saved] == sorted(
                    change.cursor for change in saved
                )
                assert (
                    await writer.save_facts(
                        batch, changed_at=46.0, support_groups=support
                    )
                    == ()
                )
                for invalid in (
                    replace(shortened, text="改写正文"),
                    replace(shortened, invalid_at=26.0),
                    replace(shortened, expired_at=39.0),
                    replace(shortened, invalid_at=None, expired_at=None),
                ):
                    with pytest.raises(ValueError):
                        await writer.save_facts(
                            (fact(subject, "reject"), invalid), changed_at=47.0
                        )
                with pytest.raises(ValueError):
                    await writer.save_facts((newer, newer), changed_at=47.0)
            async with storage.session(scope.id) as reader:
                assert await reader.get_facts((old.id, FactId("reject"))) == {
                    old.id: shortened
                }
                assert await reader.get_fact(old.id, exclude_sensitive=True) is None
                (latest,) = await reader.list_fact_changes(old.id, limit=1)
                assert latest == saved[0]
                assert latest.cutoff_triggers == triggers
                remaining = await reader.list_fact_changes(old.id, after=latest.cursor)
                assert [
                    (c.before, c.after, c.changed_at) for c in (latest, *remaining)
                ] == [
                    (closed, shortened, 45.0),
                    (old, closed, 35.0),
                    (None, old, 11.0),
                ]
                assert (
                    await reader.list_fact_changes(old.id, after=remaining[-1].cursor)
                    == ()
                )
                assert await reader.get_fact(newer.id) == newer
                hits = await reader.search_lexical(
                    "乌龙",
                    5,
                    filters=FactFilter(valid_at=24.0),
                    reference_time=45.0,
                )
                assert [item for item, _ in hits] == [old.id]
                assert await reader.query_facts(
                    FactQuery(
                        FactFilter(
                            subject=old.subject, predicate=old.predicate, valid_at=25.0
                        ),
                        limit=10,
                    ),
                    reference_time=25.0,
                ) == (newer,)
            async with storage.session(scope.id, write=True) as writer:
                await writer.delete_facts((intermediate.id,))
                assert await writer.get_fact(old.id) == shortened
                assert await writer.get_fact(newer.id) == newer
                assert [
                    change.changed_at
                    for change in await writer.list_fact_changes(old.id)
                ] == [35.0, 11.0]

    run(exercise())


def test_appending_support_preserves_stored_rows_after_erasure(
    database: PgDatabase,
) -> None:
    """证据组擦除留下编号空洞后，追加只写新关系和审计。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            subject = Entity(scope.id, "小夏")
            first, third, extra = (
                message(scope, key) for key in ("first", "third", "extra")
            )
            erased = message(scope, "erased", session=SessionId("erase"))
            original = fact(subject, "old", provenance=(first.id, erased.id, third.id))
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(first.session_id)
                await writer.register_session(erased.session_id)
                await writer.append_messages((first, erased, third, extra))
                await writer.upsert_entity(subject)
                await writer.save_facts(
                    (original,),
                    changed_at=20.0,
                    support_groups={
                        original.id: ((first.id,), (erased.id,), (third.id,))
                    },
                )
                assert (
                    await writer.delete_session_data(erased.session_id) == frozenset()
                )

                conn = cast(PostgresSession, writer)._conn
                params = (scope.id.value, original.id.value)
                relations = """
                    SELECT position, support_group, message_id, ctid::text
                    FROM mnemotrace.fact_provenance
                    WHERE space_id = %s AND fact_id = %s ORDER BY position
                    """
                fact_row = """
                    SELECT ctid::text FROM mnemotrace.facts
                    WHERE space_id = %s AND fact_id = %s
                    """
                initial_relations = await (
                    await conn.execute(relations, params)
                ).fetchall()
                initial_fact_row = await (
                    await conn.execute(fact_row, params)
                ).fetchone()
                supported = replace(original, provenance=(first.id, third.id, extra.id))
                groups = (first.id,), (third.id,), (extra.id,)
                (change,) = await writer.save_facts(
                    (supported,), changed_at=30.0, support_groups={original.id: groups}
                )
                final_relations = await (
                    await conn.execute(relations, params)
                ).fetchall()
                assert final_relations[:-1] == initial_relations
                assert final_relations[-1][:3] == (3, 3, extra.id.value)
                assert (
                    await (await conn.execute(fact_row, params)).fetchone()
                    == initial_fact_row
                )
                assert change.after == supported
                assert await writer.get_fact_support_groups((original.id,)) == {
                    original.id: groups
                }

    run(exercise())


def test_strong_references_and_failed_batches_do_not_publish(
    database: PgDatabase,
) -> None:
    """实体、原文和截止触发只在绑定空间解析，批次验证失败时原子回滚。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            foreign_entity = Entity(sibling.id, "外空间")
            local_entity = Entity(scope.id, "本空间")
            source = message(sibling, "foreign")
            foreign_fact = fact(foreign_entity, "foreign")
            async with storage.session(sibling.id, write=True) as writer:
                await writer.upsert_entity(foreign_entity)
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.save_facts((foreign_fact,), changed_at=10.0)

            good = fact(local_entity, "good")
            candidate = fact(local_entity, "bad")
            local_source = message(scope, "local")
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(local_entity)
                await writer.register_session(local_source.session_id)
                await writer.append_messages((local_source,))
                await writer.save_facts((good,), changed_at=10.0)
                assert await writer.get_facts((foreign_fact.id,)) == {}
                assert await writer.list_fact_changes(foreign_fact.id) == ()
                pending = fact(local_entity, "pending")
                with pytest.raises(ValueError, match="新建手工事实"):
                    await writer.save_facts(
                        (pending, replace(candidate, provenance=(local_source.id,))),
                        changed_at=20.0,
                    )
                assert await writer.get_facts((pending.id, candidate.id)) == {}
                for invalid in (
                    replace(candidate, subject=EntityRef(foreign_entity.id)),
                    replace(candidate, object=EntityRef(foreign_entity.id)),
                    replace(
                        candidate, source=Source.EXTRACTED, provenance=(source.id,)
                    ),
                    replace(
                        candidate,
                        source=Source.EXTRACTED,
                        provenance=(MessageId("missing"),),
                    ),
                    foreign_fact,
                ):
                    with pytest.raises(ValueError):
                        await writer.save_facts((pending, invalid), changed_at=20.0)
                    assert await writer.get_fact(pending.id) is None
                with pytest.raises(ValueError, match="截止须引用"):
                    await writer.save_facts(
                        (pending, candidate.close(18.0, 20.0)),
                        changed_at=20.0,
                        cutoff_triggers={
                            candidate.id: (CutoffTrigger(foreign_fact.id, 18.0),)
                        },
                    )
                assert await writer.get_fact(pending.id) is None
                with pytest.raises(ValueError):
                    await writer.save_facts(
                        (
                            replace(good, labels=(Label("food"),)),
                            replace(
                                candidate,
                                source=Source.EXTRACTED,
                                provenance=(source.id,),
                            ),
                        ),
                        changed_at=25.0,
                    )
                assert await writer.get_fact(good.id) == good
                assert len(await writer.list_fact_changes(good.id)) == 1
            async with storage.session(scope.id) as reader:
                assert await reader.query_facts(FactQuery(), reference_time=25.0) == (
                    good,
                )

    run(exercise())


def test_fact_filters_and_keyset_paging(database: PgDatabase) -> None:
    """按查询有效时点、任一标签、任一来源和稳定游标查询。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "用户")
            origin = message(scope, "src")
            food = Label("food")
            current = replace(
                fact(subject, "b", provenance=(origin.id,)),
                valid_at=50.0,
                labels=(food,),
            )
            ended = replace(fact(subject, "a"), valid_at=5.0, labels=(food,)).close(
                50.0, 60.0
            )
            unknown_end = fact(subject, "c").close(35.0, 60.0)
            future = replace(fact(subject, "中"), valid_at=100.0, recorded_at=12.0)
            unknown = fact(subject, "ä")
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.register_session(origin.session_id)
                await writer.append_messages((origin,))
                await writer.save_facts(
                    (unknown, future, ended, current, unknown_end), changed_at=65.0
                )
            async with storage.session(scope.id) as reader:
                assert await reader.query_facts(FactQuery(), reference_time=50.0) == (
                    current,
                    unknown,
                )
                assert await reader.query_facts(
                    FactQuery(FactFilter(state="history")),
                    reference_time=50.0,
                ) == (ended, unknown_end)
                assert await reader.query_facts(
                    FactQuery(FactFilter(state="history", valid_at=50.0)),
                    reference_time=0.0,
                ) == (ended,)
                filtered = FactFilter(
                    state="all",
                    subject=current.subject,
                    predicate=current.predicate,
                    labels=(Label("music"), food),
                    provenance=(MessageId("absent"), origin.id),
                )
                assert await reader.query_facts(
                    FactQuery(filtered), reference_time=50.0
                ) == (current,)
                timeline = FactFilter(state="all")
                first = await reader.query_facts(
                    FactQuery(timeline, limit=2), reference_time=50.0
                )
                assert first == (future, ended)
                cursor = FactCursor(first[-1].recorded_at, first[-1].id)
                assert await reader.query_facts(
                    FactQuery(timeline, after=cursor),
                    reference_time=50.0,
                ) == (current, unknown_end, unknown)
                assert await reader.query_facts(
                    FactQuery(
                        FactFilter(
                            state="all",
                            subject=current.subject,
                            predicate=current.predicate,
                        ),
                        limit=10,
                    ),
                    reference_time=50.0,
                ) == (
                    future,
                    ended,
                    current,
                    unknown_end,
                    unknown,
                )

    run(exercise())


def test_edges_follow_direction_time_and_visibility(database: PgDatabase) -> None:
    """一跳只返回实体关系，自环去重，按有效起点排序。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            a, b = Entity(scope.id, "甲"), Entity(scope.id, "乙")
            outbound = replace(fact(a, "out"), object=EntityRef(b.id), valid_at=20.0)
            inbound = replace(fact(b, "in"), object=EntityRef(a.id), valid_at=20.0)
            loop = replace(fact(a, "loop"), object=EntityRef(a.id), valid_at=25.0)
            secret = replace(fact(a, "secret"), object=EntityRef(b.id), sensitive=True)
            ended = replace(fact(a, "ended"), object=EntityRef(b.id)).close(20.0, 30.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(a)
                await writer.upsert_entity(b)
                await writer.save_facts(
                    (outbound, inbound, loop, secret, ended, fact(a, "literal")),
                    changed_at=30.0,
                )
            async with storage.session(scope.id) as reader:
                assert await reader.edges(a.id, reference_time=30.0, limit=10) == (
                    loop,
                    inbound,
                    outbound,
                    secret,
                )
                assert await reader.edges(
                    a.id, reference_time=30.0, direction="in", limit=10
                ) == (
                    loop,
                    inbound,
                )
                assert await reader.edges(
                    a.id,
                    reference_time=30.0,
                    direction="out",
                    exclude_sensitive=True,
                    limit=10,
                ) == (loop, outbound)
                assert await reader.edges(
                    a.id,
                    reference_time=30.0,
                    valid_at=20.0,
                    limit=1,
                ) == (inbound,)

    run(exercise())
