"""验证 PostgreSQL 批量邻接读取的事实筛选、隔离与截断。"""

from dataclasses import replace

import pytest

from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.values import EntityRef, Label, Predicate
from mnemotrace_next.ports.queries import FactFilter, FactQuery
from mnemotrace_next.ports.storage import GraphEdge, GraphEdgeBatch

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def _edge(identifier: str, subject: Entity, target: Entity) -> Fact:
    return replace(fact(subject, identifier), object=EntityRef(target.id))


def test_adjacent_edges_match_fact_filters_and_bound_space(
    database: PgDatabase,
) -> None:
    """同一过滤下只返回绑定空间内与任一输入端点相连的事实边。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            endpoint, incoming_from, outgoing_to, hidden = (
                Entity(scope.id, name) for name in ("查询端点", "入端", "出端", "隐私")
            )
            foreign, foreign_target = (
                Entity(sibling.id, endpoint.name),
                Entity(sibling.id, "其他"),
            )
            source_a, source_b = message(scope, "a"), message(scope, "b")
            work, family = Label("professional_details"), Label("family")
            incoming = replace(
                _edge("01-in", incoming_from, endpoint),
                valid_at=5.0,
                labels=(work,),
                provenance=(source_a.id,),
            )
            active = replace(
                _edge("02-active", endpoint, outgoing_to),
                valid_at=20.0,
                labels=(family,),
                provenance=(source_b.id,),
            )
            records = (
                incoming,
                active,
                replace(_edge("03-ended", endpoint, outgoing_to), valid_at=5.0).close(
                    20.0, 20.0
                ),
                _edge("04-ended-unknown", endpoint, outgoing_to).close(20.0, 20.0),
                _edge("05-unknown", endpoint, outgoing_to),
                replace(_edge("06-future", endpoint, outgoing_to), valid_at=21.0),
                replace(_edge("07-sensitive", endpoint, hidden), sensitive=True),
                replace(
                    _edge("08-predicate", endpoint, outgoing_to),
                    predicate=Predicate("有关"),
                ),
                _edge("09-other-endpoint", incoming_from, outgoing_to),
                _edge("10-nonadjacent", outgoing_to, hidden),
                fact(endpoint, "11-literal"),
                replace(
                    _edge("12-no-label", endpoint, outgoing_to),
                    provenance=(source_a.id,),
                ),
                replace(_edge("13-no-source", endpoint, outgoing_to), labels=(family,)),
            )
            async with storage.session(scope.id, write=True) as writer:
                for entity in (endpoint, incoming_from, outgoing_to, hidden):
                    await writer.upsert_entity(entity)
                await writer.append_messages((source_a, source_b))
                await writer.save_facts(records, changed_at=21.0)
            async with storage.session(sibling.id, write=True) as writer:
                await writer.upsert_entity(foreign)
                await writer.upsert_entity(foreign_target)
                await writer.save_facts(
                    (_edge("07-sensitive", foreign, foreign_target),), changed_at=21.0
                )

            combined = FactFilter(
                labels=(work, family),
                provenance=(source_a.id, source_b.id),
                exclude_sensitive=True,
            )
            cases = (
                FactFilter(),
                FactFilter(valid_at=20.0, exclude_sensitive=True),
                FactFilter(state="history"),
                FactFilter(state="history", valid_at=20.0),
                FactFilter(state="all"),
                FactFilter(subject=EntityRef(endpoint.id)),
                FactFilter(predicate=Predicate("有关")),
                combined,
            )
            local_ids = (endpoint.id, incoming_from.id)
            async with storage.session(scope.id) as reader:
                for filters in cases:
                    eligible = await reader.query_facts(
                        FactQuery(filters=filters), reference_time=20.0
                    )
                    expected = tuple(
                        GraphEdge(
                            item.id, item.subject.entity_id, item.object.entity_id
                        )
                        for item in sorted(eligible, key=lambda item: item.id.value)
                        if isinstance(item.object, EntityRef)
                        and (
                            item.subject.entity_id in local_ids
                            or item.object.entity_id in local_ids
                        )
                    )
                    assert await reader.adjacent_edges(
                        (*local_ids, endpoint.id, foreign.id),
                        filters=filters,
                        reference_time=20.0,
                        limit=20,
                    ) == GraphEdgeBatch(expected, False)

                combined_edges = await reader.adjacent_edges(
                    local_ids, filters=combined, reference_time=20.0, limit=20
                )
                assert tuple(edge.fact_id for edge in combined_edges.edges) == (
                    incoming.id,
                    active.id,
                )
                assert await reader.adjacent_edges(
                    (), filters=FactFilter(), reference_time=20.0, limit=2
                ) == GraphEdgeBatch((), False)

    run(exercise())


def test_adjacent_edges_exclude_before_truncating(database: PgDatabase) -> None:
    """排除已读事实后恰好达到上限时，批次不标记截断。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            endpoint, other = Entity(scope.id, "查询端点"), Entity(scope.id, "邻居")
            first, second, third = (
                _edge(identifier, endpoint, other) for identifier in ("a", "b", "c")
            )
            hidden = replace(_edge("0-hidden", endpoint, other), sensitive=True)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(endpoint)
                await writer.upsert_entity(other)
                await writer.save_facts((hidden, third, second, first), changed_at=10.0)

            filters = FactFilter(exclude_sensitive=True)
            async with storage.session(scope.id) as reader:
                truncated = await reader.adjacent_edges(
                    (endpoint.id,), filters=filters, reference_time=20.0, limit=2
                )
                assert truncated.truncated
                assert tuple(edge.fact_id for edge in truncated.edges) == (
                    first.id,
                    second.id,
                )
                exact = await reader.adjacent_edges(
                    (endpoint.id,),
                    filters=filters,
                    reference_time=20.0,
                    limit=2,
                    exclude_fact_ids=(first.id,),
                )
                assert not exact.truncated
                assert tuple(edge.fact_id for edge in exact.edges) == (
                    second.id,
                    third.id,
                )

    run(exercise())
