"""验证真库候选和事实计算结果的原子保存与擦除。"""

import json
from collections.abc import Sequence
from dataclasses import replace

import httpx
import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres.reader import PostgresReader
from mnemotrace.application.adjudication import (
    FactComparison,
    compare_facts,
    find_comparison_candidates,
    prepare_facts,
)
from mnemotrace.application.fact_lifecycle import calculate_fact_writes
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import FactId, SessionId
from mnemotrace.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace.ports.queries import FactQuery
from mnemotrace.ports.storage import CutoffTrigger

from ..inference.adjudication_samples import candidate
from ..inference.sample import chat_response, configured_chat
from ..ports.fakes import FakeEmbedder, FakeIdGenerator
from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_write_candidates_limit_and_fuse_same_and_cross_slot_results(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """繁忙槽位和跨槽各保留十条，双路命中和榜外同槽高分都可入选。"""

    probes: list[tuple[int, int]] = []
    query_facts = PostgresReader.query_facts

    async def measure(
        reader: PostgresReader, query: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        rows = await query_facts(reader, query, reference_time=reference_time)
        probes.append((query.limit, len(rows)))
        return rows

    monkeypatch.setattr(PostgresReader, "query_facts", measure)

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope, sibling = space(), space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            await storage.create_space(sibling)
            user, project = Entity.space_user(scope.id), Entity(scope.id, "星河项目")
            predicate = Predicate("project_owner")
            timeline = tuple(
                replace(
                    fact(user, f"a-slot-{index:03}", text="早期发布说明"),
                    predicate=predicate,
                )
                for index in range(102)
            )
            timeline = (
                replace(timeline[0], valid_at=70.0),
                *timeline[1:-2],
                replace(timeline[-2], text="星河项目归属用户"),
                replace(timeline[-1], valid_at=1.0, text="星河项目归属用户").close(
                    2.0, 10.0
                ),
            )
            lexical = tuple(
                replace(
                    fact(project, f"lexical-{index:02}", text="星河项目归属用户"),
                    predicate=predicate,
                    object=EntityRef(user.id),
                )
                for index in range(11)
            )
            vectors = tuple(
                fact(project, f"vector-{index:02}", text="语义近邻")
                for index in range(11)
            )
            foreign_entity = Entity.space_user(sibling.id)
            foreign = fact(foreign_entity, "outside", text="星河项目归属用户")
            embedding = FakeEmbedder()
            async with storage.session(sibling.id, write=True) as writer:
                await writer.upsert_entity(foreign_entity)
                await writer.save_facts((foreign,), changed_at=20.0)
                await writer.upsert_vectors(
                    ((foreign.id, (1.0, 0.0)),),
                    embedding_space=embedding.embedding_space,
                )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(user)
                await writer.upsert_entity(project)
                await writer.save_facts(
                    (*timeline, *lexical, *vectors), changed_at=20.0
                )
                await writer.upsert_vectors(
                    tuple(
                        (item.id, (1.0, 0.0))
                        for item in (
                            *timeline[:3],
                            *timeline[-2:],
                            lexical[0],
                            *vectors,
                        )
                    ),
                    embedding_space=embedding.embedding_space,
                )
            prepared = await prepare_facts(
                (
                    candidate(
                        "source",
                        text=lexical[0].text,
                        predicate=predicate.value,
                        start=30.0,
                    ),
                ),
                space_id=scope.id,
                embedder=embedding,
                ids=FakeIdGenerator(),
                received_at=30.0,
            )
            async with storage.session(scope.id) as reader:
                result = await find_comparison_candidates(
                    reader,
                    prepared,
                    embedding_space=embedding.embedding_space,
                    reference_time=30.0,
                )
            expected = {
                item.id
                for item in (*timeline[:9], timeline[-2], *lexical[:6], *vectors[:4])
            }
            assert result.existing_facts.keys() == expected
            assert [old for _, old in result.pairs] == sorted(
                expected, key=lambda fact_id: fact_id.value
            )
            assert lexical[0].id in result.existing_facts
            assert timeline[0].valid_at == 70.0 and timeline[-1].invalid_at == 2.0
            assert probes == [(11, 11)]

    run(exercise())


def test_same_slot_candidates_match_each_interval_within_the_batch(
    database: PgDatabase,
) -> None:
    """少量同槽旧事实无需向量，按每条新事实的左闭右开区间纳入。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            user = Entity.space_user(scope.id)
            predicate = Predicate("reply_language")
            fixture = replace(fact(user, "fixture"), predicate=predicate)
            old = (
                replace(fixture, id=FactId("past"), valid_at=5.0).close(10.0, 40.0),
                replace(fixture, id=FactId("unknown-start")).close(15.0, 40.0),
                replace(fixture, id=FactId("gap"), valid_at=20.0).close(30.0, 40.0),
                replace(fixture, id=FactId("future"), valid_at=35.0),
            )
            embedder = FakeEmbedder()
            prepared = await prepare_facts(
                (
                    candidate("first", predicate=predicate.value, start=10.0, end=20.0),
                    candidate(
                        "second", predicate=predicate.value, start=30.0, end=40.0
                    ),
                ),
                space_id=scope.id,
                embedder=embedder,
                ids=FakeIdGenerator(),
                received_at=30.0,
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(user)
                await writer.save_facts(old, changed_at=40.0)

            async with storage.session(scope.id) as reader:
                found = await find_comparison_candidates(
                    reader,
                    prepared,
                    embedding_space=embedder.embedding_space,
                    reference_time=30.0,
                )
            first, second = (item.fact.id for item in prepared)
            assert found.existing_facts == {old[1].id: old[1], old[3].id: old[3]}
            assert found.pairs == (
                (first, old[1].id),
                (second, old[3].id),
            )

    run(exercise())


def test_write_candidates_exclude_disjoint_intervals_before_ranking(
    database: PgDatabase,
) -> None:
    """两路可召回有效期重叠的补录候选，跨谓词矛盾能提前截止旧事实。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            user, embedder = Entity.space_user(scope.id), FakeEmbedder()
            source = replace(
                message(scope, "backfill", at=30.0, content="星河项目使用英文"),
                recorded_at=100.0,
            )
            overlapping = replace(
                fact(user, "z-overlapping", text="星河项目使用中文"),
                predicate=Predicate("reply_language"),
                object=LiteralValue("中文"),
                valid_at=10.0,
            ).close(40.0, 80.0)
            disjoint = tuple(
                replace(overlapping, id=FactId(f"earlier-{index:02}"), invalid_at=30.0)
                for index in range(10)
            )
            lexical_only = replace(
                overlapping,
                id=FactId("unknown-start"),
                text="星河项目使用中文文档",
                valid_at=None,
            )
            vector_only = replace(
                fact(user, "later-overlapping", text="发布告示语种另定"), valid_at=120.0
            )
            later_disjoint = replace(
                overlapping,
                id=FactId("later-disjoint"),
                valid_at=150.0,
                invalid_at=None,
                expired_at=None,
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(source.session_id)
                await writer.upsert_entity(user)
                await writer.save_facts(
                    (*disjoint, overlapping, lexical_only, vector_only, later_disjoint),
                    changed_at=80.0,
                )
                await writer.upsert_vectors(
                    tuple(
                        (item.id, (1.0, 0.0))
                        for item in (*disjoint, vector_only, later_disjoint)
                    ),
                    embedding_space=embedder.embedding_space,
                )
            new = await prepare_facts(
                (candidate("backfill", text=source.content, start=30.0, end=150.0),),
                space_id=scope.id,
                embedder=embedder,
                ids=FakeIdGenerator(),
                received_at=100.0,
            )
            async with storage.session(scope.id) as reader:
                candidates = await find_comparison_candidates(
                    reader,
                    new,
                    embedding_space=embedder.embedding_space,
                    reference_time=100.0,
                )
            assert candidates.existing_facts.keys() == {
                overlapping.id,
                lexical_only.id,
                vector_only.id,
            }

            def respond(request: httpx.Request) -> httpx.Response:
                payload = json.loads(
                    json.loads(request.content)["messages"][-1]["content"]
                )
                texts = {row["id"]: row["text"] for row in payload["facts"]}
                assert {texts[pair["right"]] for pair in payload["pairs"]} == {
                    overlapping.text,
                    lexical_only.text,
                    vector_only.text,
                }
                return chat_response(
                    json.dumps(
                        {
                            "decisions": [
                                {
                                    "id": pair["id"],
                                    "verdict": "conflict"
                                    if texts[pair["right"]] == overlapping.text
                                    else "coexists",
                                }
                                for pair in payload["pairs"]
                            ]
                        }
                    )
                )

            async with configured_chat(respond) as llm:
                comparisons = await compare_facts(llm, new, candidates)
            result = calculate_fact_writes(
                new,
                comparisons,
                existing_facts=candidates.existing_facts,
                existing_support_groups={},
                changed_at=100.0,
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.append_messages((source,))
                changes = await writer.save_facts(
                    result.facts,
                    changed_at=100.0,
                    support_groups=result.support_groups,
                    cutoff_triggers=result.cutoff_triggers,
                )
            updated = next(
                change for change in changes if change.after.id == overlapping.id
            )
            assert updated.after.invalid_at == 30.0
            assert updated.cutoff_triggers == (
                CutoffTrigger(new[0].fact.id, 30.0, (source.id,)),
            )

    run(exercise())


@pytest.mark.parametrize("reuse_group", [False, True], ids=["new-group", "same-group"])
def test_duplicate_evidence_raises_sensitivity_in_storage(
    database: PgDatabase, reuse_group: bool
) -> None:
    """归并发现敏感证据时提升事实敏感性，已有证据组无需重复写入。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            original = message(scope, "original", content="默认使用英文")
            evidence = (
                original
                if reuse_group
                else message(scope, "evidence", content=original.content)
            )
            (seed, sensitive, ordinary) = await prepare_facts(
                (
                    candidate(original.id.value, text=original.content),
                    replace(
                        candidate(evidence.id.value, text=evidence.content),
                        sensitive=True,
                    ),
                    candidate(evidence.id.value, text=evidence.content),
                ),
                space_id=scope.id,
                embedder=FakeEmbedder(),
                ids=FakeIdGenerator(),
                received_at=20.0,
            )
            expected_groups = (
                ((original.id,),) if reuse_group else ((original.id,), (evidence.id,))
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(original.session_id)
                await writer.append_messages(
                    (original,) if reuse_group else (original, evidence)
                )
                await writer.upsert_entity(seed.entities[0])
                await writer.save_facts((seed.fact,), changed_at=20.0)
                groups = await writer.get_fact_support_groups((seed.fact.id,))
                writes = calculate_fact_writes(
                    (sensitive, ordinary),
                    tuple(
                        FactComparison((item.fact.id, seed.fact.id), "duplicate")
                        for item in (sensitive, ordinary)
                    ),
                    existing_facts={seed.fact.id: seed.fact},
                    existing_support_groups=groups,
                    changed_at=21.0,
                )
                changes = await writer.save_facts(
                    writes.facts, changed_at=21.0, support_groups=writes.support_groups
                )
                (change,) = changes
                assert change.before == seed.fact
                assert change.after.sensitive
                assert await writer.get_fact_support_groups((seed.fact.id,)) == {
                    seed.fact.id: expected_groups,
                }
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(seed.fact.id) == change.after
                assert (
                    await reader.get_fact(seed.fact.id, exclude_sensitive=True) is None
                )

    run(exercise())


def test_supplemental_evidence_preserves_source_free_cutoff_attribution(
    database: PgDatabase,
) -> None:
    """无原文的旧事实可触发截止，并在同一事务获得补验通过的证据。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            user = Entity.space_user(scope.id)
            old = replace(
                fact(user, "manual", text="从 20 秒起默认用英文"),
                valid_at=20.0,
                predicate=Predicate("default_language"),
                object=LiteralValue("英文"),
            )
            earlier, support, later = (
                message(scope, "earlier", content="从 10 秒起默认用法文"),
                message(scope, "support", content="20 至 40 秒期间默认用英文"),
                message(scope, "later", content="从 40 秒起默认用中文"),
            )
            prepared = await prepare_facts(
                (
                    candidate(
                        earlier.id.value,
                        text=earlier.content,
                        start=10.0,
                        target=LiteralValue("法文"),
                    ),
                    candidate(
                        support.id.value, text=support.content, start=20.0, end=40.0
                    ),
                    candidate(
                        later.id.value,
                        text=later.content,
                        start=40.0,
                        target=LiteralValue("中文"),
                    ),
                ),
                space_id=scope.id,
                embedder=FakeEmbedder(),
                ids=FakeIdGenerator(),
                received_at=100.0,
            )
            backfill, duplicate, successor = (item.fact for item in prepared)
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(earlier.session_id)
                await writer.upsert_entity(user)
                await writer.save_facts((old,), changed_at=20.0)
                groups = await writer.get_fact_support_groups((old.id,))
                result = calculate_fact_writes(
                    prepared,
                    (
                        FactComparison((backfill.id, old.id), "conflict"),
                        FactComparison((duplicate.id, old.id), "duplicate"),
                        FactComparison((successor.id, old.id), "conflict"),
                    ),
                    existing_facts={old.id: old},
                    existing_support_groups=groups,
                    changed_at=100.0,
                )
                await writer.append_messages((earlier, support, later))
                changes = await writer.save_facts(
                    result.facts,
                    changed_at=100.0,
                    support_groups=result.support_groups,
                    cutoff_triggers=result.cutoff_triggers,
                )
                assert await writer.get_fact_support_groups((old.id,)) == {
                    old.id: ((support.id,),),
                }
                history = {change.after.id: change for change in changes}
                assert history[old.id].after.invalid_at == 40.0
                assert history[backfill.id].cutoff_triggers == (
                    CutoffTrigger(old.id, 20.0, ()),
                )

    run(exercise())


def test_calculated_cutoffs_persist_evidence_and_erase_audit(
    database: PgDatabase,
) -> None:
    """通过存储端口发布阶段计算结果，并核对擦除后仍有独立证据的事实。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            embedder, ids = FakeEmbedder(), FakeIdGenerator()
            original = message(scope, "original", at=10.0, content="用户参加周末跑团")
            early_session = SessionId("early")
            later = message(
                scope, "later", at=40.0, content="用户从 40 秒起不再参加跑团"
            )
            sooner = message(
                scope,
                "sooner",
                session=early_session,
                at=20.0,
                content="用户从 20 秒起不再参加跑团",
            )
            corroboration = message(
                scope, "corroboration", at=50.0, content="用户从 20 秒起已经退出跑团"
            )
            (seed,) = await prepare_facts(
                (
                    candidate(
                        original.id.value,
                        start=10.0,
                        text=original.content,
                        predicate="runs_with_club",
                    ),
                ),
                space_id=scope.id,
                embedder=embedder,
                ids=ids,
                received_at=10.0,
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(original.session_id)
                await writer.register_session(early_session)
                await writer.append_messages((original,))
                await writer.upsert_entity(seed.entities[0])
                await writer.save_facts((seed.fact,), changed_at=10.0)

            new = await prepare_facts(
                tuple(
                    candidate(
                        item.id.value,
                        start=start,
                        text=item.content,
                        predicate="runs_with_club",
                    )
                    for item, start in (
                        (later, 40.0),
                        (sooner, 20.0),
                        (corroboration, 20.0),
                    )
                ),
                space_id=scope.id,
                embedder=embedder,
                ids=ids,
                received_at=60.0,
            )
            async with storage.session(scope.id) as reader:
                candidates = await find_comparison_candidates(
                    reader,
                    new,
                    embedding_space=embedder.embedding_space,
                    reference_time=60.0,
                )

            def respond(request: httpx.Request) -> httpx.Response:
                payload = json.loads(
                    json.loads(request.content)["messages"][-1]["content"]
                )
                text = {row["id"]: row["text"] for row in payload["facts"]}
                decisions = []
                for pair in payload["pairs"]:
                    left, right = text[pair["left"]], text[pair["right"]]
                    if original.content in (left, right):
                        verdict = "conflict"
                    elif {left, right} == {sooner.content, corroboration.content}:
                        verdict = "duplicate"
                    else:
                        verdict = "coexists"
                    decisions.append({"id": pair["id"], "verdict": verdict})
                return chat_response(json.dumps({"decisions": decisions}))

            async with configured_chat(respond) as llm:
                comparisons = await compare_facts(llm, new, candidates)
            async with storage.session(scope.id, write=True) as writer:
                groups = await writer.get_fact_support_groups((seed.fact.id,))
                result = calculate_fact_writes(
                    new,
                    comparisons,
                    existing_facts=candidates.existing_facts,
                    existing_support_groups=groups,
                    changed_at=61.0,
                )
                await writer.append_messages((later, sooner, corroboration))
                changes = await writer.save_facts(
                    result.facts,
                    changed_at=61.0,
                    support_groups=result.support_groups,
                    cutoff_triggers=result.cutoff_triggers,
                )
                await writer.upsert_vectors(
                    tuple(result.vectors.items()),
                    embedding_space=embedder.embedding_space,
                )
                assert {change.after.id for change in changes} == {
                    seed.fact.id,
                    new[0].fact.id,
                    new[1].fact.id,
                }
                updated = next(
                    change for change in changes if change.after.id == seed.fact.id
                )
                assert updated.before == seed.fact and updated.after.invalid_at == 20.0
                assert updated.cutoff_triggers == (
                    CutoffTrigger(new[0].fact.id, 40.0, (later.id,)),
                    CutoffTrigger(new[1].fact.id, 20.0, (sooner.id,)),
                )
                assert await writer.get_fact_support_groups((new[1].fact.id,)) == {
                    new[1].fact.id: ((sooner.id,), (corroboration.id,)),
                }
                await writer.delete_session_data(early_session)
                assert await writer.get_fact(seed.fact.id) == updated.after
                assert await writer.get_fact_support_groups((new[1].fact.id,)) == {
                    new[1].fact.id: ((corroboration.id,),),
                }
                remaining = await writer.list_fact_changes(seed.fact.id)
                assert len(remaining) == 1 and remaining[0].before is None

    run(exercise())
