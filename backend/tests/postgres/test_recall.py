"""通过公开 Python 入口验证 PostgreSQL 召回。"""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from mnemotrace_next import Memory, RecallEvidence
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.reader import PostgresReader
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import FactId
from mnemotrace_next.ports.queries import FactFilter
from tests.ports.fakes import FakeClock, FakeEmbedder, FakeIdGenerator, FakeLLM

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_recall_applies_time_and_visibility_before_candidate_limits(
    database: PgDatabase,
) -> None:
    """历史边界、敏感事实和空间隔离在两路候选阶段生效。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope, other = space(), space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            embedder, clock = FakeEmbedder(), FakeClock()
            await storage.create_space(scope)
            await storage.create_space(other)

            subject = Entity(scope.id, "合成主体")
            source = message(scope, "source", content="发布规范 MT-042")
            text = source.content
            current = replace(
                fact(subject, "current", text=text, provenance=(source.id,)),
                valid_at=100.0,
            )
            unknown = fact(subject, "unknown", text=text)
            stopped = replace(fact(subject, "stopped", text=text), valid_at=10.0).close(
                100.0, 100.0
            )
            future = replace(fact(subject, "future", text=text), valid_at=101.0)
            hidden = tuple(
                replace(
                    fact(subject, f"00-hidden-{index:02}", text=text),
                    valid_at=100.0,
                    sensitive=True,
                )
                for index in range(33)
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.upsert_entity(subject)
                await writer.save_facts(
                    (current, stopped, future, unknown, *hidden), changed_at=100.0
                )
                await writer.upsert_vectors(
                    tuple(
                        (item.id, (1.0, 0.0)) for item in (current, stopped, *hidden)
                    ),
                    embedding_space=embedder.embedding_space,
                )

            other_subject = Entity(other.id, "另一个合成主体")
            other_fact = replace(
                fact(other_subject, "00-foreign", text=text), valid_at=100.0
            )
            async with storage.session(other.id, write=True) as writer:
                await writer.upsert_entity(other_subject)
                await writer.save_facts((other_fact,), changed_at=100.0)
                await writer.upsert_vectors(
                    ((other_fact.id, (1.0, 0.0)),),
                    embedding_space=embedder.embedding_space,
                )

            owner = Memory(
                space_id=scope.id,
                storage=storage,
                llm=FakeLLM(),
                embedder=embedder,
                clock=clock,
                ids=FakeIdGenerator(),
            )
            assert (await owner.recall("MT-042", top_k=1)).evidence[
                0
            ].fact_id == hidden[0].id

            restricted = Memory.for_recall(
                space_id=scope.id,
                storage=storage,
                embedder=embedder,
                clock=clock,
                exclude_sensitive=True,
            )
            assert [
                item.fact_id for item in (await restricted.recall("MT-042")).evidence
            ] == [current.id, unknown.id]
            assert (await restricted.recall("MT-042", as_of=100.0)).evidence == (
                RecallEvidence(current.id, text, 100.0, None, (source.id,)),
            )
            assert (await restricted.recall("MT-042", as_of=99.0)).evidence == (
                RecallEvidence(stopped.id, text, 10.0, 100.0, ()),
            )
            assert (await restricted.recall("上线前看哪些记录", top_k=1)).evidence == (
                RecallEvidence(current.id, text, 100.0, None, (source.id,)),
            )

    run(exercise())


def test_recall_checks_owner_before_embedding(database: PgDatabase) -> None:
    """其他账号的空间在发送查询之前被拒绝。"""

    class UnexpectedEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            raise AssertionError("未授权请求不能进入嵌入端口")

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            owner = PostgresStorage(connections, owner_id=scope.owner_id)
            other = PostgresStorage(connections, owner_id="other")
            await owner.create_space(scope)
            reader = Memory.for_recall(
                space_id=scope.id,
                storage=other,
                embedder=UnexpectedEmbedder(),
                clock=FakeClock(),
            )
            with pytest.raises(LookupError, match="空间不可访问"):
                await reader.recall("合成查询")

    run(exercise())


def test_recall_searches_and_loads_facts_in_one_snapshot(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """词法命中后并发截止事实，不改变本次召回读取的区间。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope, embedder = space(), FakeEmbedder()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            subject = Entity(scope.id, "合成主体")
            current = replace(fact(subject, "current", text="发布规范"), valid_at=10.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((current,), changed_at=10.0)
                await writer.upsert_vectors(
                    ((current.id, (1.0, 0.0)),),
                    embedding_space=embedder.embedding_space,
                )

            original_search = PostgresReader.search_lexical
            updated = False

            async def search_then_update(
                reader: PostgresReader,
                query: str,
                limit: int,
                *,
                filters: FactFilter,
                reference_time: float,
                min_score: float | None = None,
            ) -> Sequence[tuple[FactId, float]]:
                nonlocal updated
                hits = await original_search(
                    reader,
                    query,
                    limit,
                    filters=filters,
                    reference_time=reference_time,
                    min_score=min_score,
                )
                if not updated:
                    async with storage.session(scope.id, write=True) as writer:
                        await writer.save_facts(
                            (current.close(50.0, 100.0),), changed_at=100.0
                        )
                    updated = True
                return hits

            monkeypatch.setattr(PostgresReader, "search_lexical", search_then_update)
            recall = Memory.for_recall(
                space_id=scope.id, storage=storage, embedder=embedder, clock=FakeClock()
            )
            assert (await recall.recall("发布规范")).evidence == (
                RecallEvidence(current.id, current.text, 10.0, None, ()),
            )
            assert (await recall.recall("发布规范")).evidence == ()

    run(exercise())


def test_recall_keeps_authorized_evidence_when_erased_during_reranking(
    database: PgDatabase,
) -> None:
    """重排等待时释放连接，擦除后新请求读取新快照。"""

    async def exercise() -> None:
        async with pool(database.runtime, max_size=1) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            subject = Entity(scope.id, "合成主体")
            source = message(scope, "source", content="发布规范")
            first = fact(
                subject, "visible-a", text=source.content, provenance=(source.id,)
            )
            second = replace(first, id=FactId("visible-b"))
            hidden = replace(first, id=FactId("hidden"), sensitive=True)
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.upsert_entity(subject)
                await writer.save_facts((first, second, hidden), changed_at=100.0)

            started, resume = asyncio.Event(), asyncio.Event()

            class PausedReranker:
                async def rerank(
                    self,
                    query: str,
                    documents: Sequence[tuple[FactId, str]],
                    limit: int,
                ) -> Sequence[FactId]:
                    assert tuple(documents) == (
                        (first.id, first.text),
                        (second.id, second.text),
                    )
                    started.set()
                    await resume.wait()
                    return (second.id,)

            reader = Memory.for_recall(
                space_id=scope.id,
                storage=storage,
                embedder=FakeEmbedder(),
                clock=FakeClock(),
                exclude_sensitive=True,
                reranker=PausedReranker(),
            )
            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                inflight = tasks.create_task(
                    reader.recall("发布规范", top_k=1, rerank=True)
                )
                await started.wait()
                async with storage.session(scope.id, write=True) as writer:
                    deleted = await writer.delete_session_data(source.session_id)
                    assert deleted == {first.id, second.id, hidden.id}
                fresh = await reader.recall("发布规范", rerank=True)
                assert fresh.evidence == ()
                assert fresh.degraded_reason is None
                resume.set()

            result = inflight.result()
            assert result.evidence == (
                RecallEvidence(second.id, second.text, None, None, (source.id,)),
            )
            assert result.degraded_reason is None

    run(exercise())
