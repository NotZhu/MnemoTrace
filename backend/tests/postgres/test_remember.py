"""通过公开用例验证 PostgreSQL 中的记忆写入。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from itertools import count
from time import perf_counter
from types import TracebackType
from typing import cast

import pytest
from psycopg import AsyncConnection, errors
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg.transaction import AsyncTransaction
from psycopg_pool import AsyncConnectionPool

from mnemotrace_next import (
    CommitOutcomeUnknown,
    Memory,
    MessageInput,
    RememberInvalidated,
    RememberRetryRequired,
    SessionUnavailable,
)
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres import session as postgres_session
from mnemotrace_next.adapters.postgres.facts import FactWrite, write_fact_batch
from mnemotrace_next.adapters.postgres.reader import PostgresReader
from mnemotrace_next.adapters.postgres.session import PostgresSession
from mnemotrace_next.application import remember as remember_application
from mnemotrace_next.application.adjudication import (
    BatchPairs,
    ComparisonCandidates,
    PreparedFact,
    Verdict,
    find_comparison_candidates,
)
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, MessageId, SessionId, SpaceId
from mnemotrace_next.domain.profile import Profile, ProfileEntry
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import LiteralValue, Predicate
from mnemotrace_next.ports.inference import CompletionTruncated, InvalidStructuredOutput
from mnemotrace_next.ports.queries import FactFilter, FactQuery
from mnemotrace_next.ports.storage import (
    CutoffTrigger,
    SpaceReader,
    Storage,
    SupportGroups,
)

from ..inference.adjudication_samples import FactIds
from ..ports.fakes import FakeClock, FakeEmbedder, FakeLLM
from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres

_PREDICATE = Predicate("project_language")
_CURRENT = "星河项目默认用英文"


class _Ids(FactIds):
    def __init__(self) -> None:
        super().__init__()
        self._messages = count()
        self._sessions = count()

    def new_message_id(self) -> MessageId:
        return MessageId(f"incoming-{next(self._messages)}")

    def new_session_id(self) -> SessionId:
        return SessionId(f"session-{next(self._sessions)}")


class _Embedder(FakeEmbedder):
    def __init__(self, hook: Callable[[int], Awaitable[None]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.hook = hook

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.calls.append(tuple(texts))
        if self.hook is not None:
            await self.hook(len(self.calls))
        return await super().embed(texts)


class _Model(FakeLLM):
    def __init__(
        self,
        *,
        verdicts: Mapping[str, Verdict] | None = None,
        hook: Callable[[int], Awaitable[None]] | None = None,
        extraction_hook: Callable[[int], Awaitable[None]] | None = None,
        empty: bool = False,
        invalid: bool = False,
        sensitive: bool = False,
    ) -> None:
        self.verdicts = verdicts or {}
        self.hook = hook
        self.extraction_hook = extraction_hook
        self.empty = empty
        self.invalid = invalid
        self.sensitive = sensitive
        self.extraction_calls: list[dict[str, object]] = []
        self.comparison_calls: list[tuple[str, ...]] = []

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        payload = json.loads(prompt)
        if "messages" in payload:
            self.extraction_calls.append(payload)
            if self.extraction_hook is not None:
                await self.extraction_hook(len(self.extraction_calls))
            if self.empty:
                return {"facts": []}
            source = payload["messages"][0]["number"]
            return {
                "facts": [
                    {
                        "subject": {"kind": "entity", "value": "星河项目"},
                        "predicate": _PREDICATE.value,
                        "object": {"kind": "literal", "value": "英文"},
                        "text": _CURRENT,
                        "valid_at_evidence": {
                            "kind": "message_time",
                            "source_number": source,
                            "value": None,
                            "days": None,
                        },
                        "invalid_at_evidence": None,
                        "source_numbers": [source],
                        "labels": [],
                        "sensitive": self.sensitive,
                    }
                ]
            }
        facts = {row["id"]: row["text"] for row in payload["facts"]}
        old_texts = tuple(facts[pair["right"]] for pair in payload["pairs"])
        self.comparison_calls.append(old_texts)
        if self.hook is not None:
            await self.hook(len(self.comparison_calls))
        if self.invalid:
            return {"decisions": []}
        return {
            "decisions": [
                {"id": pair["id"], "verdict": self.verdicts.get(text, "coexists")}
                for pair, text in zip(payload["pairs"], old_texts, strict=True)
            ]
        }


class _ComparisonGate:
    def __init__(self, count: int = 1) -> None:
        self.ready = [asyncio.Event() for _ in range(count)]
        self.resume = [asyncio.Event() for _ in range(count)]

    async def __call__(self, number: int) -> None:
        if number <= len(self.ready):
            self.ready[number - 1].set()
            await self.resume[number - 1].wait()


@asynccontextmanager
async def _workspace(
    database: PgDatabase, storage_type: type[PostgresStorage] = PostgresStorage
) -> AsyncIterator[tuple[Space, PostgresStorage]]:
    async with pool(database.runtime, max_size=1) as connections:
        scope = space()
        storage = storage_type(connections, owner_id=scope.owner_id)
        await storage.create_space(scope)
        yield scope, storage


def _memory(
    scope: Space, storage: Storage, model: FakeLLM, embedder: FakeEmbedder
) -> Memory:
    return Memory(
        space_id=scope.id,
        storage=storage,
        llm=model,
        embedder=embedder,
        clock=FakeClock(),
        ids=_Ids(),
    )


def test_remember_commits_messages_facts_indexes_and_audit(
    database: PgDatabase,
) -> None:
    """公开用例提交原文、事实、证据、索引、审计及依赖画像清理。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目", type="project")
            old = replace(
                fact(project, "old", text="星河项目默认用中文"),
                predicate=_PREDICATE,
                object=LiteralValue("中文"),
                valid_at=10.0,
            )
            model, embedder = _Model(verdicts={old.text: "conflict"}), _Embedder()
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            history = tuple(
                message(
                    scope,
                    f"sent-{index}",
                    session=session_id,
                    content=f"历史消息 {index}",
                )
                for index in range(12)
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.append_messages(history)
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)
                await writer.save_profile(
                    Profile(
                        scope.id,
                        {"language": (ProfileEntry(old.text, (old.id,)),)},
                        10.0,
                    )
                )

            result = await memory.remember(
                (
                    MessageInput("user", _CURRENT, 50.0),
                    MessageInput("assistant", "收到"),
                ),
                session_id=session_id,
            )

            assert result.message_ids == (
                MessageId("incoming-0"),
                MessageId("incoming-1"),
            )
            created, cutoff = result.fact_changes
            assert created.before is None and created.after.valid_at == 50.0
            assert created.after.provenance == (result.message_ids[0],)
            assert (
                cutoff.before,
                cutoff.after.invalid_at,
                cutoff.after.expired_at,
            ) == (old, 50.0, 100.0)
            assert cutoff.cutoff_triggers == (
                CutoffTrigger(created.after.id, 50.0, (result.message_ids[0],)),
            )
            assert created.cursor < cutoff.cursor
            assert model.comparison_calls == [(old.text,)]
            assert embedder.calls == [(_CURRENT,)]
            (extraction,) = model.extraction_calls
            rows = cast(list[dict[str, object]], extraction["previous"])
            assert [row["number"] for row in rows] == list(range(1, 11))
            assert [row["content"] for row in rows] == [
                item.content for item in history[-10:]
            ]
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(old.id) == cutoff.after
                assert await reader.get_fact(created.after.id) == created.after
                assert (await reader.list_fact_changes(old.id))[0] == cutoff
                assert (
                    tuple(row.id for row in await reader.last_messages(session_id, 2))
                    == result.message_ids
                )
                assert await reader.get_entity(project.id) == project
                assert await reader.get_profile() is None
                filters = FactFilter()
                assert created.after.id in dict(
                    await reader.search_lexical(
                        _CURRENT, 10, filters=filters, reference_time=100.0
                    )
                )
                assert await reader.search_vectors(
                    (1.0, 0.0),
                    10,
                    filters=filters,
                    reference_time=100.0,
                    embedding_space=embedder.embedding_space,
                ) == ((created.after.id, 1.0),)
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_fact_support_groups(
                    (old.id, created.after.id)
                ) == {
                    old.id: (),
                    created.after.id: ((result.message_ids[0],),),
                }

    run(exercise())


