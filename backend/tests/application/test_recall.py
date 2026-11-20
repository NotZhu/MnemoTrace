"""验证 Python 召回入口的行为。"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import cast

import pytest

from mnemotrace_next import GraphRecallOptions, Memory, RecallResult
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace_next.ports.inference import InferenceError
from mnemotrace_next.ports.storage import SpaceSession
from tests.ports.fakes import (
    FakeClock,
    FakeEmbedder,
    FakeIdGenerator,
    FakeLLM,
    FakeStorage,
)

_SPACE = Space(SpaceId("recall"), "owner", "召回测试", 0.0)
_SUBJECT = Entity(_SPACE.id, "合成主体")


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

    asyncio.run(exercise())


def test_recall_reranks_a_bounded_pool_and_fills_with_rrf() -> None:
    """重排按需启用，部分返回按原排名补齐，并按 ID 区分同正文事实。"""
    storage = FakeStorage()
    candidates = tuple(_fact(f"{index:02}", "发布规范") for index in range(25))

    class PartialReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
        ) -> Sequence[FactId]:
            assert query == "发布规范"
            assert limit == 3
            assert tuple(documents) == tuple(
                (candidate.id, candidate.text) for candidate in candidates[:20]
            )
            return candidates[19].id, candidates[1].id

    memory = Memory(
        space_id=_SPACE.id,
        storage=storage,
        llm=FakeLLM(),
        embedder=FakeEmbedder(),
        clock=FakeClock(),
        ids=FakeIdGenerator(),
        reranker=PartialReranker(),
    )

    async def exercise() -> None:
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts(candidates, changed_at=10.0)

        baseline = await memory.recall("发布规范", top_k=3)
        assert tuple(item.fact_id for item in baseline.evidence) == tuple(
            candidate.id for candidate in candidates[:3]
        )
        result = await memory.recall("发布规范", top_k=3, rerank=True)
        assert tuple(item.fact_id for item in result.evidence) == (
            candidates[19].id,
            candidates[1].id,
            candidates[0].id,
        )
        assert result.degraded_reason is None

    asyncio.run(exercise())


def test_recall_skips_reranking_for_a_single_candidate() -> None:
    """唯一候选不需要推理也能确定顺序。"""
    storage = FakeStorage()
    only_fact = _fact("only", "发布规范")

    class UnexpectedReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
        ) -> Sequence[FactId]:
            raise AssertionError("单个候选无需调用重排器")

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

    asyncio.run(exercise())


@pytest.mark.parametrize("embedding_times_out", (True, False), ids=("embed", "rank"))
def test_recall_reports_inference_timeout_and_keeps_the_expected_order(
    embedding_times_out: bool,
) -> None:
    """嵌入超时走词法，重排超时保留 RRF，空结果也报告降级。"""
    storage = FakeStorage()
    semantic = _fact("a", "部署前检查变更记录")
    lexical = _fact("b", "发布规范")
    shared = _fact("z", "发布规范")

    class QueryEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            if embedding_times_out:
                raise TimeoutError("合成嵌入超时")
            return await super().embed(texts)

    class TimeoutReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
        ) -> Sequence[FactId]:
            assert not embedding_times_out
            assert [fact_id for fact_id, _ in documents] == [
                shared.id,
                semantic.id,
                lexical.id,
            ]
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
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as writer:
            await writer.upsert_entity(_SUBJECT)
            await writer.save_facts((semantic, lexical, shared), changed_at=10.0)
            await writer.upsert_vectors(
                ((semantic.id, (1.0, 0.0)), (shared.id, (0.7, 0.3))),
                embedding_space=embedder.embedding_space,
            )

        result = await reader.recall("发布规范", top_k=2, rerank=True)
        ids = [item.fact_id for item in result.evidence]
        if embedding_times_out:
            assert ids == [lexical.id, shared.id]
            assert result.degraded_reason == "embedding_timeout"
            assert await reader.recall("未命中", rerank=True) == RecallResult(
                (), "embedding_timeout"
            )
        else:
            assert ids == [shared.id, semantic.id]
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
    (("foreign",), ("a", "a"), ("a", "b", "c")),
    ids=("foreign", "duplicate", "over-limit"),
)
def test_recall_rejects_invalid_reranking(ranking: tuple[str, ...]) -> None:
    """候选外标识、重复和超量都报告推理错误。"""
    storage = FakeStorage()

    class InvalidReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
        ) -> Sequence[FactId]:
            return tuple(FactId(identifier) for identifier in ranking)

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
        with pytest.raises(InferenceError, match="重排返回"):
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
