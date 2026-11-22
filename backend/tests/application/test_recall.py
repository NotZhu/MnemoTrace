"""验证 Python 召回入口的行为。"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import cast

import pytest

from mnemotrace_next import GraphRecallOptions, Memory, RecallResult
from mnemotrace_next.application.recall import (
    _candidates_within_boost,
    _recency_bonus,
    _score_with_boosts,
)
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace_next.ports.inference import InferenceError
from mnemotrace_next.ports.storage import SpaceSession
from tests.ports.fakes import (
    FakeClock,
    FakeEmbedder,
    FakeIdGenerator,
    FakeLLM,
    FakeSpaceSession,
    FakeStorage,
)

_SPACE = Space(SpaceId("recall"), "owner", "召回测试", 0.0)
_SUBJECT = Entity(_SPACE.id, "合成主体")
_RANKING_OPTIONS = ((False, False), (True, False), (False, True), (True, True))


class _UnexpectedEmbedder(FakeEmbedder):
    """验证边界拒绝的请求不会发给嵌入端口。"""

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise AssertionError("无效或未授权的查询不能请求嵌入模型")


def _fact(identifier: str, text: str) -> Fact:
    return Fact(
        id=FactId(identifier),
        space_id=_SPACE.id,
        subject=EntityRef(_SUBJECT.id),
        predicate=Predicate("preference"),
        object=LiteralValue(text),
        text=text,
        valid_at=None,
        invalid_at=None,
        recorded_at=10.0,
        expired_at=None,
        provenance=(),
        labels=(),
        sensitive=False,
        source=Source.MANUAL,
    )


def test_read_only_recall_combines_independent_hits() -> None:
    """同一事实只返回一次，任一通道的独占命中也可返回。"""
    storage, embedder = FakeStorage(), FakeEmbedder()
    reader = Memory.for_recall(
        space_id=_SPACE.id, storage=storage, embedder=embedder, clock=FakeClock()
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        semantic = _fact("a", "部署前检查变更记录")
        lexical = _fact("b", "MT-042 涉及发布流程")
        shared = _fact("z", "MT-042")
        weak = _fact("weak", "无关事项")
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts((semantic, lexical, shared, weak), changed_at=10.0)
            await writer.upsert_vectors(
                (
                    (semantic.id, (1.0, 0.0)),
                    (shared.id, (0.9, 0.0)),
                    (weak.id, (0.0, 1.0)),
                ),
                embedding_space=embedder.embedding_space,
            )

        result = await reader.recall("MT-042", top_k=5)
        assert [item.fact_id for item in result.evidence] == [
            shared.id,
            semantic.id,
            lexical.id,
        ]
        assert result.evidence[0].text == shared.text
        assert (await reader.recall("MT-042", top_k=1)).evidence == result.evidence[:1]
        assert await reader.recall("MT-042", top_k=5, prefer_recent=True) == result

    asyncio.run(exercise())


def test_recall_boosts_only_visible_facts_within_the_relevance_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新近度和显著度只调整已授权、能进入截断线的候选。"""
    storage, embedder, clock = FakeStorage(), FakeEmbedder(), FakeClock()
    now = clock.now()
    old = now - 90 * 86_400
    text = "发布规范"
    candidates = tuple(_fact(identifier, text) for identifier in "abcdef")
    unknown, backfill, recent, fourth, fifth, sixth = candidates
    backfill = replace(backfill, valid_at=old, recorded_at=now)
    recent = replace(recent, valid_at=now)
    salience_before = FactSalience(2.5, now)
    strong = replace(_fact("strong", text), valid_at=old)
    hidden = replace(_fact("0-hidden", text), valid_at=now, sensitive=True)
    expired = replace(_fact("0-expired", text), valid_at=old).close(now, now)
    future = replace(_fact("0-future", text), valid_at=now + 1.0)
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=embedder,
        clock=clock,
        exclude_sensitive=True,
    )
    reads: list[tuple[FactId, ...]] = []
    salience_reads: list[tuple[FactId, ...]] = []
    get_facts = FakeSpaceSession.get_facts
    get_salience = FakeSpaceSession.get_salience

    async def record_load(
        session: FakeSpaceSession,
        ids: Sequence[FactId],
        *,
        exclude_sensitive: bool = False,
    ) -> Mapping[FactId, Fact]:
        reads.append(tuple(ids))
        return await get_facts(session, ids, exclude_sensitive=exclude_sensitive)

    monkeypatch.setattr(FakeSpaceSession, "get_facts", record_load)

    async def record_salience(
        session: FakeSpaceSession,
        ids: Sequence[FactId],
        *,
        exclude_sensitive: bool = False,
    ) -> Mapping[FactId, FactSalience]:
        assert exclude_sensitive
        salience_reads.append(tuple(ids))
        return await get_salience(session, ids, exclude_sensitive=exclude_sensitive)

    monkeypatch.setattr(FakeSpaceSession, "get_salience", record_salience)

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts(
                (
                    unknown,
                    backfill,
                    recent,
                    fourth,
                    fifth,
                    sixth,
                    strong,
                    hidden,
                    expired,
                    future,
                ),
                changed_at=now,
            )
            await writer.upsert_vectors(
                ((strong.id, (1.0, 0.0)),), embedding_space=embedder.embedding_space
            )
            await writer.save_salience(
                {item.id: salience_before for item in (recent, hidden, expired, future)}
            )

        baseline = await reader.recall(text, top_k=3)
        baseline_ids = (strong.id, unknown.id, backfill.id)
        assert tuple(hit.fact_id for hit in baseline.evidence) == baseline_ids
        assert await reader.recall(text, top_k=3, prefer_recent=False) == baseline

        preferred = await reader.recall(text, top_k=3, prefer_recent=True)
        assert tuple(hit.fact_id for hit in preferred.evidence) == (
            strong.id,
            recent.id,
            unknown.id,
        )
        boost_ids = (*baseline_ids, recent.id, fourth.id, fifth.id)
        assert reads == [baseline_ids, baseline_ids, boost_ids]
        assert salience_reads == []
        monkeypatch.setattr(
            storage.sessions[_SPACE.id], "_space", replace(_SPACE, memory_decay=True)
        )
        assert await reader.recall(text, top_k=3) == preferred
        assert await reader.recall(text, top_k=3, prefer_recent=True) == preferred
        assert salience_reads == [boost_ids, boost_ids]
        assert await reader.recall(
            text, top_k=3, as_of=now, prefer_recent=True
        ) == await reader.recall(text, top_k=3, as_of=now)
        assert salience_reads == [boost_ids, boost_ids]
        async with storage.session(_SPACE.id) as session:
            assert await session.get_salience((recent.id,), exclude_sensitive=True) == {
                recent.id: salience_before
            }

    asyncio.run(exercise())


