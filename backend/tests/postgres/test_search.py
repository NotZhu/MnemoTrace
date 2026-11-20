"""验证双路检索、模型维度和事实索引的事务一致性。"""

from dataclasses import replace
from math import inf, nan, nextafter

import pytest
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres import search as postgres_search
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.values import Label
from mnemotrace_next.ports.queries import FactFilter

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_vector_top_k_filters_scope_before_ranking(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """更近的不可见向量不占候选名额，同分稳定按 ID 排序。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            subject = Entity(scope.id, "甲")
            foreign_subject = Entity(sibling.id, "乙")
            foreign_fact = fact(foreign_subject, "foreign")
            async with storage.session(sibling.id, write=True) as writer:
                await writer.upsert_entity(foreign_subject)
                await writer.save_facts((foreign_fact,), changed_at=15.0)
                await writer.upsert_vectors(
                    ((foreign_fact.id, (1.0, 0.0, 0.0)),), embedding_space="v1"
                )
            source_a, source_b = message(scope, "sa"), message(scope, "sb")
            label = Label("food")
            a = replace(
                fact(subject, "a", provenance=(source_a.id, source_b.id)),
                valid_at=10.0,
                labels=(label,),
            )
            b = replace(a, id=FactId("b"))
            unrelated = tuple(fact(subject, f"outside-{i}") for i in range(30))
            secret = replace(a, id=FactId("secret"), sensitive=True)
            closed = replace(b, id=FactId("closed")).close(11.0, 15.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                for session_id in {source_a.session_id, source_b.session_id}:
                    await writer.register_session(session_id)
                await writer.append_messages((source_a, source_b))
                await writer.save_facts(
                    (b, a, secret, closed, *unrelated), changed_at=15.0
                )
                await writer.upsert_vectors(
                    (
                        (b.id, (4.0, 3.0)),
                        (a.id, (1.0, 0.0)),
                        (a.id, (4.0, 3.0)),
                        *(
                            (item.id, (1.0, 0.0))
                            for item in (*unrelated, secret, closed)
                        ),
                    ),
                    embedding_space="v1",
                )
                await writer.upsert_vectors(
                    ((a.id, (1e300, 0.0, 1e300)),), embedding_space="v2"
                )
                for invalid in (
                    (0.0, 0.0),
                    (nan, 0.0),
                    (1.0, inf),
                    (1.0, 0.0, 0.0),
                    (),
                ):
                    with pytest.raises(ValueError):
                        await writer.upsert_vectors(
                            ((a.id, invalid),), embedding_space="v1"
                        )
                with pytest.raises(ValueError):
                    await writer.upsert_vectors(
                        ((a.id, (0.0, 1.0)), (foreign_fact.id, (1.0, 0.0))),
                        embedding_space="invalid-batch",
                    )
            lookups: list[tuple[SpaceId, str]] = []
            embedding_dim = postgres_search.embedding_dim

            async def count_lookup(
                conn: AsyncConnection[TupleRow], space_id: SpaceId, embedding_space: str
            ) -> int | None:
                lookups.append((space_id, embedding_space))
                return await embedding_dim(conn, space_id, embedding_space)

            monkeypatch.setattr(postgres_search, "embedding_dim", count_lookup)
            async with storage.session(scope.id) as reader:
                filters = FactFilter(
                    subject=a.subject,
                    predicate=a.predicate,
                    labels=(label,),
                    provenance=(source_b.id, source_a.id),
                    exclude_sensitive=True,
                )
                hits = await reader.search_vectors(
                    (1.0, 0.0),
                    2,
                    filters=filters,
                    reference_time=20.0,
                    embedding_space="v1",
                )
                assert [item for item, _ in hits] == [a.id, b.id]
                assert [score for _, score in hits] == pytest.approx([0.8, 0.8])
                threshold = hits[0][1]
                assert (
                    await reader.search_vectors(
                        (1.0, 0.0),
                        1,
                        filters=filters,
                        reference_time=20.0,
                        embedding_space="v1",
                        min_score=threshold,
                    )
                    == hits[:1]
                )
                assert (
                    await reader.search_vectors(
                        (1.0, 0.0),
                        2,
                        filters=filters,
                        reference_time=20.0,
                        embedding_space="v1",
                        min_score=nextafter(threshold, inf),
                    )
                    == ()
                )
                (second_model,) = await reader.search_vectors(
                    (1e-300, 0.0, 1e-300),
                    2,
                    filters=filters,
                    reference_time=20.0,
                    embedding_space="v2",
                )
                assert second_model[0] == a.id
                assert second_model[1] == pytest.approx(1.0)
                assert (
                    await reader.search_vectors(
                        (1.0,),
                        2,
                        filters=filters,
                        reference_time=20.0,
                        embedding_space="invalid-batch",
                    )
                    == ()
                )
                for invalid_query in ((1.0,), (0.0, 0.0), (nan, 0.0)):
                    with pytest.raises(ValueError):
                        await reader.search_vectors(
                            invalid_query,
                            2,
                            filters=filters,
                            reference_time=20.0,
                            embedding_space="v1",
                        )
                assert await reader.search_vectors(
                    (1.0, 0.0),
                    2,
                    filters=FactFilter(state="history"),
                    reference_time=20.0,
                    embedding_space="v1",
                ) == ((closed.id, 1.0),)
            assert lookups == [
                (scope.id, "v1"),
                (scope.id, "v2"),
                (scope.id, "invalid-batch"),
            ]

    run(exercise())


def test_vector_dimension_limit_applies_before_registration(
    database: PgDatabase,
) -> None:
    """读写共用 pgvector 的维度上限，边界维度仍可写入和检索。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            item = fact(subject, "vector-limit")
            at_limit = (1.0,) + (0.0,) * 15_999
            too_large = (*at_limit, 0.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((item,), changed_at=20.0)
                with pytest.raises(ValueError, match="16000"):
                    await writer.upsert_vectors(
                        ((item.id, too_large),), embedding_space="limit"
                    )
                await writer.upsert_vectors(
                    ((item.id, at_limit),), embedding_space="limit"
                )
            async with storage.session(scope.id) as reader:
                with pytest.raises(ValueError, match="16000"):
                    await reader.search_vectors(
                        too_large,
                        1,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space="unregistered",
                    )
                assert await reader.search_vectors(
                    at_limit,
                    1,
                    filters=FactFilter(),
                    reference_time=20.0,
                    embedding_space="limit",
                ) == ((item.id, 1.0),)

    run(exercise())


def test_lexical_search_uses_fact_text_and_same_filter_semantics(
    database: PgDatabase,
) -> None:
    """C locale 下仍支持中英词项、阈值和过滤顺序。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            a = fact(subject, "a", text="喝乌龙 AZ938")
            b = replace(a, id=FactId("b"))
            hidden = replace(a, id=FactId("0"), sensitive=True)
            different_text = fact(subject, "other", text="软件质量")
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((b, a, hidden, different_text), changed_at=20.0)
            async with storage.session(scope.id) as reader:
                filters = FactFilter(exclude_sensitive=True)
                for query in ("乌龙", "乌龙、ＡＺ９３８", "龙", "az938"):
                    hits = await reader.search_lexical(
                        query, 2, filters=filters, reference_time=20.0
                    )
                    assert [item for item, _ in hits] == [a.id, b.id]
                    threshold = hits[0][1]
                    assert (
                        await reader.search_lexical(
                            query,
                            1,
                            filters=filters,
                            reference_time=20.0,
                            min_score=threshold,
                        )
                        == hits[:1]
                    )
                    assert (
                        await reader.search_lexical(
                            query,
                            2,
                            filters=filters,
                            reference_time=20.0,
                            min_score=nextafter(threshold, inf),
                        )
                        == ()
                    )
                assert (
                    await reader.search_lexical(
                        "|&！",
                        3,
                        filters=filters,
                        reference_time=20.0,
                    )
                    == ()
                )

    run(exercise())


def test_dual_search_and_fact_load_share_snapshot_with_concurrent_writer(
    database: PgDatabase,
) -> None:
    """两路检索和候选加载都看到授权读事务开始时的事实。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            old, new = fact(subject, "old"), fact(subject, "new")
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((old,), changed_at=20.0)
                await writer.upsert_vectors(((old.id, (1.0,)),), embedding_space="v1")
            filters = FactFilter(exclude_sensitive=True)
            async with storage.session(scope.id) as snapshot:
                assert await snapshot.search_vectors(
                    (1.0,),
                    3,
                    filters=filters,
                    reference_time=20.0,
                    embedding_space="v1",
                ) == ((old.id, 1.0),)
                async with storage.session(scope.id, write=True) as writer:
                    await writer.save_facts(
                        (replace(old, sensitive=True), new), changed_at=30.0
                    )
                    await writer.upsert_vectors(
                        ((new.id, (1.0,)),), embedding_space="v1"
                    )
                hits = await snapshot.search_lexical(
                    "乌龙", 3, filters=filters, reference_time=20.0
                )
                assert [item for item, _ in hits] == [old.id]
                assert await snapshot.get_facts(
                    (old.id, new.id), exclude_sensitive=True
                ) == {
                    old.id: old,
                }
            async with storage.session(scope.id) as fresh:
                assert await fresh.search_vectors(
                    (1.0,),
                    3,
                    filters=filters,
                    reference_time=20.0,
                    embedding_space="v1",
                ) == ((new.id, 1.0),)
                hits = await fresh.search_lexical(
                    "乌龙", 3, filters=filters, reference_time=20.0
                )
                assert [item for item, _ in hits] == [new.id]

    run(exercise())


@pytest.mark.parametrize(
    "vector_failure", (False, True), ids=("after-index", "vector-error")
)
def test_fact_and_both_indexes_roll_back_together(
    database: PgDatabase, vector_failure: bool
) -> None:
    """发布异常一起回滚消息、事实、变更记录、嵌入登记和索引。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            source = message(scope, "unpublished-source")
            candidate = fact(subject, "unpublished", provenance=(source.id,))
            filters = FactFilter()
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(source.session_id)
            expected = ValueError if vector_failure else RuntimeError
            match = "范数" if vector_failure else "发布中断"
            with pytest.raises(expected, match=match):
                async with storage.session(scope.id, write=True) as writer:
                    await writer.append_messages((source,))
                    await writer.upsert_entity(subject)
                    await writer.save_facts((candidate,), changed_at=20.0)
                    vector = (0.0,) if vector_failure else (1.0,)
                    await writer.upsert_vectors(
                        ((candidate.id, vector),), embedding_space="v1"
                    )
                    assert await writer.search_lexical(
                        "乌龙", 1, filters=filters, reference_time=20.0
                    )
                    raise RuntimeError("发布中断")
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((source.id,)) == {}
                assert await reader.get_fact(candidate.id) is None
                assert await reader.list_fact_changes(candidate.id) == ()
                assert (
                    await reader.search_lexical(
                        "乌龙", 1, filters=filters, reference_time=20.0
                    )
                    == ()
                )
                assert (
                    await reader.search_vectors(
                        (1.0, 0.0),
                        1,
                        filters=filters,
                        reference_time=20.0,
                        embedding_space="v1",
                    )
                    == ()
                )

    run(exercise())
