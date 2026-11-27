"""验证滚动摘要在运行角色下的调度、发布和并发恢复。"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from unittest.mock import AsyncMock, PropertyMock

import psycopg
import pytest

from mnemotrace import worker
from mnemotrace.adapters.inference import InferenceClients
from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres import session as pg_session
from mnemotrace.adapters.postgres import storage as pg_storage
from mnemotrace.adapters.postgres.reader import PostgresReader
from mnemotrace.adapters.postgres.session_summary_refresh import (
    PostgresSessionSummaryRefresh,
)
from mnemotrace.adapters.runtime import UuidGenerator
from mnemotrace.application.operations import LeaseLost
from mnemotrace.application.remember import MessageInput, Remember
from mnemotrace.application.session_summary_refresh import SessionSummaryRunner
from mnemotrace.domain.ids import SessionId
from mnemotrace.domain.space import Space
from mnemotrace.ports.inference import CompletionRefused
from mnemotrace.ports.storage import CommitOutcomeUnknown, SpaceUnavailable
from mnemotrace.runtime_settings import RuntimeSettings

from ..ports.fakes import FakeClock, FakeEmbedder, FakeLLM
from .conftest import PgDatabase
from .sample import SESSION, message, pool, run, space

pytestmark = pytest.mark.postgres


class _Model(FakeLLM):
    def __init__(
        self, *, blocked: bool = False, failure: Exception | None = None
    ) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.failure = failure
        self.prompts: list[dict[str, object]] = []
        self.extractions: list[dict[str, object]] = []

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        if schema["required"] != ["summary"]:
            self.extractions.append(json.loads(prompt))
            return {"facts": []}
        self.prompts.append(json.loads(prompt))
        self.entered.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return {"summary": "已归纳。"}


@dataclass(frozen=True)
class _Scope:
    database: PgDatabase
    space: Space
    storage: PostgresStorage
    queue: PostgresSessionSummaryRefresh

    def runner(self, model: _Model) -> SessionSummaryRunner:
        return SessionSummaryRunner(
            self.queue, lambda _: self.storage, model, worker._is_transient
        )

    async def enable(self, enabled: bool) -> None:
        updated = await self.storage.update_space(
            self.space.id, name=None, memory_decay=None, session_summary_enabled=enabled
        )
        assert updated.session_summary_enabled is enabled

    async def append(self, *numbers: int, session: SessionId = SESSION) -> None:
        async with self.storage.session(self.space.id, write=True) as writer:
            await writer.append_messages(
                tuple(
                    message(
                        self.space,
                        f"{session.value}-{number}",
                        session=session,
                        content="长" * 15_000 if number == 0 else str(number),
                    )
                    for number in numbers
                )
            )

    def state(self, session: SessionId = SESSION) -> psycopg.rows.TupleRow | None:
        with psycopg.connect(self.database.admin) as conn:
            return conn.execute(
                """
                SELECT status, summary_text, covered_through_seq, available_at, followup_at,
                    attempts, error_code, updated_at
                FROM mnemotrace.session_summaries WHERE space_id = %s AND session_id = %s
                """,
                (self.space.id.value, session.value),
            ).fetchone()

    def make_due(self) -> None:
        with psycopg.connect(self.database.admin) as conn:
            conn.execute(
                """
                UPDATE mnemotrace.session_summaries SET
                    available_at = CASE WHEN status = 'queued' THEN clock_timestamp() END,
                    lease_until = CASE WHEN status = 'running' THEN clock_timestamp() END
                WHERE space_id = %s AND session_id = %s AND status IN ('queued', 'running')
                """,
                (self.space.id.value, SESSION.value),
            )

    def remaining_wait(self) -> float:
        with psycopg.connect(self.database.admin) as conn:
            row = conn.execute(
                """
                SELECT EXTRACT(EPOCH FROM available_at - clock_timestamp())::float8
                FROM mnemotrace.session_summaries WHERE space_id = %s AND session_id = %s
                """,
                (self.space.id.value, SESSION.value),
            ).fetchone()
        assert row is not None and isinstance(row[0], float)
        return row[0]


@asynccontextmanager
async def _scope(
    database: PgDatabase, *, enabled: bool = True
) -> AsyncIterator[_Scope]:
    async with pool(database.runtime) as connections:
        target = space()
        storage = PostgresStorage(connections, owner_id=target.owner_id)
        await storage.create_space(target)
        async with storage.session(target.id, write=True) as writer:
            await writer.register_session(SESSION)
        scope = _Scope(
            database, target, storage, PostgresSessionSummaryRefresh(connections)
        )
        try:
            if enabled:
                await scope.enable(True)
            yield scope
        finally:
            with suppress(SpaceUnavailable):
                await storage.delete_space(target.id)


def test_append_threshold_uses_session_rows_and_includes_writes_without_facts(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope, _scope(database, enabled=False) as other:
            await scope.append(*range(1, 5))
            await other.append(*range(1, 30))
            assert other.state() is None
            await scope.append(*range(5, 11))
            assert scope.state() is None
            await scope.append(11)
            first = scope.state()
            assert first is not None and 55 < scope.remaining_wait() <= 60
            assert await scope.queue.claim() is None
            await scope.append(*range(12, 18))
            before = scope.state()
            assert before is not None and before[3] == first[3]
            await scope.append(17)
            assert scope.state() == before

            model = _Model()
            remember = Remember(
                space_id=scope.space.id,
                storage=scope.storage,
                llm=model,
                embedder=FakeEmbedder(),
                clock=FakeClock(),
                ids=UuidGenerator(),
            )
            await remember.remember((MessageInput("user", "18"),), session_id=SESSION)
            assert await scope.runner(model).run_once()
            assert model.prompts == [
                {
                    "previous_summary": None,
                    "messages": [
                        {"role": "user", "content": str(i)} for i in range(1, 9)
                    ],
                }
            ]
            saved = scope.state()
            assert saved is not None and saved[:2] == ("idle", "已归纳。")
            assert saved[2] > 8 and saved[7] is not None

    run(exercise())


def test_enable_waits_for_real_append_and_time_threshold_survives_runner_restart(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database, enabled=False) as scope:
            await scope.append(*range(1, 12))
            await scope.enable(True)
            await scope.append(11)
            assert scope.state() is None
            await scope.append(12)
            assert not await scope.runner(_Model()).run_once()
            scope.make_due()
            model = _Model()
            assert await scope.runner(model).run_once()
            assert model.prompts == [
                {
                    "previous_summary": None,
                    "messages": [
                        {"role": "user", "content": "1"},
                        {"role": "user", "content": "2"},
                    ],
                }
            ]

    run(exercise())


def test_disabled_space_skips_summary_read_and_scheduling(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        async with _scope(database, enabled=False) as scope:
            async with scope.storage.session(scope.space.id) as reader:
                with monkeypatch.context() as patch:
                    connection = PropertyMock(
                        side_effect=AssertionError("关闭时无需读取摘要表")
                    )
                    patch.setattr(PostgresReader, "_conn", connection)
                    assert await reader.get_session_summary(SESSION) is None
                    connection.assert_not_called()
            with monkeypatch.context() as patch:
                schedule = AsyncMock(
                    side_effect=AssertionError("关闭时无需登记摘要任务")
                )
                patch.setattr(pg_session, "schedule_summaries", schedule)
                await scope.append(*range(1, 19))
                schedule.assert_not_awaited()
            assert scope.state() is None

    run(exercise())


def test_remember_reads_only_enabled_nonempty_summary_in_the_same_session(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope, _scope(database) as other:
            await scope.append(*range(1, 19))
            model = _Model()
            assert await scope.runner(model).run_once()
            remember = Remember(
                space_id=scope.space.id,
                storage=scope.storage,
                llm=model,
                embedder=FakeEmbedder(),
                clock=FakeClock(),
                ids=UuidGenerator(),
            )
            await remember.remember("第十九条", session_id=SESSION)
            [first] = model.extractions
            assert first["session_summary"] == "已归纳。"
            previous = first["previous"]
            assert isinstance(previous, list)
            assert [row["content"] for row in previous] == [
                str(i) for i in range(9, 19)
            ]

            separate = SessionId("separate")
            async with scope.storage.session(scope.space.id, write=True) as writer:
                await writer.register_session(separate)
            await remember.remember("其他会话", session_id=separate)
            assert "session_summary" not in model.extractions[-1]
            async with other.storage.session(other.space.id) as reader:
                assert await reader.get_session_summary(SESSION) is None

            await scope.enable(False)
            await remember.remember("关闭后", session_id=SESSION)
            assert "session_summary" not in model.extractions[-1]
            await scope.enable(True)
            await remember.remember("重新开启后", session_id=SESSION)
            assert model.extractions[-1]["session_summary"] == "已归纳。"

    run(exercise())


def test_append_during_generation_has_own_deadline_after_current_target(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(18))
            model = _Model(blocked=True)
            async with asyncio.TaskGroup() as tasks:
                running = tasks.create_task(scope.runner(model).run_once())
                try:
                    await asyncio.wait_for(model.entered.wait(), 5)
                    await asyncio.wait_for(scope.append(18), 5)
                    in_flight = scope.state()
                    assert in_flight is not None and in_flight[0] == "running"
                    assert in_flight[4] is not None
                finally:
                    model.release.set()
            assert running.result()
            assert await scope.runner(model).run_once()
            assert model.prompts[1] == {
                "previous_summary": "已归纳。",
                "messages": [{"role": "user", "content": str(i)} for i in range(1, 8)],
            }
            queued = scope.state()
            assert (
                queued is not None
                and queued[0] == "queued"
                and queued[3] == in_flight[4]
            )
            assert not await scope.runner(model).run_once()
            scope.make_due()
            assert await scope.runner(model).run_once()
            assert model.prompts[2]["messages"] == [{"role": "user", "content": "8"}]

    run(exercise())


def test_expired_lease_can_be_reclaimed_without_overwriting_summary(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            first = await scope.queue.claim()
            assert first is not None and first.attempt == 1
            assert await scope.queue.claim() is None
            scope.make_due()
            second = await scope.queue.claim()
            assert (
                second is not None
                and second.attempt == 2
                and second.target_seq == first.target_seq
            )
            async with scope.storage.session(scope.space.id, write=True) as writer:
                with pytest.raises(LeaseLost):
                    await scope.queue.complete(
                        first, writer, first.target_seq, "失效摘要"
                    )
                await scope.queue.complete(
                    second, writer, second.target_seq, "接管摘要"
                )
            assert not await scope.queue.has_active_lease(first)
            state = scope.state()
            assert state is not None and state[:3] == (
                "idle",
                "接管摘要",
                second.target_seq,
            )

    run(exercise())


def test_retries_keep_last_good_summary_and_only_new_append_restarts_failure(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            assert await scope.runner(_Model()).run_once()
            initial = scope.state()
            assert initial is not None
            await scope.append(*range(19, 27))
            timed_out = _Model(failure=TimeoutError())
            runner = scope.runner(timed_out)
            for attempt, delay in enumerate((5, 10), 1):
                assert await runner.run_once()
                state = scope.state()
                assert state is not None and state[:3] == ("queued", *initial[1:3])
                assert (
                    state[5] == attempt and delay - 3 < scope.remaining_wait() <= delay
                )
                if attempt == 1:
                    await scope.append(27)
                    after = scope.state()
                    assert after is not None and after[3] == state[3]
                assert not await runner.run_once()
                scope.make_due()

            assert await runner.run_once()
            failed = scope.state()
            assert failed is not None and failed[:3] == ("failed", *initial[1:3])
            assert failed[3:7] == (None, None, 3, "operation_timeout")
            assert failed[7] == initial[7]
            assert timed_out.prompts[0] == timed_out.prompts[1] == timed_out.prompts[2]
            assert not await runner.run_once()
            await scope.append(27)
            assert not await runner.run_once()

            await scope.append(28)
            refused = _Model(failure=CompletionRefused())
            assert await scope.runner(refused).run_once()
            terminal = scope.state()
            assert terminal is not None and terminal[:3] == failed[:3]
            assert terminal[5:7] == (1, "invalid_model_output")
            assert not await scope.runner(refused).run_once()

    run(exercise())


def test_toggle_cancels_old_lease_even_if_enabled_again(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            assert await scope.runner(_Model()).run_once()
            saved = scope.state()
            assert saved is not None
            await scope.append(*range(19, 27))
            blocked = _Model(blocked=True)
            async with asyncio.TaskGroup() as tasks:
                old = tasks.create_task(scope.runner(blocked).run_once())
                try:
                    await asyncio.wait_for(blocked.entered.wait(), 5)
                    await scope.enable(False)
                    assert scope.state() == saved
                    await scope.append(27)
                    await scope.enable(True)
                    assert scope.state() == saved
                    assert await scope.queue.claim() is None
                    await scope.append(28)
                    queued = scope.state()
                    assert queued is not None and queued[0] == "queued"
                finally:
                    blocked.release.set()
            assert old.result()
            assert scope.state() == queued
            resumed = _Model()
            assert await scope.runner(resumed).run_once()
            assert resumed.prompts == [
                {
                    "previous_summary": "已归纳。",
                    "messages": [
                        {"role": "user", "content": str(i)} for i in range(9, 19)
                    ],
                }
            ]

    run(exercise())


@pytest.mark.parametrize("erase", ("session", "purge", "space"))
def test_erasure_deletes_summary_and_invalidates_paused_worker(
    database: PgDatabase, erase: str
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            assert await scope.runner(_Model()).run_once()
            await scope.append(*range(19, 27))
            blocked = _Model(blocked=True)
            async with asyncio.TaskGroup() as tasks:
                running = tasks.create_task(scope.runner(blocked).run_once())
                try:
                    await asyncio.wait_for(blocked.entered.wait(), 5)
                    if erase == "session":
                        async with scope.storage.session(
                            scope.space.id, write=True
                        ) as writer:
                            await writer.delete_session_data(SESSION)
                            await writer.register_session(SESSION)
                    elif erase == "purge":
                        await scope.storage.purge_space(
                            scope.space.id, key="synthetic-purge"
                        )
                        async with scope.storage.session(scope.space.id) as reader:
                            assert (await reader.get_space()).session_summary_enabled
                    else:
                        await scope.storage.delete_space(scope.space.id)
                    assert scope.state() is None
                finally:
                    blocked.release.set()
            assert running.result()
            assert scope.state() is None

    run(exercise())


@pytest.mark.parametrize("committed", (False, True))
def test_commit_outcome_is_resolved_from_original_lease(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            original = pg_storage._session_transaction

            @asynccontextmanager
            async def uncertain(
                conn: psycopg.AsyncConnection[psycopg.rows.TupleRow], *, write: bool
            ) -> AsyncIterator[None]:
                async with original(conn, write=write):
                    yield
                    if write and not committed:
                        raise CommitOutcomeUnknown("未提交")
                if write and committed:
                    raise CommitOutcomeUnknown("确认丢失")

            model = _Model()
            runner = scope.runner(model)
            with monkeypatch.context() as patch:
                patch.setattr(pg_storage, "_session_transaction", uncertain)
                assert await runner.run_once()
            state = scope.state()
            assert state is not None
            if committed:
                assert state[:2] == ("idle", "已归纳。")
                assert not await runner.run_once()
            else:
                assert state[:3] == ("queued", None, None)
                assert 2 < scope.remaining_wait() <= 5
                scope.make_due()
                assert await runner.run_once()
                assert model.prompts[0] == model.prompts[1]

    run(exercise())


def test_erasure_generation_change_discards_result_with_active_lease(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            separate = SessionId("separate")
            async with scope.storage.session(scope.space.id, write=True) as writer:
                await writer.register_session(separate)
            await scope.append(1, session=separate)
            await scope.append(*range(1, 19))
            blocked = _Model(blocked=True)
            async with asyncio.TaskGroup() as tasks:
                running = tasks.create_task(scope.runner(blocked).run_once())
                try:
                    await asyncio.wait_for(blocked.entered.wait(), 5)
                    async with scope.storage.session(
                        scope.space.id, write=True
                    ) as writer:
                        await writer.delete_session_data(separate)
                finally:
                    blocked.release.set()
            assert running.result()
            state = scope.state()
            assert state is not None and state[:3] == ("queued", None, None)
            scope.make_due()
            assert await scope.runner(blocked).run_once()
            assert blocked.prompts[0] == blocked.prompts[1]

    run(exercise())


def test_worker_consumes_persisted_summary_after_startup(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        async with _scope(database) as scope:
            await scope.append(*range(1, 19))
            model = _Model()

            @asynccontextmanager
            async def open_models(
                *args: object, **options: object
            ) -> AsyncIterator[InferenceClients]:
                yield InferenceClients(llm=model, embedder=FakeEmbedder())

            monkeypatch.setattr(worker, "open_inference", open_models)
            stopping = asyncio.Event()
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    worker._serve(
                        RuntimeSettings(database.runtime),
                        1,
                        stopping,
                        None,
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        await model.entered.wait()
                        while (state := scope.state()) is not None and state[
                            0
                        ] == "running":
                            await asyncio.sleep(0.02)
                        assert state is not None and state[:2] == ("idle", "已归纳。")
                finally:
                    stopping.set()

    run(exercise())
