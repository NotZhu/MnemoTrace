"""验证候选绑定和批量比较的结构化调用。"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace

import httpx
import pytest

from mnemotrace_next.application.adjudication import (
    ComparisonCandidates,
    FactComparison,
    compare_facts,
    find_comparison_candidates,
    prepare_facts,
)
from mnemotrace_next.application.extraction import EntityMention, SpaceUserMention
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef
from mnemotrace_next.ports.inference import (
    CompletionRefused,
    CompletionTruncated,
    InvalidStructuredOutput,
)

from ..ports.fakes import FakeEmbedder, FakeIdGenerator, FakeSpaceSession
from .adjudication_samples import SPACE, FactIds, candidate
from .sample import chat_response, configured_chat


class _Vectors(FakeEmbedder):
    def __init__(self, vectors: Sequence[tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(tuple(texts))
        return self.vectors


def test_prepare_facts_binds_space_user_and_embeds_text_once() -> None:
    """绑定角色使用同一个空间用户，普通同名实体保持独立。"""
    inputs = (
        candidate("first", subject=EntityMention("用户"), target=SpaceUserMention()),
        candidate(
            "second",
            subject=SpaceUserMention(),
            target=EntityMention("用户"),
            text="用户认识同名实体",
        ),
    )
    embedder = _Vectors(((1.0, 0.0), (0.0, 1.0)))

    async def exercise() -> None:
        ids = FactIds()
        first, second = await prepare_facts(
            inputs, space_id=SPACE, embedder=embedder, ids=ids, received_at=100.0
        )
        assert embedder.calls == [tuple(item.text for item in inputs)]
        user = Entity.space_user(SPACE)
        named = Entity(SPACE, "用户")
        assert user.id != named.id
        assert user.id != Entity.space_user(SpaceId("other")).id
        assert first.fact.subject == second.fact.object == EntityRef(named.id)
        assert first.fact.object == second.fact.subject == EntityRef(user.id)
        assert second.vector == (0.0, 1.0)
        assert (
            await prepare_facts(
                (), space_id=SPACE, embedder=embedder, ids=ids, received_at=100.0
            )
            == ()
        )
        assert len(embedder.calls) == 1

    asyncio.run(exercise())


def test_batch_pairs_include_threshold_and_cap_each_selection() -> None:
    """槽位直接入选，其他候选按稳定相似度挑选并进行无向去重。"""

    async def exercise() -> None:
        extracted = (
            candidate("anchor"),
            *(candidate(str(i), predicate=f"other_{i}") for i in range(12)),
            candidate("same_slot"),
        )
        prepared = await prepare_facts(
            extracted,
            space_id=SPACE,
            embedder=_Vectors(((1.0, 0.0), *((3.0, 4.0),) * 12, (-1.0, 0.0))),
            ids=FactIds(),
            received_at=100.0,
        )
        reader = FakeSpaceSession(Space(SPACE, "owner", "测试", 0.0))
        result = await find_comparison_candidates(
            reader, prepared, embedding_space="test", reference_time=100.0
        )
        assert result.existing_facts == {}
        ids = [item.fact.id for item in prepared]
        assert [right for left, right in result.pairs if left == ids[0]] == [
            *ids[1:11],
            ids[-1],
        ]
        assert len(result.pairs) == len({frozenset(pair) for pair in result.pairs})

    asyncio.run(exercise())


def test_batch_pairs_exclude_disjoint_intervals_before_ranking() -> None:
    """已知错开的跨槽位事实不挤占相似候选名额。"""

    async def exercise() -> None:
        extracted = (
            *(
                candidate(
                    f"earlier-{index}", predicate="old_slot", start=10.0, end=30.0
                )
                for index in range(10)
            ),
            candidate("anchor", start=30.0, end=50.0),
            candidate("overlap", predicate="related_slot", start=40.0),
            candidate("same_slot", start=50.0),
        )
        embedder = FakeEmbedder()
        prepared = await prepare_facts(
            extracted,
            space_id=SPACE,
            embedder=embedder,
            ids=FactIds(),
            received_at=30.0,
        )
        reader = FakeSpaceSession(Space(SPACE, "owner", "测试", 0.0))
        result = await find_comparison_candidates(
            reader,
            prepared,
            embedding_space=embedder.embedding_space,
            reference_time=30.0,
        )
        anchor, overlap, same_slot = (item.fact.id for item in prepared[-3:])
        assert [pair for pair in result.pairs if anchor in pair] == [
            (anchor, overlap),
            (anchor, same_slot),
        ]

    asyncio.run(exercise())


def test_comparison_sends_unique_facts_and_disjoint_pairs_bypass_model() -> None:
    """已知错开的事实由应用判断，其余关系一次请求并按原配对顺序返回。"""

    async def exercise() -> None:
        prepared = await prepare_facts(
            (
                candidate(
                    "new",
                    text="星河项目改由乙负责",
                    predicate="managed_by",
                    subject=EntityMention("星河项目"),
                    target=EntityMention("乙"),
                    start=20.0,
                ),
            ),
            space_id=SPACE,
            embedder=FakeEmbedder(),
            ids=FakeIdGenerator(),
            received_at=100.0,
        )
        new = prepared[0].fact
        conflict = replace(
            new,
            id=FactId("conflict"),
            text="甲负责星河项目",
            valid_at=10.0,
            subject=EntityRef(Entity(SPACE, "甲").id),
            predicate=replace(new.predicate, value="manages"),
            object=new.subject,
        )
        unrelated = replace(
            conflict,
            id=FactId("other"),
            text="乙负责远山项目",
            subject=EntityRef(Entity(SPACE, "乙").id),
            object=EntityRef(Entity(SPACE, "远山项目").id),
        )
        history = replace(
            new,
            id=FactId("history"),
            valid_at=10.0,
            invalid_at=20.0,
            recorded_at=10.0,
            expired_at=30.0,
        )
        pairs = ((new.id, conflict.id), (new.id, history.id), (new.id, unrelated.id))
        existing = {fact.id: fact for fact in (conflict, unrelated, history)}
        calls: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
            assert len(payload["facts"]) == 3
            assert (
                payload["facts"][0]["subject"]
                == payload["facts"][1]["object"]["entity"]
            )
            assert (
                payload["facts"][0]["subject"]
                != payload["facts"][2]["object"]["entity"]
            )
            assert [pair["id"] for pair in payload["pairs"]] == ["1", "3"]
            return chat_response(
                '{"decisions":[{"id":"3","verdict":"coexists"},{"id":"1","verdict":"conflict"}]}'
            )

        async with configured_chat(respond) as llm:
            decisions = await compare_facts(
                llm, prepared, ComparisonCandidates(existing, pairs)
            )
            assert decisions == (
                FactComparison(pairs[0], "conflict"),
                FactComparison(pairs[1], "coexists", disjoint=True),
                FactComparison(pairs[2], "coexists"),
            )
            assert (
                await compare_facts(llm, prepared, ComparisonCandidates({}, ())) == ()
            )
            assert await compare_facts(
                llm, prepared, ComparisonCandidates(existing, (pairs[1],))
            ) == (decisions[1],)
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("body", "finish_reason", "refusal", "error"),
    [
        ('{"decisions":[]}', "stop", None, InvalidStructuredOutput),
        (
            '{"decisions":[{"id":"2","verdict":"unknown"}]}',
            "stop",
            None,
            InvalidStructuredOutput,
        ),
        (
            '{"decisions":[{"id":"1","verdict":"unknown"},{"id":"1","verdict":"duplicate"}]}',
            "stop",
            None,
            InvalidStructuredOutput,
        ),
        ('{"decisions":[', "length", None, CompletionTruncated),
        (None, "stop", "拒答", CompletionRefused),
    ],
    ids=["missing", "unknown-id", "duplicate-id", "truncated", "refusal"],
)
def test_comparison_rejects_incomplete_or_invalid_responses(
    body: str | None, finish_reason: str, refusal: str | None, error: type[Exception]
) -> None:
    """模型失败或配对结果不完整时结束整批裁决。"""

    async def exercise() -> None:
        prepared = await prepare_facts(
            (candidate("new"),),
            space_id=SPACE,
            embedder=FakeEmbedder(),
            ids=FactIds(),
            received_at=100.0,
        )
        original = prepared[0].fact
        old = replace(original, id=FactId("old"))
        comparisons = ComparisonCandidates({old.id: old}, ((original.id, old.id),))
        async with configured_chat(
            lambda _: chat_response(body, finish_reason=finish_reason, refusal=refusal)
        ) as llm:
            with pytest.raises(error):
                await compare_facts(llm, prepared, comparisons)

    asyncio.run(exercise())
