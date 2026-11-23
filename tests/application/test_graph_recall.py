"""验证图召回的起点、扩展预算和融合行为。"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

import pytest

from mnemotrace import GraphRecallOptions, Memory, RecallRelation
from mnemotrace.application.graph_recall import graph_ranking
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Source
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import EntityId, FactId, SpaceId
from mnemotrace.domain.salience import FactSalience
from mnemotrace.domain.space import Space
from mnemotrace.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace.ports.queries import FactFilter
from tests.ports.fakes import (
    FakeClock,
    FakeEmbedder,
    FakeIdGenerator,
    FakeLLM,
    FakeStorage,
)

_SPACE = Space(SpaceId("graph-recall"), "owner", "合成图召回", 0.0)
_LINK = Predicate("links_to")


def _edge(
    identifier: str, subject: Entity, target: Entity, text: str = "合成关系"
) -> Fact:
    return Fact(
        id=FactId(identifier),
        space_id=_SPACE.id,
        subject=EntityRef(subject.id),
        predicate=_LINK,
        object=EntityRef(target.id),
        text=text,
        valid_at=10.0,
        invalid_at=None,
        recorded_at=10.0,
        expired_at=None,
        provenance=(),
        labels=(),
        sensitive=False,
        source=Source.MANUAL,
    )


async def _seed(
    storage: FakeStorage,
    entities: Sequence[Entity],
    facts: Sequence[Fact],
    *,
    memory_decay: bool = False,
) -> None:
    await storage.create_space(replace(_SPACE, memory_decay=memory_decay))
    async with storage.session(_SPACE.id, write=True) as writer:
        for entity in entities:
            await writer.upsert_entity(entity)
        await writer.save_facts(facts, changed_at=10.0)


def test_graph_recall_expands_subjects_and_returns_flat_relation_evidence() -> None:
    """图路从词法关系的主体开始，二跳才能补到普通召回没有的事实。"""

    async def exercise() -> None:
        storage = FakeStorage()
        origin, bridge, target = (
            Entity(_SPACE.id, name) for name in ("项目", "负责人", "小组")
        )
        seed = _edge("z-seed", origin, bridge, "releasecode 发布事项")
        follow = _edge("a-follow", bridge, target, "负责人属于支持小组")
        literal = replace(seed, id=FactId("b-literal"), object=LiteralValue("发布事项"))
        await _seed(storage, (origin, bridge, target), (seed, follow, literal))
        memory = Memory(
            space_id=_SPACE.id,
            storage=storage,
            embedder=FakeEmbedder(),
            clock=FakeClock(),
            llm=FakeLLM(),
            ids=FakeIdGenerator(),
        )
        plain = await memory.recall("releasecode", top_k=5)
        assert [item.fact_id for item in plain.evidence] == [literal.id, seed.id]
        assert plain.evidence[0].relation is None
        relation = plain.evidence[1].relation
        assert relation is not None
        assert relation == RecallRelation(origin.id, _LINK, bridge.id)
        one_hop = await memory.recall(
            "releasecode", top_k=5, graph=GraphRecallOptions(max_hops=1)
        )
        assert [item.fact_id for item in one_hop.evidence] == [seed.id, literal.id]
        expanded = await memory.recall(
            "releasecode", top_k=5, graph=GraphRecallOptions()
        )
        assert [item.fact_id for item in expanded.evidence] == [
            seed.id,
            literal.id,
            follow.id,
        ]
        assert expanded.evidence[-1].relation == RecallRelation(
            bridge.id, _LINK, target.id
        )
        explicit = await memory.recall(
            "releasecode",
            top_k=5,
            graph=GraphRecallOptions(
                origin_entity_ids=[EntityId("missing"), relation.object_entity_id],
                predicates=[relation.predicate],
                max_hops=1,
            ),
        )
        assert {item.fact_id for item in explicit.evidence} == {
            seed.id,
            literal.id,
            follow.id,
        }
        for ids in ((), (EntityId("missing"),)):
            assert (
                await memory.recall(
                    "releasecode",
                    top_k=5,
                    graph=GraphRecallOptions(origin_entity_ids=ids),
                )
                == plain
            )

    asyncio.run(exercise())


@pytest.mark.parametrize(("top_k", "tail_count"), ((1, 1), (8, 29)))
def test_graph_order_interleaves_all_distinct_ranked_subjects(
    top_k: int, tail_count: int
) -> None:
    """两路命中同一主体时继续取下一个，直到两路候选都用完。"""

    async def exercise() -> None:
        storage = FakeStorage()
        a, b, c, d, tip = (Entity(_SPACE.id, name) for name in "abcde")
        first, repeated, second, sem_first, sem_second = (
            _edge(identifier, start, tip)
            for identifier, start in (
                ("a-1", a),
                ("a-2", a),
                ("b", b),
                ("c", c),
                ("d", d),
            )
        )
        tail_subjects = tuple(
            Entity(_SPACE.id, f"tail-{index}") for index in range(tail_count)
        )
        tail = tuple(_edge(subject.name, subject, tip) for subject in tail_subjects)
        await _seed(
            storage,
            (a, b, c, d, tip, *tail_subjects),
            (first, repeated, second, sem_first, sem_second, *tail),
        )
        async with storage.session(_SPACE.id) as reader:
            ranking = await graph_ranking(
                reader,
                GraphRecallOptions(max_hops=1),
                (first.id, repeated.id, second.id, *(edge.id for edge in tail)),
                (first.id, sem_first.id, sem_second.id),
                filters=FactFilter(),
                reference_time=100.0,
                top_k=top_k,
            )
        assert ranking == (
            first.id,
            repeated.id,
            sem_first.id,
            second.id,
            sem_second.id,
            *(edge.id for edge in tail),
        )

    asyncio.run(exercise())


def test_graph_expands_each_subject_once_with_a_shared_edge_budget() -> None:
    """自环和汇合只加入事实候选，回到访问过的主体时不重跑其出边。"""

    async def exercise() -> None:
        storage = FakeStorage()
        root, left, right, tip = (
            Entity(_SPACE.id, name) for name in ("起点", "左", "右", "末端")
        )
        first_layer = (
            _edge("00-loop", root, root),
            _edge("01-left", root, left),
            *(_edge(f"0{index}-right", root, right) for index in range(2, 6)),
        )
        left_edges = (_edge("y-left-tip", left, tip), _edge("z-left-tip", left, tip))
        from_right = _edge("a-right-tip", right, tip)
        cycle = _edge("back-to-root", tip, root)
        await _seed(
            storage,
            (root, left, right, tip),
            (*first_layer, *left_edges, from_right, cycle),
        )
        options = GraphRecallOptions(origin_entity_ids=[root.id, root.id], max_hops=4)
        first_eight = tuple(edge.id for edge in (*first_layer, *left_edges))
        assert options.origin_entity_ids == (root.id,)
        async with storage.session(_SPACE.id) as reader:
            for top_k, expected in (
                (1, first_eight),
                (2, (*first_eight, from_right.id, cycle.id)),
            ):
                assert (
                    await graph_ranking(
                        reader,
                        options,
                        (),
                        (),
                        filters=FactFilter(),
                        reference_time=100.0,
                        top_k=top_k,
                    )
                    == expected
                )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mode", ("lexical_seed", "explicit_seed", "rerank", "rerank_timeout")
)
def test_graph_candidates_participate_in_inference_and_timeout_fallback(
    mode: str,
) -> None:
    """重排收到统一候选池，嵌入超时仍可从词法或显式起点扩展。"""

    async def exercise() -> None:
        storage = FakeStorage()
        origin, bridge, tip = (
            Entity(_SPACE.id, name) for name in ("起点", "桥", "末端")
        )
        seed = replace(
            _edge("seed", origin, bridge, "releasecode"), valid_at=100.0 - 90 * 86_400
        )
        follow = replace(_edge("follow", bridge, tip), valid_at=100.0)
        await _seed(
            storage,
            (origin, bridge, tip),
            (seed, follow),
            memory_decay=mode == "explicit_seed",
        )

        class Embedder(FakeEmbedder):
            async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
                if mode in {"lexical_seed", "explicit_seed"}:
                    raise TimeoutError
                return await super().embed(texts)

        class Reranker:
            async def rerank(
                self, query: str, documents: Sequence[tuple[FactId, str]]
            ) -> Sequence[tuple[FactId, float]]:
                assert mode in {"rerank", "rerank_timeout"}
                assert tuple(documents) == (
                    (seed.id, seed.text),
                    (follow.id, follow.text),
                )
                if mode == "rerank_timeout":
                    raise TimeoutError
                return ((follow.id, 1.0), (seed.id, 0.5))

        recall = Memory.for_recall(
            space_id=_SPACE.id,
            storage=storage,
            embedder=Embedder(),
            clock=FakeClock(),
            reranker=Reranker(),
        )
        query, options = (
            ("无词法命中", GraphRecallOptions(origin_entity_ids=(origin.id,)))
            if mode == "explicit_seed"
            else ("releasecode", GraphRecallOptions())
        )
        result = await recall.recall(query, graph=options, top_k=2, rerank=True)
        if mode == "rerank":
            assert result.degraded_reason is None
            assert [item.fact_id for item in result.evidence] == [follow.id, seed.id]
        else:
            assert result.degraded_reason == (
                "reranker_timeout" if mode == "rerank_timeout" else "embedding_timeout"
            )
            assert [item.fact_id for item in result.evidence] == [seed.id, follow.id]
        if mode == "explicit_seed":
            recent = await recall.recall(
                query, graph=options, top_k=1, rerank=True, prefer_recent=True
            )
            async with storage.session(_SPACE.id, write=True) as writer:
                await writer.save_salience({follow.id: FactSalience(3.0, 100.0)})
            salient = await recall.recall(query, graph=options, top_k=1, rerank=True)
            assert salient == recent
            assert salient.degraded_reason == "embedding_timeout"
            assert [item.fact_id for item in salient.evidence] == [follow.id]

    asyncio.run(exercise())


@pytest.mark.parametrize("hops", (0, 5, True, 1.5))
def test_graph_options_reject_invalid_hops(hops: object) -> None:
    with pytest.raises(ValueError, match="max_hops"):
        GraphRecallOptions(max_hops=cast(int, hops))


def test_graph_options_validate_and_snapshot_sequences() -> None:
    origin_entity_ids = [EntityId("a")]
    options = GraphRecallOptions(
        origin_entity_ids=origin_entity_ids, predicates=[_LINK, _LINK]
    )
    origin_entity_ids.clear()
    assert options.origin_entity_ids == (EntityId("a"),)
    assert options.predicates == (_LINK,)
    with pytest.raises(ValueError, match="origin_entity_ids"):
        GraphRecallOptions(origin_entity_ids=(EntityId("a"),) * 33)
    with pytest.raises(ValueError, match="origin_entity_ids"):
        GraphRecallOptions(origin_entity_ids=(EntityId("a\x00b"),))
    with pytest.raises(ValueError, match="predicates"):
        GraphRecallOptions(predicates=())
    with pytest.raises(ValueError, match="predicates"):
        GraphRecallOptions(predicates=(_LINK,) * 33)