def test_ranking_cutoff_includes_the_exact_maximum_boost_boundary() -> None:
    """最高加成正好达到截断线时，候选仍可能保留。"""
    ranking = [(FactId("a"), 1.05), (FactId("b"), 1.0), (FactId("c"), 0.99)]
    assert _candidates_within_boost(ranking, 1) == ranking[:2]
    assert _candidates_within_boost(ranking, 5) == ranking


@pytest.mark.parametrize(
    ("age_days", "factor"), ((None, 1.0), (0, 1.05), (30, 1.025), (90, 1.00625))
)
def test_recency_score_uses_a_thirty_day_half_life(
    age_days: int | None, factor: float
) -> None:
    """未知起点为中性，已知起点的加成按半衰期变化。"""
    now = FakeClock().now()
    valid_at = None if age_days is None else now - age_days * 86_400
    assert _score_with_boosts(
        0.02, _recency_bonus(valid_at, now), 0.0
    ) == pytest.approx(0.02 * factor)


@pytest.mark.parametrize(
    ("recency_bonus", "normalized_salience", "factor"),
    ((0.0, 0.0, 1.0), (0.0, 0.25, 1.0125), (0.02, 0.2, 1.03), (0.05, 1.0, 1.05)),
)
def test_recency_and_salience_share_the_boost_cap(
    recency_bonus: float, normalized_salience: float, factor: float
) -> None:
    """两种信号可以叠加，整体加成至多 5%。"""
    assert _score_with_boosts(
        0.02, recency_bonus, normalized_salience
    ) == pytest.approx(0.02 * factor)