@pytest.mark.parametrize("empty", [True, False], ids=["zero-facts", "undecided"])
def test_remember_can_commit_only_messages(database: PgDatabase, empty: bool) -> None:
    """合法空抽取或不确定裁决仍提交原文，画像依赖保持有效。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(fact(project, "old"), predicate=_PREDICATE, valid_at=10.0)
            model, embedder = (
                _Model(verdicts={old.text: "unknown"}, empty=empty),
                _Embedder(),
            )
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            profile = Profile(
                scope.id, {"data": (ProfileEntry(old.text, (old.id,)),)}, 10.0
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)
                await writer.save_profile(profile)

            result = await memory.remember(_CURRENT, session_id=session_id)

            assert result.fact_changes == ()
            assert len(model.extraction_calls) == 1
            assert model.comparison_calls == ([] if empty else [(old.text,)])
            assert embedder.calls == ([] if empty else [(_CURRENT,)])
            async with storage.session(scope.id) as reader:
                assert [
                    row.id for row in await reader.last_messages(session_id, 10)
                ] == list(result.message_ids)
                assert await reader.get_profile() == profile
                assert await reader.query_facts(FactQuery(), reference_time=100.0) == (
                    old,
                )

    run(exercise())


@pytest.mark.parametrize(
    "sensitive", [False, True], ids=["support-only", "sensitivity"]
)
def test_remember_adds_erasable_evidence_to_manual_fact(
    database: PgDatabase, sensitive: bool
) -> None:
    """手工事实可补证，擦除会话后保留事实和独立于原文的审计。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(
                fact(project, "old", text=_CURRENT),
                predicate=_PREDICATE,
                object=LiteralValue("英文"),
                valid_at=100.0,
            )
            model = _Model(verdicts={old.text: "duplicate"}, sensitive=sensitive)
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            profile = Profile(
                scope.id, {"data": (ProfileEntry(old.text, (old.id,)),)}, 100.0
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                (created,) = await writer.save_facts((old,), changed_at=100.0)
                await writer.save_profile(profile)

            result = await memory.remember(_CURRENT, session_id=session_id)

            (change,) = result.fact_changes
            assert change.before == old
            assert change.after == replace(
                old, provenance=result.message_ids, sensitive=sensitive
            )
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_fact_support_groups((old.id,)) == {
                    old.id: (result.message_ids,)
                }
                assert (await writer.list_fact_changes(old.id))[0] == change
                assert await writer.get_profile() == (None if sensitive else profile)

                assert await writer.delete_session_data(session_id) == frozenset()
                assert not await writer.has_session(session_id)
                assert await writer.get_fact(old.id) == replace(
                    old, sensitive=sensitive
                )
                assert await writer.get_fact_support_groups((old.id,)) == {old.id: ()}
                assert await writer.list_fact_changes(old.id) == (created,)
                assert await writer.get_profile() == (None if sensitive else profile)

    run(exercise())


@pytest.mark.parametrize("second_update", ["none", "added", "removed"])
def test_remember_rejudges_only_changed_pairs(
    database: PgDatabase, second_update: str
) -> None:
    """复用原输入及已裁决的配对，并允许第二次复核在锁内处理移除。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            stable = replace(
                fact(project, "a-stable", text="初次比较的事实"),
                predicate=_PREDICATE,
                valid_at=10.0,
            )
            added = replace(
                stable, id=FactId("b-added"), text="首次比较后加入的事实", valid_at=20.0
            )
            newer = replace(
                stable, id=FactId("c-added"), text="重判期间加入的事实", valid_at=30.0
            )
            gate = _ComparisonGate(1 if second_update == "none" else 2)
            model, embedder = (
                _Model(verdicts={added.text: "conflict"}, hook=gate),
                _Embedder(),
            )
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((stable,), changed_at=10.0)

            async def compete() -> None:
                await gate.ready[0].wait()
                async with storage.session(scope.id, write=True) as writer:
                    await writer.save_facts((added,), changed_at=20.0)
                gate.resume[0].set()
                if second_update != "none":
                    await gate.ready[1].wait()
                    async with storage.session(scope.id, write=True) as writer:
                        if second_update == "added":
                            await writer.save_facts((newer,), changed_at=30.0)
                        else:
                            await writer.delete_facts((added.id,))
                    gate.resume[1].set()

            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                tasks.create_task(compete())
                if second_update == "added":
                    with pytest.raises(RememberRetryRequired):
                        await memory.remember(_CURRENT, session_id=session_id)
                else:
                    result = await memory.remember(_CURRENT, session_id=session_id)
                    assert result.message_ids == (MessageId("incoming-0"),)
                    assert result.fact_changes[0].after.id == FactId("new-0")
                    if second_update == "none":
                        assert len(result.fact_changes) == 2
                        assert result.fact_changes[1].after.id == added.id
                        assert result.fact_changes[1].after.invalid_at == 100.0
                    else:
                        assert len(result.fact_changes) == 1
            assert len(model.extraction_calls) == 1
            assert embedder.calls == [(_CURRENT,)]
            assert model.comparison_calls == [(stable.text,), (added.text,)]
            async with storage.session(scope.id) as reader:
                committed = await reader.last_messages(session_id, 10)
                assert [row.id for row in committed] == (
                    [] if second_update == "added" else [MessageId("incoming-0")]
                )
                assert (await reader.get_fact(FactId("new-0")) is None) == (
                    second_update == "added"
                )
                if second_update == "added":
                    assert await reader.get_fact(added.id) == added

    run(exercise())


def test_remember_rejudges_newly_ranked_fact_and_drops_evicted_conflict(
    database: PgDatabase,
) -> None:
    """同槽候选换榜后仅补判入榜的新事实，出榜项原有的矛盾判定不再提交。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            original = tuple(
                replace(
                    fact(project, f"old-{index}", text=f"候选说明 {index}"),
                    predicate=_PREDICATE,
                    valid_at=10.0,
                )
                for index in range(10)
            )
            evicted = original[-1]
            added = replace(original[0], id=FactId("late"), text=_CURRENT)
            gate = _ComparisonGate()
            model = _Model(verdicts={evicted.text: "conflict"}, hook=gate)
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts(original, changed_at=10.0)

            async def compete() -> None:
                await gate.ready[0].wait()
                async with storage.session(scope.id, write=True) as writer:
                    await writer.save_facts((added,), changed_at=20.0)
                gate.resume[0].set()

            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                tasks.create_task(compete())
                result = await memory.remember(_CURRENT, session_id=session_id)

            assert model.comparison_calls == [
                tuple(item.text for item in original),
                (added.text,),
            ]
            (change,) = result.fact_changes
            assert change.after.id == FactId("new-0")
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(evicted.id) == evicted

    run(exercise())


@pytest.mark.parametrize("update", ["metadata", "shorten", "disjoint"])
def test_remember_revalidates_time_and_reads_latest_support_groups(
    database: PgDatabase, update: str
) -> None:
    """有效区间变更按最新范围裁决，证据变化沿用比较结果。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(
                fact(project, "old", text=_CURRENT),
                predicate=_PREDICATE,
                object=LiteralValue("英文"),
                valid_at=10.0,
            )
            verdict: Verdict = "duplicate" if update == "metadata" else "unknown"
            gate = _ComparisonGate()
            model = _Model(verdicts={old.text: verdict}, hook=gate)
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            evidence = message(scope, "evidence", session=session_id)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)

            async def compete() -> None:
                await gate.ready[0].wait()
                async with storage.session(scope.id, write=True) as writer:
                    if update == "metadata":
                        await writer.append_messages((evidence,))
                        await writer.save_facts(
                            (replace(old, sensitive=True, provenance=(evidence.id,)),),
                            changed_at=105.0,
                            support_groups={old.id: ((evidence.id,),)},
                        )
                    else:
                        cutoff = 20.0 if update == "shorten" else 100.0
                        await writer.save_facts(
                            (old.close(cutoff, 105.0),), changed_at=105.0
                        )
                gate.resume[0].set()

            event_time = 100.0 if update == "disjoint" else 10.0
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                tasks.create_task(compete())
                result = await memory.remember(
                    (MessageInput("user", _CURRENT, event_time),), session_id=session_id
                )
            assert model.comparison_calls == (
                [(old.text,), (old.text,)] if update == "shorten" else [(old.text,)]
            )
            if update == "metadata":
                (change,) = result.fact_changes
                assert change.after.sensitive and change.after.id == old.id
                async with storage.session(scope.id, write=True) as writer:
                    assert await writer.get_fact_support_groups((old.id,)) == {
                        old.id: ((evidence.id,), (result.message_ids[0],)),
                    }
            elif update == "shorten":
                assert result.fact_changes == ()
            else:
                (change,) = result.fact_changes
                assert change.before is None and change.after.valid_at == event_time

    run(exercise())


@pytest.mark.parametrize(
    "error",
    [InvalidStructuredOutput, CompletionTruncated],
    ids=["missing", "truncated"],
)
def test_remember_does_not_write_when_a_comparison_shard_fails(
    database: PgDatabase, error: type[Exception]
) -> None:
    """同一锚点的第一片成功、第二片失败时，原文和事实均未写入。"""

    class Model(_Model):
        async def complete_json(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> Mapping[str, object]:
            response = await super().complete_json(
                system=system, prompt=prompt, schema=schema
            )
            if "facts" in response:
                (template,) = cast(list[dict[str, object]], response["facts"])
                return {
                    "facts": [
                        {**template, "text": f"星河项目设置 {index}"}
                        for index in range(32)
                    ]
                }
            if json.loads(prompt)["pairs"][0]["id"] == "31":
                if error is CompletionTruncated:
                    raise CompletionTruncated("测试裁决分片被截断")
                return {"decisions": []}
            return response

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            model = Model()
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            with pytest.raises(error):
                await memory.remember(_CURRENT, session_id=session_id)
            assert [len(call) for call in model.comparison_calls[:2]] == [30, 1]
            async with storage.session(scope.id) as reader:
                assert not await reader.last_messages(session_id, 10)
                assert not await reader.query_facts(
                    FactQuery(FactFilter(state="all")), reference_time=100.0
                )

    run(exercise())


def test_remember_writes_disjoint_same_slot_facts_without_comparison(
    database: PgDatabase,
) -> None:
    """同槽相邻区间的两条新事实分别入库，无需比较或截止。"""

    class Model(_Model):
        async def complete_json(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> Mapping[str, object]:
            response = await super().complete_json(
                system=system, prompt=prompt, schema=schema
            )
            if "facts" not in response:
                return response
            (template,) = cast(list[dict[str, object]], response["facts"])
            (source,) = cast(list[int], template["source_numbers"])

            def time_evidence(seconds: int) -> dict[str, object]:
                return {
                    "kind": "absolute",
                    "source_number": source,
                    "value": f"1970-01-01T00:00:{seconds:02d}Z",
                    "days": None,
                }

            return {
                "facts": [
                    {
                        **template,
                        "object": {"kind": "literal", "value": language},
                        "text": f"星河项目在第 {start} 至 {end} 秒使用{language}",
                        "valid_at_evidence": time_evidence(start),
                        "invalid_at_evidence": time_evidence(end),
                    }
                    for language, start, end in (("中文", 10, 20), ("英文", 20, 30))
                ]
            }

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            model = Model()
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            result = await memory.remember(
                "分段回顾星河项目的用语", session_id=session_id
            )
            assert result.message_ids == (MessageId("incoming-0"),)
            assert model.comparison_calls == []
            assert all(not change.cutoff_triggers for change in result.fact_changes)
            async with storage.session(scope.id) as reader:
                saved = await reader.query_facts(
                    FactQuery(FactFilter(state="all")), reference_time=100.0
                )
            assert [(row.object, row.valid_at, row.invalid_at) for row in saved] == [
                (LiteralValue("中文"), 10.0, 20.0),
                (LiteralValue("英文"), 20.0, 30.0),
            ]

    run(exercise())


@pytest.mark.parametrize("phase", ["extract", "embed", "compare"])
@pytest.mark.parametrize(
    "erase_current", [True, False], ids=["session", "other-session"]
)
def test_remember_rejects_inflight_erasure(
    database: PgDatabase, erase_current: bool, phase: str
) -> None:
    """原会话撤销和同空间擦除均阻止后续推理与提交。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(fact(project, "old"), predicate=_PREDICATE, valid_at=10.0)
            gate = _ComparisonGate()
            model = _Model(
                hook=gate if phase == "compare" else None,
                extraction_hook=gate if phase == "extract" else None,
            )
            embedder = _Embedder(gate if phase == "embed" else None)
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            erased_id = session_id if erase_current else await memory.create_session()
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)
                await writer.append_messages(
                    (message(scope, "erased", session=erased_id),)
                )

            async def erase() -> None:
                await gate.ready[0].wait()
                async with storage.session(scope.id, write=True) as writer:
                    await writer.delete_session_data(erased_id)
                gate.resume[0].set()

            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                tasks.create_task(erase())
                with pytest.raises(
                    SessionUnavailable if erase_current else RememberInvalidated
                ):
                    await memory.remember(_CURRENT, session_id=session_id)
            assert embedder.calls == ([] if phase == "extract" else [(_CURRENT,)])
            assert model.comparison_calls == (
                [(old.text,)] if phase == "compare" else []
            )
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((MessageId("incoming-0"),)) == {}
                assert await reader.query_facts(FactQuery(), reference_time=100.0) == (
                    old,
                )
                assert (
                    await reader.search_vectors(
                        (1.0, 0.0),
                        10,
                        filters=FactFilter(),
                        reference_time=100.0,
                        embedding_space=embedder.embedding_space,
                    )
                    == ()
                )

    run(exercise())