@pytest.mark.parametrize(("prefer_recent", "memory_decay"), _RANKING_OPTIONS)
def test_recall_boosts_after_scoring_the_bounded_rrf_pool(
    prefer_recent: bool, memory_decay: bool
) -> None:
    """加成可把外部分数第六的候选提入 Top-5，池外候选不送重排。"""
    storage = FakeStorage()
    candidates = tuple(
        replace(
            _fact(f"{index:02}", "发布规范"),
            valid_at=100.0 if index in (5, 21) else 100.0 - 90 * 86_400,
        )
        for index in range(25)
    )
    boosted = prefer_recent or memory_decay

    class ScoredReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            assert query == "发布规范"
            assert tuple(documents) == tuple(
                (candidate.id, candidate.text) for candidate in candidates[:20]
            )
            return tuple(
                (fact_id, 0.9 if i < 4 else 0.6 if i == 4 else 0.59 if i == 5 else 0.1)
                for i, (fact_id, _) in reversed(tuple(enumerate(documents)))
            )

    memory = Memory(
        space_id=_SPACE.id,
        storage=storage,
        llm=FakeLLM(),
        embedder=FakeEmbedder(),
        clock=FakeClock(),
        ids=FakeIdGenerator(),
        reranker=ScoredReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(replace(_SPACE, memory_decay=memory_decay))
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts(candidates, changed_at=10.0)
            await writer.save_salience(
                {candidates[index].id: FactSalience(2.0, 100.0) for index in (5, 21)}
            )

        result = await memory.recall(
            "发布规范",
            top_k=5,
            rerank=True,
            prefer_recent=prefer_recent,
        )
        assert tuple(item.fact_id for item in result.evidence) == (
            *(item.id for item in candidates[:4]),
            candidates[5 if boosted else 4].id,
        )
        assert result.degraded_reason is None
        assert result.rerank_applied
        historical = await memory.recall(
            "发布规范", top_k=5, as_of=100.0, rerank=True, prefer_recent=True
        )
        assert tuple(item.fact_id for item in historical.evidence) == tuple(
            item.id for item in candidates[:5]
        )

    asyncio.run(exercise())


def test_recall_skips_reranking_for_a_single_candidate() -> None:
    """候选池只有一条时直接返回证据，不调用外部重排器。"""
    storage = FakeStorage()
    only_fact = _fact("only", "发布规范")

    class UnexpectedReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            raise AssertionError("单个候选无须调用重排器")

    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=FakeEmbedder(),
        clock=FakeClock(),
        reranker=UnexpectedReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts((only_fact,), changed_at=10.0)
        result = await reader.recall("发布规范", rerank=True)
        assert tuple(item.fact_id for item in result.evidence) == (only_fact.id,)
        assert result.degraded_reason is None
        assert not result.rerank_applied

    asyncio.run(exercise())


@pytest.mark.parametrize("embedding_times_out", (True, False), ids=("embed", "rank"))
@pytest.mark.parametrize(("prefer_recent", "memory_decay"), _RANKING_OPTIONS)
def test_recall_reports_inference_timeout_and_keeps_the_expected_order(
    embedding_times_out: bool,
    prefer_recent: bool,
    memory_decay: bool,
) -> None:
    """嵌入超时走词法，重排超时保留 RRF，空结果也报告降级。"""
    storage = FakeStorage()
    semantic = _fact("a", "部署前检查变更记录")
    lexical = replace(
        _fact("b", "发布规范"), valid_at=None if embedding_times_out else 100.0
    )
    shared = replace(
        _fact("z", "发布规范"), valid_at=100.0 if embedding_times_out else None
    )
    boosted = prefer_recent or memory_decay

    class QueryEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            if embedding_times_out:
                raise TimeoutError("合成嵌入超时")
            return await super().embed(texts)

    class TimeoutReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            assert not embedding_times_out
            assert tuple(fact_id for fact_id, _ in documents) == (
                shared.id,
                semantic.id,
                lexical.id,
            )
            raise TimeoutError("合成重排超时")

    embedder = QueryEmbedder()
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=embedder,
        clock=FakeClock(),
        reranker=TimeoutReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(replace(_SPACE, memory_decay=memory_decay))
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts((semantic, lexical, shared), changed_at=10.0)
            await writer.upsert_vectors(
                ((semantic.id, (1.0, 0.0)), (shared.id, (0.7, 0.3))),
                embedding_space=embedder.embedding_space,
            )
            target = shared if embedding_times_out else lexical
            await writer.save_salience({target.id: FactSalience(3.0, 100.0)})

        result = await reader.recall(
            "发布规范",
            top_k=2,
            rerank=True,
            prefer_recent=prefer_recent,
        )
        ids = [item.fact_id for item in result.evidence]
        assert not result.rerank_applied
        if embedding_times_out:
            assert ids == (
                [shared.id, lexical.id] if boosted else [lexical.id, shared.id]
            )
            assert result.degraded_reason == "embedding_timeout"
            assert await reader.recall(
                "未命中",
                rerank=True,
                prefer_recent=prefer_recent,
            ) == RecallResult((), "embedding_timeout")
        else:
            assert ids == [shared.id, lexical.id if boosted else semantic.id]
            assert result.degraded_reason == "reranker_timeout"

    asyncio.run(exercise())


def test_recall_rejects_unconfigured_reranking_before_embedding() -> None:
    """显式开启未装配的重排器时报告配置错误。"""
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=FakeStorage(),
        embedder=_UnexpectedEmbedder(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="装配重排器"):
        asyncio.run(reader.recall("发布规范", rerank=True))


@pytest.mark.parametrize(
    "vectors",
    (
        None,
        (),
        (None,),
        ((),),
        ((1.0, 0.0), (1.0, 0.0)),
        (0.25,),
        (b"12",),
        ((1.0,),),
        ((1.0, 0.0, 0.0),),
        (("1", 0.0),),
        ((True, 0.0),),
        ((float("nan"), 0.0),),
        ((float("inf"), 0.0),),
        ((10**400, 0.0),),
        ((-0.0, 0.0),),
    ),
    ids=(
        "missing-batch",
        "empty-batch",
        "missing-vector",
        "empty-vector",
        "extra-vector",
        "scalar",
        "bytes-vector",
        "short-vector",
        "long-vector",
        "text-component",
        "bool-component",
        "nan-component",
        "infinite-component",
        "overflowing-component",
        "zero-vector",
    ),
)
def test_recall_rejects_an_invalid_query_embedding(vectors: object) -> None:
    """无效查询嵌入在打开候选读会话前报告推理错误。"""

    class AuthOnlyStorage(FakeStorage):
        opened = False

        @asynccontextmanager
        async def session(
            self, space_id: SpaceId, *, write: bool = False
        ) -> AsyncIterator[SpaceSession]:
            assert not self.opened, "无效查询嵌入不应打开候选读会话"
            self.opened = True
            async with super().session(space_id, write=write) as session:
                yield session

    class InvalidEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return cast(Sequence[Sequence[float]], vectors)

    storage = AuthOnlyStorage()
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=InvalidEmbedder(),
        clock=FakeClock(),
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        with pytest.raises(InferenceError, match="查询嵌入"):
            await reader.recall("发布规范")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "ranking",
    (
        (("a", 0.5),),
        (("a", 0.5), ("b", 0.5), ("foreign", 0.7)),
        (("a", 0.5), ("b", 0.5), ("a", 0.7)),
        (("a", 0.5), ("b", 0.5), ("c", float("nan"))),
        (("a", 0.5), ("b", 0.5), ("c", float("inf"))),
        (("a", 0.5), ("b", 0.5), ("c", True)),
        (("a", 0.5), ("b", 0.5), ("c", -0.1)),
        (("a", 0.5), ("b", 0.5), ("c", 1.1)),
    ),
    ids=("missing", "foreign", "duplicate", "nan", "inf", "bool", "negative", "over"),
)
def test_recall_rejects_invalid_reranking(
    ranking: tuple[tuple[str, float], ...],
) -> None:
    """候选外标识、重复和超量都报告推理错误。"""
    storage = FakeStorage()

    class InvalidReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            return tuple((FactId(identifier), score) for identifier, score in ranking)

    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=FakeEmbedder(),
        clock=FakeClock(),
        reranker=InvalidReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts(
                tuple(_fact(identifier, "发布规范") for identifier in ("a", "b", "c")),
                changed_at=10.0,
            )
        with pytest.raises(InferenceError, match="重排"):
            await reader.recall("发布规范", top_k=2, rerank=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("query", "top_k", "as_of"),
    [
        (" \t ", 1, None),
        ("带\x00空字符", 1, None),
        ("x" * 4097, 1, None),
        ("\ud800", 1, None),
        ("发布规范", True, None),
        ("发布规范", 0, None),
        ("发布规范", 101, None),
        ("发布规范", 1, float("nan")),
        ("发布规范", 1, float("inf")),
        ("发布规范", 1, True),
        ("发布规范", 1, 10**400),
    ],
    ids=[
        "blank",
        "nul",
        "too-long",
        "unicode",
        "bool-limit",
        "zero-limit",
        "large-limit",
        "nan",
        "infinity",
        "bool-time",
        "overflow-time",
    ],
)
def test_recall_rejects_invalid_requests_before_inference(
    query: str, top_k: int, as_of: float | None
) -> None:
    """无效的输入在访问空间或请求嵌入前被拒绝。"""
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=FakeStorage(),
        embedder=_UnexpectedEmbedder(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError):
        asyncio.run(reader.recall(query, top_k=top_k, as_of=as_of))