def test_remember_rejects_unavailable_space_or_session_before_inference(
    database: PgDatabase,
) -> None:
    """未授权空间和其他空间的会话不能进入推理。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            model, embedder = _Model(), _Embedder()
            async with pool(database.runtime, max_size=1) as connections:
                outsider = PostgresStorage(connections, owner_id="outsider")
                unauthorized = _memory(scope, outsider, model, embedder)
                with pytest.raises(LookupError, match="空间不可访问"):
                    await unauthorized.remember(
                        _CURRENT, session_id=SessionId("unavailable")
                    )
            sibling = space()
            await storage.create_space(sibling)
            sibling_memory = _memory(sibling, storage, model, embedder)
            foreign_session = await sibling_memory.create_session()
            memory = _memory(scope, storage, model, embedder)
            with pytest.raises(SessionUnavailable):
                await memory.remember(_CURRENT, session_id=foreign_session)
            assert model.extraction_calls == []
            assert embedder.calls == []

    run(exercise())


@pytest.mark.parametrize("fault", ["comparison", "support-groups", "audit", "vector"])
def test_remember_rolls_back_failed_writes(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """推理错误和事务中途故障均不留下这批原文或派生数据。"""

    async def exercise() -> None:
        async with _workspace(database) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(fact(project, "old"), predicate=_PREDICATE, valid_at=10.0)
            model, embedder = (
                _Model(verdicts={old.text: "conflict"}, invalid=fault == "comparison"),
                _Embedder(),
            )
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            profile = Profile(
                scope.id, {"data": (ProfileEntry(old.text, (old.id,)),)}, 10.0
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)
                await writer.save_profile(profile)

            async def audit_fault(
                conn: AsyncConnection[TupleRow],
                changes: Sequence[FactWrite],
                changed_at: float,
            ) -> None:
                assert await write_fact_batch(conn, changes, changed_at)
                raise RuntimeError("合成审计故障")

            save_vectors = PostgresSession.upsert_vectors

            async def vector_fault(
                writer: PostgresSession,
                items: Sequence[tuple[FactId, Sequence[float]]],
                *,
                embedding_space: str,
            ) -> None:
                await save_vectors(writer, items, embedding_space=embedding_space)
                raise RuntimeError("合成向量故障")

            async def incomplete_groups(
                writer: PostgresSession, fact_ids: Sequence[FactId]
            ) -> Mapping[FactId, SupportGroups]:
                return {}

            if fault == "audit":
                monkeypatch.setattr(postgres_session, "write_fact_batch", audit_fault)
            if fault == "vector":
                monkeypatch.setattr(PostgresSession, "upsert_vectors", vector_fault)
            if fault == "support-groups":
                monkeypatch.setattr(
                    PostgresSession, "get_fact_support_groups", incomplete_groups
                )
            if fault == "comparison":
                with pytest.raises(InvalidStructuredOutput):
                    await memory.remember(_CURRENT, session_id=session_id)
            else:
                with pytest.raises(RuntimeError, match="证据组读取不完整|合成.*故障"):
                    await memory.remember(_CURRENT, session_id=session_id)

            async with storage.session(scope.id) as reader:
                assert await reader.last_messages(session_id, 10) == ()
                assert await reader.query_facts(FactQuery(), reference_time=100.0) == (
                    old,
                )
                assert (
                    await reader.search_vectors(
                        (1.0, 0.0),
                        10,
                        filters=FactFilter(),
                        reference_time=100.0,
                        embedding_space=embedder.embedding_space,
                    )
                    == ()
                )
                assert len(await reader.list_fact_changes(old.id)) == 1
                assert await reader.get_profile() == profile

    run(exercise())


@pytest.mark.parametrize(
    "fault", ["before", "timeout-before", "rejected", "unknown", "timeout-commit"]
)
def test_remember_propagates_commit_failure_without_resending(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """只有进入提交阶段后丢失确认时才返回结果未知。"""
    fail = False
    drop_ack = False
    attempts = 0
    deadline: asyncio.Timeout
    original_exit = AsyncTransaction.__aexit__

    async def timeout_request() -> None:
        deadline.reschedule(asyncio.get_running_loop().time())
        await asyncio.Future[None]()

    async def exit_transaction(
        transaction: AsyncTransaction,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        nonlocal drop_ack
        result = await original_exit(transaction, exc_type, exc_val, exc_tb)
        if drop_ack and transaction.status is transaction.Status.COMMITTED:
            drop_ack = False
            if fault == "timeout-commit":
                await timeout_request()
            raise errors.OperationalError("合成提交确认丢失")
        return result

    monkeypatch.setattr(AsyncTransaction, "__aexit__", exit_transaction)

    class CommitFaultStorage(PostgresStorage):
        @asynccontextmanager
        async def _session(
            self, space_id: SpaceId, write: bool
        ) -> AsyncIterator[SpaceReader]:
            nonlocal attempts, drop_ack
            async with super()._session(space_id, write) as reader:
                yield reader
                if write and fail:
                    attempts += 1
                    if fault == "before":
                        raise errors.ConnectionFailure("合成提交前连接断开")
                    if fault == "timeout-before":
                        await timeout_request()
                    if fault == "rejected":
                        conn = cast(PostgresSession, reader)._conn
                        await conn.execute("""
                            CREATE TEMP TABLE remember_commit_check (
                                id integer UNIQUE DEFERRABLE INITIALLY DEFERRED
                            ) ON COMMIT DROP
                            """)
                        await conn.execute(
                            "INSERT INTO remember_commit_check VALUES (1), (1)"
                        )
                    else:
                        drop_ack = True

    async def exercise() -> None:
        nonlocal deadline, fail
        async with _workspace(database, CommitFaultStorage) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(fact(project, "old"), predicate=_PREDICATE, valid_at=10.0)
            model = _Model(verdicts={old.text: "conflict"})
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()
            profile = Profile(
                scope.id, {"data": (ProfileEntry(old.text, (old.id,)),)}, 10.0
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((old,), changed_at=10.0)
                await writer.save_profile(profile)

            fail = True
            error = {
                "before": errors.ConnectionFailure,
                "timeout-before": TimeoutError,
                "rejected": errors.UniqueViolation,
                "unknown": CommitOutcomeUnknown,
                "timeout-commit": CommitOutcomeUnknown,
            }[fault]
            with pytest.raises(error) as failure:
                async with asyncio.timeout(None) as deadline:
                    await memory.remember(_CURRENT, session_id=session_id)
            ack_lost = fault in {"unknown", "timeout-commit"}
            if fault == "unknown":
                assert isinstance(failure.value.__cause__, errors.OperationalError)
            if fault == "timeout-commit":
                assert isinstance(failure.value.__cause__, asyncio.CancelledError)
            assert attempts == 1 and len(model.extraction_calls) == 1
            async with storage.session(scope.id) as reader:
                assert bool(await reader.last_messages(session_id, 10)) is ack_lost
                assert (await reader.get_profile() is None) is ack_lost
                assert (await reader.get_fact(FactId("new-0")) is not None) is ack_lost
                assert len(await reader.list_fact_changes(old.id)) == (
                    2 if ack_lost else 1
                )

    run(exercise())


def test_remember_returns_committed_result_when_pool_return_times_out(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已确认提交后遇到调用方超时，完成连接归还并返回实际变更。"""
    armed = False
    returning = False
    returned = False
    deadline: asyncio.Timeout
    original_putconn = AsyncConnectionPool.putconn

    async def putconn(
        connections: AsyncConnectionPool[AsyncConnection[TupleRow]],
        conn: AsyncConnection[TupleRow],
    ) -> None:
        nonlocal returning, returned
        if not returning:
            await original_putconn(connections, conn)
            return
        returning = False
        assert conn.info.transaction_status == TransactionStatus.IDLE
        loop = asyncio.get_running_loop()
        resume = asyncio.Event()
        deadline.reschedule(loop.time())
        loop.call_soon(resume.set)
        await resume.wait()
        await original_putconn(connections, conn)
        returned = True

    monkeypatch.setattr(AsyncConnectionPool, "putconn", putconn)

    class ReturnFaultStorage(PostgresStorage):
        @asynccontextmanager
        async def _session(
            self, space_id: SpaceId, write: bool
        ) -> AsyncIterator[SpaceReader]:
            nonlocal returning
            async with super()._session(space_id, write) as reader:
                yield reader
                returning = write and armed

    async def exercise() -> None:
        nonlocal armed, deadline
        async with _workspace(database, ReturnFaultStorage) as (scope, storage):
            model = _Model()
            memory = _memory(scope, storage, model, _Embedder())
            session_id = await memory.create_session()

            armed = True
            async with asyncio.timeout(None) as deadline:
                result = await memory.remember(_CURRENT, session_id=session_id)
            assert deadline.expired() and returned
            assert len(model.extraction_calls) == 1
            (change,) = result.fact_changes

            async with storage.session(scope.id) as reader:
                messages = await reader.last_messages(session_id, 10)
                assert tuple(message.id for message in messages) == result.message_ids
                assert await reader.get_fact(change.after.id) == change.after
                assert await reader.list_fact_changes(change.after.id) == (change,)

    run(exercise())