def test_recall_rejects_invalid_graph_options_before_inference() -> None:
    """图召回只接受经过校验的参数对象。"""
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=FakeStorage(),
        embedder=_UnexpectedEmbedder(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="GraphRecallOptions"):
        asyncio.run(reader.recall("发布规范", graph=cast(GraphRecallOptions, {})))


@pytest.mark.parametrize("invalid", (1, "true", None))
def test_recall_rejects_invalid_ranking_option_before_inference(
    invalid: object,
) -> None:
    """排序开关只接受布尔值。"""
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=FakeStorage(),
        embedder=_UnexpectedEmbedder(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="prefer_recent"):
        asyncio.run(reader.recall("发布规范", prefer_recent=cast(bool, invalid)))


def test_recall_returns_empty_evidence_for_no_hits() -> None:
    """已授权的空空间可以正常查询。"""
    storage = FakeStorage()
    reader = Memory.for_recall(
        space_id=_SPACE.id, storage=storage, embedder=FakeEmbedder(), clock=FakeClock()
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        assert (await reader.recall("发布规范")).evidence == ()

    asyncio.run(exercise())


def test_character_budget_keeps_a_complete_reranked_prefix_and_tracks_only_that_prefix() -> (
    None
):
    """按 Unicode 码点裁切重排结果，超限后保留证据顺序。"""
    storage = FakeStorage()
    first = _fact("b", "预算 甲🙂")
    second = _fact("c", "预算 第二条更长")
    last = _fact("a", "预算")

    class FixedReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            scores = {first.id: 0.9, second.id: 0.8, last.id: 0.7}
            return tuple((fact_id, scores[fact_id]) for fact_id, _ in documents)

    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=storage,
        embedder=FakeEmbedder(),
        clock=FakeClock(),
        reranker=FixedReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(replace(_SPACE, memory_decay=True))
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts((last, first, second), changed_at=10.0)
        order = (first.id, second.id, last.id)
        assert len(first.text) == 5
        for top_k, budget, expected, truncated in (
            (3, None, order, False),
            (3, 5, order[:1], True),
            (3, 5 + len(last.text), order[:1], True),
            (3, 4, (), True),
            (3, sum(len(item.text) for item in (first, second, last)), order, False),
            (1, 5, order[:1], False),
        ):
            result, usage = await reader.recall_with_usage(
                "预算",
                track_usage=True,
                rerank=True,
                top_k=top_k,
                max_evidence_chars=budget,
            )
            assert result.rerank_applied
            assert result.truncated is truncated
            assert tuple(item.fact_id for item in result.evidence) == expected
            assert (usage.fact_ids if usage else ()) == expected
        no_hits = await reader.recall("无关查询", max_evidence_chars=1)
        assert not no_hits.evidence and not no_hits.truncated

    asyncio.run(exercise())


@pytest.mark.parametrize("budget", (False, 0, -1, 1.5, "5"))
def test_recall_rejects_invalid_character_budget_before_inference(
    budget: object,
) -> None:
    """字符预算只接受正整数。"""
    reader = Memory.for_recall(
        space_id=_SPACE.id,
        storage=FakeStorage(),
        embedder=_UnexpectedEmbedder(),
        clock=FakeClock(),
    )
    with pytest.raises(ValueError, match="max_evidence_chars"):
        asyncio.run(reader.recall("预算", max_evidence_chars=cast(int, budget)))