def test_remember_records_write_lock_duration_for_long_timeline(
    database: PgDatabase,
    record_property: Callable[[str, object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重叠且带向量的同槽历史触发混合排序，记录初查与持锁复核耗时。"""
    durations: list[tuple[float, float]] = []
    lookup_durations: list[float] = []
    reused_pairs: list[BatchPairs | None] = []
    probes: list[tuple[int, int]] = []
    query_facts = PostgresReader.query_facts
    vector = (1.0,) + (0.0,) * 1023

    class Embedder(FakeEmbedder):
        @property
        def dim(self) -> int:
            return len(vector)

        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return (vector,) * len(texts)

    async def measure_lookup(
        reader: SpaceReader,
        prepared: Sequence[PreparedFact],
        *,
        embedding_space: str,
        reference_time: float,
        batch_pairs: BatchPairs | None = None,
    ) -> ComparisonCandidates:
        start = perf_counter()
        result = await find_comparison_candidates(
            reader,
            prepared,
            embedding_space=embedding_space,
            reference_time=reference_time,
            batch_pairs=batch_pairs,
        )
        lookup_durations.append((perf_counter() - start) * 1000)
        reused_pairs.append(batch_pairs)
        return result

    async def measure_probe(
        reader: PostgresReader, spec: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        rows = await query_facts(reader, spec, reference_time=reference_time)
        probes.append((spec.limit, len(rows)))
        return rows

    monkeypatch.setattr(
        remember_application, "find_comparison_candidates", measure_lookup
    )
    monkeypatch.setattr(PostgresReader, "query_facts", measure_probe)

    class TimedStorage(PostgresStorage):
        @asynccontextmanager
        async def _session(
            self, space_id: SpaceId, write: bool
        ) -> AsyncIterator[SpaceReader]:
            start = perf_counter()
            async with super()._session(space_id, write) as reader:
                acquired = perf_counter()
                yield reader
            if write:
                durations.append(
                    ((acquired - start) * 1000, (perf_counter() - acquired) * 1000)
                )

    async def exercise() -> None:
        async with _workspace(database, TimedStorage) as (scope, storage):
            project = Entity(scope.id, "星河项目")
            old = replace(
                fact(project, "old", text=_CURRENT), predicate=_PREDICATE, valid_at=1.0
            )
            history = tuple(
                replace(
                    old, id=FactId(f"history-{index:03}"), text=f"历史安排 {index}"
                ).close(120.0, 20.0)
                for index in range(250)
            )
            cross_slot = tuple(
                replace(
                    fact(project, f"vector-{index:03}", text=f"发布附录 {index}"),
                    predicate=Predicate("appendix"),
                )
                for index in range(100)
            )
            gate = _ComparisonGate()
            model, embedder = _Model(hook=gate), Embedder()
            memory = _memory(scope, storage, model, embedder)
            session_id = await memory.create_session()
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(project)
                await writer.save_facts((*history, old, *cross_slot), changed_at=30.0)
                await writer.upsert_vectors(
                    tuple((item.id, vector) for item in (*history, old, *cross_slot)),
                    embedding_space=embedder.embedding_space,
                )
            durations.clear()

            async def competing_writer() -> None:
                await gate.ready[0].wait()
                async with pool(database.runtime, max_size=2) as connections:
                    other = PostgresStorage(connections, owner_id=scope.owner_id)
                    async with other.session(scope.id, write=True):
                        gate.resume[0].set()
                        async with connections.connection() as observer:
                            await observer.set_autocommit(True)
                            while True:
                                rows = await observer.execute(
                                    """
                                    SELECT 1 FROM pg_stat_activity
                                    WHERE pid <> pg_backend_pid()
                                      AND wait_event_type = 'Lock'
                                      AND query LIKE %s
                                    """,
                                    ("%mnemotrace_next.spaces%FOR UPDATE%",),
                                )
                                if await rows.fetchone() is not None:
                                    break
                                await asyncio.sleep(0.01)

            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                tasks.create_task(competing_writer())
                write = tasks.create_task(
                    memory.remember(_CURRENT, session_id=session_id)
                )
            result = write.result()

            assert len(result.fact_changes) == 1
            assert model.comparison_calls == [
                tuple(item.text for item in (*history[:9], old, *cross_slot[:10]))
            ]
            assert probes == [(11, 11), (11, 11)]
            initial_pairs, rechecked_pairs = reused_pairs
            assert initial_pairs is not None and initial_pairs is rechecked_pairs
            ((acquire, held),) = durations
            lookup, recheck = lookup_durations
            record_property("candidate_lookup_ms", round(lookup, 3))
            record_property("write_lock_candidate_recheck_ms", round(recheck, 3))
            record_property("write_lock_acquire_ms", round(acquire, 3))
            record_property("write_lock_held_ms", round(held, 3))

    run(exercise())
