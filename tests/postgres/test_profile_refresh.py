"""验证画像 HTTP、持久刷新与发布竞态。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection, AsyncCursor, sql
from psycopg.abc import Params, QueryNoTemplate
from psycopg.rows import TupleRow

from mnemotrace import worker
from mnemotrace.adapters.inference import InferenceClients
from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres import storage as pg_storage
from mnemotrace.adapters.postgres.identity import issue_key
from mnemotrace.adapters.postgres.operations import PostgresOperations
from mnemotrace.adapters.postgres.pool import ConnectionPool, runtime_pool
from mnemotrace.adapters.postgres.profile_refresh import (
    _CLAIM_PAGE_SIZE,
    _NEXT_DUE_SPACE,
    PostgresProfileRefresh,
    _group_evidence_query,
)
from mnemotrace.adapters.postgres.session import PostgresSession
from mnemotrace.adapters.runtime import SystemClock, UuidGenerator
from mnemotrace.application import profile_refresh
from mnemotrace.application.operations import OperationRunner
from mnemotrace.application.profile import MAX_PROFILE_FACTS
from mnemotrace.application.profile_refresh import (
    ClaimedProfileRefresh,
    ProfileRefreshRunner,
    ProfileSnapshot,
)
from mnemotrace.application.remember import Remember
from mnemotrace.application.task_policy import MAX_ATTEMPTS, ClaimOutcome
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import ProfileGroup
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import FactId, SessionId, SpaceId
from mnemotrace.domain.profile import Profile, ProfileEntry
from mnemotrace.domain.space import Space
from mnemotrace.domain.values import Label, Predicate
from mnemotrace.ports.inference import LLM, InvalidStructuredOutput
from mnemotrace.ports.queries import FactQuery
from mnemotrace.ports.runtime import Clock
from mnemotrace.ports.storage import CommitOutcomeUnknown, SpaceReader
from mnemotrace.runtime_settings import RuntimeSettings
from mnemotrace.server.app import create_app
from mnemotrace.server.settings import Settings
from mnemotrace.server.v1 import profiles

from ..ports.fakes import FakeClock, FakeEmbedder, FakeLLM
from .conftest import PgDatabase
from .sample import SESSION, fact, message, pool, run

pytestmark = pytest.mark.postgres
_EMPTY: dict[str, list[object]] = {
    "basic": [],
    "communication_preferences": [],
    "preferences": [],
    "constraints": [],
}


@dataclass(frozen=True)
class _HttpSpace:
    client: AsyncClient
    owner: str
    path: str
    headers: dict[str, str]
    foreign: dict[str, str]

    @property
    def id(self) -> SpaceId:
        return SpaceId(self.path.split("/")[-2])

    async def submit(self) -> dict[str, object]:
        response = await self.client.post(self.path + "/refresh", headers=self.headers)
        assert response.status_code == 202, response.text
        return dict(response.json())

    async def view(self) -> dict[str, object]:
        response = await self.client.get(self.path, headers=self.headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def set_auto_refresh(self, enabled: bool) -> None:
        response = await self.client.patch(
            self.path.removesuffix("/profile"),
            headers=self.headers,
            json={"profile_auto_refresh": enabled},
        )
        assert response.status_code == 200, response.text
        assert response.json()["profile_auto_refresh"] is enabled

    def runner(
        self, connections: ConnectionPool, llm: LLM, *, clock: Clock | None = None
    ) -> ProfileRefreshRunner:
        return ProfileRefreshRunner(
            PostgresProfileRefresh(connections),
            lambda owner: PostgresStorage(connections, owner_id=owner),
            llm,
            FakeClock() if clock is None else clock,
            worker._is_transient,
        )


@asynccontextmanager
async def _http_space(
    database: PgDatabase, *, configured: bool = True
) -> AsyncIterator[_HttpSpace]:
    owner = uuid4().hex
    with psycopg.connect(database.key_admin) as conn:
        key = issue_key(conn, owner)
        foreign = issue_key(conn, uuid4().hex)
    models = {
        "MNEMOTRACE_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "MNEMOTRACE_LLM_MODEL": "profile-test",
        "MNEMOTRACE_LLM_JSON_MODE": "json_schema",
        "MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS": "32768",
        "MNEMOTRACE_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
        "MNEMOTRACE_EMBED_MODEL": "profile-test",
        "MNEMOTRACE_EMBED_REVISION": "r1",
        "MNEMOTRACE_EMBED_DIM": "2",
    }
    app = create_app(
        Settings.from_env(
            {
                "MNEMOTRACE_POSTGRES_DSN": database.runtime,
                **(models if configured else {}),
            }
        )
    )
    headers = {"Authorization": f"Bearer {key.token}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/v1/spaces", json={"name": "合成画像空间"}, headers=headers
        )
        assert response.status_code == 201, response.text
        space_path = response.headers["Location"]
        scope = _HttpSpace(
            client,
            owner,
            space_path + "/profile",
            headers,
            {"Authorization": f"Bearer {foreign.token}"},
        )
        try:
            yield scope
        finally:
            await client.delete(space_path, headers=headers)


class _Model(FakeLLM):
    def __init__(
        self,
        text: str = "长期偏好",
        *,
        before_response: Callable[[], Awaitable[None]] | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.text = text
        self.before_response = before_response
        self.failure = failure
        self.prompts: list[list[str]] = []

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        request = json.loads(prompt)
        if "messages" in request:
            (origin,) = request["messages"]
            return {
                "facts": [
                    {
                        "subject": {"kind": "user", "value": None},
                        "predicate": "language",
                        "object": {"kind": "literal", "value": "中文"},
                        "text": origin["content"],
                        "valid_at_evidence": None,
                        "invalid_at_evidence": None,
                        "source_numbers": [origin["number"]],
                        "profile_groups": ["basic"],
                        "labels": [],
                        "sensitive": False,
                    }
                ]
            }
        self.prompts.append([row["text"] for row in request["facts"]])
        if self.before_response is not None:
            await self.before_response()
        if self.failure is not None:
            raise self.failure
        groups = cast(list[str], schema["required"])
        return {
            group: [
                {
                    "text": self.text,
                    "evidence_numbers": [
                        next(
                            row["number"]
                            for row in request["facts"]
                            if group in row["profile_groups"]
                        )
                    ],
                }
            ]
            for group in groups
        }


async def _write_fact(
    scope: _HttpSpace,
    storage: PostgresStorage,
    identifier: str,
    *,
    session: SessionId = SESSION,
    predicate: str = "preference",
    valid_at: float | None = None,
    invalid_at: float | None = None,
    sensitive: bool = False,
    groups: tuple[ProfileGroup, ...] = (ProfileGroup.BASIC,),
) -> Fact:
    space = Space(scope.id, scope.owner, "画像测试", 0)
    subject = Entity.space_user(scope.id)
    origin = message(space, identifier, session=session)
    evidence = replace(
        fact(subject, identifier, text=identifier, provenance=(origin.id,)),
        predicate=Predicate(predicate),
        valid_at=valid_at,
        invalid_at=invalid_at,
        expired_at=20.0 if invalid_at is not None else None,
        sensitive=sensitive,
        profile_groups=groups,
    )
    async with storage.session(scope.id, write=True) as writer:
        await writer.upsert_entity(subject)
        if not await writer.has_session(session):
            await writer.register_session(session)
        await writer.append_messages((origin,))
        await writer.save_facts((evidence,), changed_at=20.0)
    return evidence


def _make_claimable(database: PgDatabase, space_id: SpaceId) -> None:
    with psycopg.connect(database.admin) as conn:
        conn.execute(
            """
            UPDATE mnemotrace.profile_refreshes
            SET lease_until = CASE WHEN status = 'running' THEN clock_timestamp() END,
                available_at = CASE WHEN status = 'queued' THEN clock_timestamp() END
            WHERE space_id = %s AND status IN ('queued', 'running')
            """,
            (space_id.value,),
        )


def test_exhausted_profile_settlement_skips_a_busy_space(database: PgDatabase) -> None:
    def state(space_id: SpaceId) -> TupleRow:
        with psycopg.connect(database.admin) as conn:
            row = conn.execute(
                """
                SELECT status, attempts, lease_token, lease_until, error_code
                FROM mnemotrace.profile_refreshes WHERE space_id = %s
                """,
                (space_id.value,),
            ).fetchone()
        assert row is not None
        return row

    async def exercise() -> None:
        async with (
            _http_space(database) as first,
            _http_space(database) as second,
            runtime_pool(RuntimeSettings(database.runtime), max_size=3) as connections,
        ):
            await first.submit()
            queue = PostgresProfileRefresh(connections)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                job = await queue.claim()
                assert isinstance(job, ClaimedProfileRefresh)
                assert job.space_id == first.id and job.attempt == attempt
                _make_claimable(database, first.id)
            expired = state(first.id)
            assert expired[:2] == ("running", MAX_ATTEMPTS)

            first_storage = PostgresStorage(connections, owner_id=first.owner)
            second_storage = PostgresStorage(connections, owner_id=second.owner)
            await _write_fact(second, second_storage, "ready")
            await second.submit()
            model = _Model()
            runner = first.runner(connections, model)
            async with first_storage.session(first.id, write=True):
                assert await runner.run_once()
                assert state(first.id) == expired
                assert (await second.view())["refresh_status"] == "succeeded"

            assert sorted(
                await asyncio.gather(runner.run_once(), runner.run_once())
            ) == [
                False,
                True,
            ]
            assert state(first.id) == (
                "failed",
                MAX_ATTEMPTS,
                None,
                None,
                "worker_lease_expired",
            )
            assert model.prompts == [["ready"]]

    run(exercise())


def test_profile_claim_uses_due_order_across_ready_and_exhausted_jobs(
    isolated_database: PgDatabase,
) -> None:
    database = isolated_database
    with psycopg.connect(database.admin) as conn:
        conn.execute("""
            INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
            VALUES ('earliest', 'owner', 'fixture', 0),
                   ('exhausted', 'owner', 'fixture', 0),
                   ('latest', 'owner', 'fixture', 0)
            """)
        conn.execute("""
            INSERT INTO mnemotrace.profile_refreshes (space_id, available_at)
            VALUES ('earliest', to_timestamp(1)), ('latest', to_timestamp(3))
            """)
        conn.execute(
            """
            INSERT INTO mnemotrace.profile_refreshes
                (space_id, status, attempts, available_at, lease_token, lease_until)
            VALUES ('exhausted', 'running', %s, NULL, 'expired', to_timestamp(2))
            """,
            (MAX_ATTEMPTS,),
        )

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            queue = PostgresProfileRefresh(connections)
            first = await queue.claim()
            assert isinstance(first, ClaimedProfileRefresh)
            assert first.space_id == SpaceId("earliest")
            assert await queue.claim() is ClaimOutcome.SETTLED
            last = await queue.claim()
            assert isinstance(last, ClaimedProfileRefresh)
            assert last.space_id == SpaceId("latest")
            assert await queue.claim() is ClaimOutcome.EMPTY

    run(exercise())


def test_profile_claim_continues_after_a_page_of_locked_candidates(
    isolated_database: PgDatabase,
) -> None:
    database = isolated_database
    expired_count = _CLAIM_PAGE_SIZE + 1
    ready_space = SpaceId(f"{expired_count + 1:06}")
    with psycopg.connect(database.admin) as conn:
        conn.execute(
            """
            WITH inserted AS (
                INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
                SELECT lpad(i::text, 6, '0'), 'owner', 'fixture', 0
                FROM generate_series(1, %s::integer) AS i
                RETURNING space_id
            )
            INSERT INTO mnemotrace.profile_refreshes
                (space_id, status, attempts, available_at, lease_token, lease_until)
            SELECT space_id, 'running', %s, NULL, 'expired', to_timestamp(1)
            FROM inserted
            """,
            (expired_count, MAX_ATTEMPTS),
        )
        conn.execute(
            """
            INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
            VALUES (%s, 'owner', 'fixture', 0)
            """,
            (ready_space.value,),
        )
        conn.execute(
            """
            INSERT INTO mnemotrace.profile_refreshes (space_id, available_at)
            VALUES (%s, to_timestamp(1))
            """,
            (ready_space.value,),
        )

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            queue = PostgresProfileRefresh(connections)
            with psycopg.connect(database.admin) as locked:
                locked.execute(
                    "SELECT space_id FROM mnemotrace.spaces WHERE space_id <= %s FOR UPDATE",
                    (f"{_CLAIM_PAGE_SIZE:06}",),
                )
                assert await queue.claim() is ClaimOutcome.SETTLED
                failed = locked.execute("""
                    SELECT space_id FROM mnemotrace.profile_refreshes WHERE status = 'failed'
                    """).fetchall()
                assert failed == [(f"{expired_count:06}",)]
                job = await queue.claim()
                assert (
                    isinstance(job, ClaimedProfileRefresh)
                    and job.space_id == ready_space
                )
                assert await queue.claim() is ClaimOutcome.EMPTY
            assert await queue.claim() is ClaimOutcome.SETTLED

    run(exercise())


def test_profile_claim_scan_stays_bounded_as_ready_jobs_grow(
    isolated_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = isolated_database
    execute = AsyncConnection.execute
    scans: list[int] = []

    async def measure_ordered_scan(
        connection: AsyncConnection[TupleRow],
        query: QueryNoTemplate,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool = False,
    ) -> AsyncCursor[TupleRow]:
        if (
            isinstance(query, str)
            and "FROM mnemotrace.profile_refreshes AS job" in query
            and "ORDER BY" in query
        ):
            probe = await execute(
                connection, "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
            )
            row = await probe.fetchone()
            assert row is not None
            plan = row[0][0]["Plan"]
            scans.append(plan["Shared Hit Blocks"] + plan["Shared Read Blocks"])
        return await execute(connection, query, params, prepare=prepare, binary=binary)

    def append_ready_jobs(start: int, end: int) -> None:
        with psycopg.connect(database.admin) as conn:
            conn.execute(
                """
                WITH inserted AS (
                    INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
                    SELECT lpad(i::text, 6, '0'), 'owner', 'fixture', 0
                    FROM generate_series(%s::integer, %s::integer) AS i
                    RETURNING space_id
                )
                INSERT INTO mnemotrace.profile_refreshes (space_id, available_at)
                SELECT space_id, to_timestamp(1) FROM inserted
                """,
                (start, end),
            )
            conn.execute("ANALYZE mnemotrace.profile_refreshes")
            conn.execute("ANALYZE mnemotrace.spaces")

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            queue = PostgresProfileRefresh(connections)
            append_ready_jobs(1, 1_000)
            assert isinstance(await queue.claim(), ClaimedProfileRefresh)
            assert scans
            small = sum(scans)
            scans.clear()
            append_ready_jobs(1_001, 10_000)
            assert isinstance(await queue.claim(), ClaimedProfileRefresh)
            assert scans and sum(scans) <= small + 4

    monkeypatch.setattr(AsyncConnection, "execute", measure_ordered_scan)
    run(exercise())


def _group_revisions(
    database: PgDatabase, space_id: SpaceId
) -> dict[ProfileGroup, tuple[int, int]]:
    with psycopg.connect(database.admin) as conn:
        rows = conn.execute(
            """
            SELECT group_name, target_revision, published_revision
            FROM mnemotrace.profile_group_refreshes WHERE space_id = %s
            """,
            (space_id.value,),
        ).fetchall()
    return {ProfileGroup(name): (target, published) for name, target, published in rows}


def _space_transition(database: PgDatabase, space: SpaceId) -> float | None:
    with psycopg.connect(database.admin) as conn:
        row = conn.execute(
            """
            SELECT profile_next_transition_at, (
                SELECT min(transition_at) FROM mnemotrace.profile_group_refreshes
                WHERE space_id = s.space_id
            )
            FROM mnemotrace.spaces AS s WHERE s.space_id = %s
            """,
            (space.value,),
        ).fetchone()
    assert row is not None
    earliest, group_minimum = row
    assert earliest == group_minimum
    return cast(float | None, earliest)


def test_profile_refresh_uses_lease_order_after_clock_rollback(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            evidence = await _write_fact(scope, storage, "source")
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_profile(
                    Profile(
                        scope.id,
                        {"basic": (ProfileEntry("旧画像", (evidence.id,)),)},
                        200.0,
                    )
                )
            before = await scope.view()
            assert before["profile"] is not None
            assert before["updated_at"] == "1970-01-01T00:03:20Z"

            await scope.submit()
            assert await scope.runner(connections, _Model("新画像")).run_once()
            after = await scope.view()
            assert after["refresh_status"] == "succeeded"
            assert after["updated_at"] == "1970-01-01T00:01:40Z"
            assert after["profile"] == {
                **_EMPTY,
                "basic": [{"text": "新画像", "fact_ids": ["source"]}],
            }

    run(exercise())


def test_profile_publication_does_not_page_facts_under_the_write_lock(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_queries: list[FactQuery] = []
    query_facts = PostgresSession.query_facts

    async def count_writer_query(
        writer: PostgresSession, spec: FactQuery, *, reference_time: float
    ) -> tuple[Fact, ...]:
        writer_queries.append(spec)
        return tuple(await query_facts(writer, spec, reference_time=reference_time))

    monkeypatch.setattr(PostgresSession, "query_facts", count_writer_query)

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "source")
            await scope.submit()
            assert await scope.runner(connections, _Model()).run_once()
            assert (await scope.view())["refresh_status"] == "succeeded"
            assert writer_queries == []

    run(exercise())


@pytest.mark.parametrize("change", ["activation", "expiry", "hard_delete"])
def test_profile_rechecks_changes_after_the_last_read_snapshot(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            clock = FakeClock()
            first = await _write_fact(scope, storage, "first")
            if change == "activation":
                await _write_fact(scope, storage, "later", valid_at=101.0)
            elif change == "expiry":
                await _write_fact(scope, storage, "expiring", invalid_at=101.0)
            await scope.submit()
            get_snapshot = PostgresProfileRefresh.snapshot

            async def change_after_read(
                queue: PostgresProfileRefresh,
                reader: SpaceReader,
                reference_time: float,
            ) -> ProfileSnapshot:
                snapshot = await get_snapshot(queue, reader, reference_time)
                if change == "hard_delete":
                    async with storage.session(scope.id, write=True) as writer:
                        await writer.delete_facts((first.id,))
                else:
                    clock.at = 101.0
                return snapshot

            with monkeypatch.context() as patch:
                patch.setattr(
                    PostgresProfileRefresh,
                    "snapshot",
                    change_after_read,
                )
                assert await scope.runner(connections, _Model(), clock=clock).run_once()
            stale = await scope.view()
            assert stale["refresh_status"] == "queued"
            assert stale["profile"] is None
            assert stale["next_retry_at"] is not None
            _make_claimable(database, scope.id)
            model = _Model()
            assert await scope.runner(connections, model, clock=clock).run_once()
            assert (await scope.view())["refresh_status"] == "succeeded"
            assert (
                model.prompts
                == {
                    "activation": [["later", "first"]],
                    "expiry": [["first"]],
                    "hard_delete": [],
                }[change]
            )

    run(exercise())


def test_auto_refresh_starts_on_first_qualifying_commit(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "stored-before-enabling")
            assert (await scope.view())["refresh_status"] == "idle"
            await scope.set_auto_refresh(True)
            assert (await scope.view())["refresh_status"] == "queued"
            await _write_fact(scope, storage, "schedules-first", valid_at=80.0)
            assert (await scope.view())["refresh_status"] == "queued"
            model = _Model()
            assert await scope.runner(connections, model).run_once()
            assert model.prompts == [["schedules-first", "stored-before-enabling"]]
            assert (await scope.view())["refresh_status"] == "succeeded"

    run(exercise())


def test_http_remember_queues_the_first_automatic_profile(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            base = scope.path.removesuffix("/profile")
            await scope.set_auto_refresh(True)
            session = await scope.client.post(base + "/sessions", headers=scope.headers)
            assert session.status_code == 201
            accepted = await scope.client.post(
                session.headers["Location"] + "/remember",
                headers={**scope.headers, "Idempotency-Key": "auto-seed"},
                json={"messages": [{"role": "user", "content": "以后请用中文回复"}]},
            )
            assert accepted.status_code == 202
            model = _Model("喜欢用中文沟通")
            operation_runner = OperationRunner(
                PostgresOperations(connections),
                lambda job: Remember(
                    space_id=job.space_id,
                    storage=PostgresStorage(connections, owner_id=job.owner_id),
                    llm=model,
                    embedder=FakeEmbedder(),
                    clock=SystemClock(),
                    ids=UuidGenerator(),
                ),
                worker._is_transient,
            )
            assert await operation_runner.run_once()
            operation = await scope.client.get(
                accepted.headers["Location"], headers=scope.headers
            )
            assert operation.json()["status"] == "succeeded"
            assert (await scope.view())["refresh_status"] == "queued"
            assert await scope.runner(connections, model).run_once()
            facts = (
                await scope.client.get(base + "/facts", headers=scope.headers)
            ).json()["items"]
            [stored] = facts
            assert (await scope.view())["profile"] == {
                **_EMPTY,
                "basic": [{"text": "喜欢用中文沟通", "fact_ids": [stored["fact_id"]]}],
            }

    run(exercise())


def _database_now(database: PgDatabase) -> datetime:
    with psycopg.connect(database.admin) as conn:
        row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None and isinstance(row[0], datetime)
        return row[0]


def _auto_deadline(
    database: PgDatabase, space_id: SpaceId, *, followup: bool = False
) -> datetime:
    with psycopg.connect(database.admin) as conn:
        row = conn.execute(
            """
            SELECT CASE WHEN %s THEN followup_at ELSE available_at END
            FROM mnemotrace.profile_refreshes WHERE space_id = %s
            """,
            (followup, space_id.value),
        ).fetchone()
        assert row is not None and isinstance(row[0], datetime)
        return row[0]


@pytest.mark.parametrize("manual", [False, True])
def test_auto_refresh_keeps_the_first_deadline_and_allows_manual_advance(
    database: PgDatabase, manual: bool
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            await _write_fact(scope, storage, "first")
            assert await scope.runner(connections, _Model()).run_once()

            earliest = _database_now(database) + timedelta(seconds=60)
            await _write_fact(
                scope, storage, "new-in-second-session", session=SessionId("second")
            )
            latest = _database_now(database) + timedelta(seconds=60)
            deadline = _auto_deadline(database, scope.id)
            assert earliest <= deadline <= latest
            await _write_fact(scope, storage, "another")
            assert _auto_deadline(database, scope.id) == deadline
            assert (await scope.view())["next_retry_at"] is None
            model = _Model()
            assert not await scope.runner(connections, model).run_once()
            assert model.prompts == []
            if manual:
                assert await scope.submit() == {"refresh_status": "queued"}
                await scope.set_auto_refresh(False)
                assert (await scope.view())["refresh_status"] == "queued"
            else:
                _make_claimable(database, scope.id)
            concurrent = await asyncio.gather(
                scope.runner(connections, model).run_once(),
                scope.runner(connections, model).run_once(),
            )
            assert sum(concurrent) == 1
            assert len(model.prompts) == 1
            assert set(model.prompts[0]) == {
                "first",
                "new-in-second-session",
                "another",
            }
            assert (await scope.view())["refresh_status"] == "succeeded"
            assert not await scope.runner(connections, model).run_once()

    run(exercise())


@pytest.mark.parametrize("promote", [False, True])
def test_disabling_auto_refresh_fences_only_automatic_leases(
    database: PgDatabase, promote: bool
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "initial")
            await scope.submit()
            assert await scope.runner(connections, _Model("已存在画像")).run_once()
            original = (await scope.view())["profile"]
            await scope.set_auto_refresh(True)
            await _write_fact(scope, storage, "changed")

            async def change_policy() -> None:
                if promote:
                    assert await scope.submit() == {"refresh_status": "running"}
                await scope.set_auto_refresh(False)
                frame = await scope.view()
                assert frame["profile"] == original
                assert frame["refresh_status"] == ("running" if promote else "idle")
                if not promote:
                    await scope.set_auto_refresh(True)
                    await _write_fact(scope, storage, "new-after-enabling")

            assert await scope.runner(
                connections, _Model("在途结果", before_response=change_policy)
            ).run_once()
            view = await scope.view()
            assert view["refresh_status"] == "queued"
            assert view["profile"] == original
            if promote:
                _make_claimable(database, scope.id)
                assert await scope.runner(connections, _Model()).run_once()
            else:
                assert await scope.runner(connections, _Model("重开后结果")).run_once()
            published = await scope.view()
            assert published["refresh_status"] == "succeeded"
            assert published["profile"] != original

    run(exercise())


@pytest.mark.parametrize(
    "outcome", ["stale", "permanent_error", "restart", "unchanged", "retry", "expired"]
)
def test_inference_followup_preserves_its_window_through_settlement(
    database: PgDatabase, outcome: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            await _write_fact(scope, storage, "first")
            followup: datetime | None = None

            async def change_facts() -> None:
                nonlocal followup
                await _write_fact(scope, storage, "followup", valid_at=89.0)
                followup = _auto_deadline(database, scope.id, followup=True)
                if outcome == "unchanged":
                    async with storage.session(scope.id, write=True) as writer:
                        await writer.delete_facts((FactId("followup"),))
                else:
                    await _write_fact(scope, storage, "same-batch", valid_at=80.0)
                assert _auto_deadline(database, scope.id, followup=True) == followup

            if outcome in ("restart", "expired"):
                queue = PostgresProfileRefresh(connections)
                attempts = MAX_ATTEMPTS if outcome == "expired" else 1
                for attempt in range(1, attempts + 1):
                    abandoned = await queue.claim()
                    assert isinstance(abandoned, ClaimedProfileRefresh)
                    assert abandoned.space_id == scope.id
                    assert abandoned.attempt == attempt
                    if attempt < attempts:
                        _make_claimable(database, scope.id)
                await change_facts()
                _make_claimable(database, scope.id)
                if outcome == "expired":
                    model = _Model()
                    assert await scope.runner(connections, model).run_once()
                    assert model.prompts == []
            else:
                model = _Model(
                    before_response=change_facts,
                    failure={
                        "permanent_error": InvalidStructuredOutput("合成失败"),
                        "retry": TimeoutError(),
                    }.get(outcome),
                )
                assert await scope.runner(connections, model).run_once()
                assert model.prompts == [["first"]]
            if outcome in ("stale", "permanent_error", "expired", "unchanged"):
                assert (await scope.view())["refresh_status"] == "queued"
                assert (await scope.view())["profile"] is None
                assert _auto_deadline(database, scope.id) == followup
                assert not await scope.runner(connections, _Model()).run_once()
                _make_claimable(database, scope.id)
            if outcome == "retry":
                retrying = await scope.view()
                assert retrying["refresh_status"] == "queued"
                assert isinstance(retrying["next_retry_at"], str)
                assert _auto_deadline(database, scope.id, followup=True) == followup
                assert await scope.submit() == {"refresh_status": "queued"}
                promoted = await scope.view()
                assert promoted["next_retry_at"] == retrying["next_retry_at"]
                revisions = _group_revisions(database, scope.id)
                assert revisions.keys() == set(ProfileGroup)
                assert all(
                    target > published for target, published in revisions.values()
                )
                _make_claimable(database, scope.id)
            async with pool(database.runtime) as new_connections:
                model = _Model()
                assert await scope.runner(new_connections, model).run_once()
                assert model.prompts == (
                    [["first"]]
                    if outcome == "unchanged"
                    else [["followup", "same-batch", "first"]]
                )
            assert (await scope.view())["refresh_status"] == "succeeded"
            assert not await scope.runner(connections, _Model()).run_once()

    run(exercise())


def test_purge_clears_auto_refresh_deadline_and_preserves_setting(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            await _write_fact(scope, storage, "before-purge")
            assert await scope.runner(connections, _Model()).run_once()
            await _write_fact(scope, storage, "waiting", invalid_at=4_000_000_000)
            assert _space_transition(database, scope.id) == 4_000_000_000
            assert not await scope.runner(connections, _Model()).run_once()
            base = scope.path.removesuffix("/profile")
            response = await scope.client.post(
                base + "/purge",
                headers={**scope.headers, "Idempotency-Key": "purge-auto-profile"},
            )
            assert response.status_code == 204
            assert (await scope.view())["refresh_status"] == "idle"
            assert (await scope.view())["profile"] is None
            assert _space_transition(database, scope.id) is None
            assert (await scope.client.get(base, headers=scope.headers)).json()[
                "profile_auto_refresh"
            ]
            await _write_fact(
                scope, storage, "after-purge", session=SessionId("after-purge")
            )
            model = _Model()
            assert await scope.runner(connections, model).run_once()
            assert model.prompts == [["after-purge"]]

    run(exercise())


def test_auto_refresh_ignores_ineligible_evidence_and_rolled_back_writes(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            _http_space(database) as other,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            foreign = await other.client.patch(
                scope.path.removesuffix("/profile"),
                headers=other.headers,
                json={"profile_auto_refresh": True},
            )
            assert foreign.status_code == 404
            for bad in (None, 1, "true"):
                invalid = await scope.client.patch(
                    scope.path.removesuffix("/profile"),
                    headers=scope.headers,
                    json={"profile_auto_refresh": bad},
                )
                assert invalid.status_code == 422
                [error] = invalid.json()["errors"]
                assert error["path"] == "/profile_auto_refresh"
            await scope.set_auto_refresh(True)
            listing = await scope.client.get("/v1/spaces", headers=scope.headers)
            [listed] = listing.json()["items"]
            assert listed["profile_auto_refresh"] is True
            await _write_fact(scope, storage, "sensitive", sensitive=True)
            await _write_fact(scope, storage, "future", valid_at=4_000_000_000)
            assert _space_transition(database, scope.id) == 4_000_000_000
            await _write_fact(scope, storage, "expired", invalid_at=90.0)
            await _write_fact(
                other, PostgresStorage(connections, owner_id=other.owner), "isolated"
            )
            assert (await scope.view())["refresh_status"] == "idle"
            assert (await other.view())["refresh_status"] == "idle"

            with pytest.raises(RuntimeError, match="回滚整个事务"):
                async with storage.session(scope.id, write=True) as writer:
                    origin = message(await writer.get_space(), "uncommitted")
                    await writer.append_messages((origin,))
                    evidence = replace(
                        fact(
                            Entity.space_user(scope.id),
                            "rollback",
                            provenance=(origin.id,),
                        ),
                        profile_groups=(ProfileGroup.BASIC,),
                        valid_at=3_999_999_000,
                    )
                    await writer.save_facts((evidence,), changed_at=20.0)
                    raise RuntimeError("回滚整个事务")
            assert (await scope.view())["refresh_status"] == "idle"
            assert _space_transition(database, scope.id) == 4_000_000_000
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(FactId("rollback")) is None
            assert not await scope.runner(connections, _Model()).run_once()

    run(exercise())


def test_auto_refresh_corrects_and_erases_entries_across_sessions(
    database: PgDatabase,
) -> None:
    class AllEvidence(_Model):
        async def _structured_response(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> Mapping[str, object]:
            rows = json.loads(prompt)["facts"]
            self.prompts.append([row["text"] for row in rows])
            return {
                "basic": [
                    {"text": row["text"], "evidence_numbers": [row["number"]]}
                    for row in rows
                ],
            }

    async def exercise() -> None:
        def ids(view: dict[str, object]) -> set[str]:
            profile = view["profile"]
            assert isinstance(profile, dict)
            return {entry["fact_ids"][0] for entry in profile["basic"]}

        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            model = AllEvidence()
            runner = scope.runner(connections, model)
            await scope.set_auto_refresh(True)
            old = await _write_fact(
                scope, storage, "old-language", predicate="language"
            )
            assert await runner.run_once()
            optional = SessionId("different-session")
            surviving = await _write_fact(
                scope, storage, "keep-name", session=optional, predicate="name"
            )
            _make_claimable(database, scope.id)
            assert await runner.run_once()
            assert ids(await scope.view()) == {"old-language", "keep-name"}

            async with storage.session(scope.id, write=True) as writer:
                second_origin = message(await writer.get_space(), "extra-support")
                await writer.append_messages((second_origin,))
                extended = replace(
                    surviving, provenance=(*surviving.provenance, second_origin.id)
                )
                await writer.save_facts(
                    (extended,),
                    changed_at=25.0,
                    support_groups={
                        surviving.id: (surviving.provenance, (second_origin.id,))
                    },
                )
                assert await writer.save_facts((extended,), changed_at=25.0) == ()
            assert (await scope.view())["refresh_status"] == "succeeded"

            async with storage.session(scope.id, write=True) as writer:
                await writer.save_facts((old.close(90.0, 95.0),), changed_at=95.0)
            assert (await scope.view())["refresh_status"] == "queued"
            assert ids(await scope.view()) == {"keep-name"}
            await _write_fact(
                scope, storage, "new-language", predicate="language", valid_at=90.0
            )
            _make_claimable(database, scope.id)
            assert await runner.run_once()
            assert ids(await scope.view()) == {"keep-name", "new-language"}

            base = scope.path.removesuffix("/profile")
            erased = await scope.client.delete(
                base + f"/sessions/{SESSION.value}", headers=scope.headers
            )
            assert erased.status_code == 204
            assert ids(await scope.view()) == {"keep-name"}
            assert (await scope.view())["refresh_status"] == "queued"
            _make_claimable(database, scope.id)
            assert await runner.run_once()
            assert model.prompts[-1] == ["keep-name"]
            assert ids(await scope.view()) == {"keep-name"}

            await _write_fact(scope, storage, "last-waiting", session=optional)
            await scope.set_auto_refresh(False)
            assert (await scope.view())["refresh_status"] == "idle"
            assert ids(await scope.view()) == {"keep-name"}
            assert not await runner.run_once()

    run(exercise())


def test_profile_http_reads_and_coalesces_manual_refresh(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with _http_space(database) as scope:
            client, path, headers = scope.client, scope.path, scope.headers
            idle = await client.get(path, headers=headers)
            assert idle.status_code == 200
            assert idle.headers["cache-control"] == "no-store"
            assert idle.json() == {
                "profile": None,
                "updated_at": None,
                "refresh_status": "idle",
                "next_retry_at": None,
                "refresh_error": None,
            }
            for method, target in (("GET", path), ("POST", path + "/refresh")):
                foreign = await client.request(method, target, headers=scope.foreign)
                assert foreign.status_code == 404
                assert foreign.json()["code"] == "space_not_found"
            invalid = await client.post(path + "/refresh", json={}, headers=headers)
            assert invalid.status_code == 400
            assert invalid.json()["code"] == "unexpected_body"
            submissions = await asyncio.gather(
                client.post(path + "/refresh", headers=headers),
                client.post(path + "/refresh", headers=headers),
            )
            for queued in submissions:
                assert queued.status_code == 202
                assert queued.headers["location"] == path
                assert queued.json() == {"refresh_status": "queued"}
            assert (await client.get(path, headers=headers)).json() == {
                **idle.json(),
                "refresh_status": "queued",
            }
            async with pool(database.runtime) as connections:
                queue = PostgresProfileRefresh(connections)
                outcomes = await asyncio.gather(queue.claim(), queue.claim())
                assert outcomes.count(ClaimOutcome.EMPTY) == 1
                [claimed] = [
                    job for job in outcomes if isinstance(job, ClaimedProfileRefresh)
                ]
                assert claimed.space_id == scope.id
                assert claimed.version == claimed.attempt == 1

    run(exercise())


def test_profile_http_with_missing_models_is_read_only(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with _http_space(database, configured=False) as scope:
            response = await scope.client.post(
                scope.path + "/refresh", headers=scope.headers
            )
            assert response.status_code == 503
            assert response.json()["code"] == "write_models_unavailable"
            assert (await scope.view())["refresh_status"] == "idle"
            async with pool(database.runtime) as connections:
                assert (
                    await PostgresProfileRefresh(connections).submit(
                        scope.owner, scope.id, configured=True
                    )
                    == "queued"
                )
                assert await scope.submit() == {"refresh_status": "queued"}
                model = _Model()
                assert await scope.runner(connections, model).run_once()
                assert model.prompts == []
            result = await scope.view()
            assert result["profile"] == _EMPTY
            assert result["refresh_status"] == "succeeded"
            assert result["updated_at"] == "1970-01-01T00:01:40Z"

    run(exercise())


def test_worker_resumes_refresh_and_publishes_the_http_profile(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _Model("希望用中文回复")

    @asynccontextmanager
    async def inference(
        *args: object, **kwargs: object
    ) -> AsyncIterator[InferenceClients]:
        yield InferenceClients(llm=model, embedder=FakeEmbedder())

    monkeypatch.setattr(worker, "open_inference", inference)

    async def exercise() -> None:
        async with _http_space(database) as scope:
            base = scope.path.removesuffix("/profile")
            session = await scope.client.post(base + "/sessions", headers=scope.headers)
            assert session.status_code == 201
            remember = await scope.client.post(
                session.headers["Location"] + "/remember",
                headers={**scope.headers, "Idempotency-Key": "profile-remember"},
                json={"messages": [{"role": "user", "content": "以后请用中文回复"}]},
            )
            assert remember.status_code == 202
            stopping = asyncio.Event()
            async with asyncio.TaskGroup() as workers:
                workers.create_task(
                    worker._serve(
                        RuntimeSettings(database.runtime),
                        worker._WorkerConcurrency(write=1),
                        stopping,
                        None,
                    )
                )
                try:
                    async with asyncio.timeout(8):
                        while True:
                            result = await scope.client.get(
                                remember.headers["Location"], headers=scope.headers
                            )
                            if result.json()["status"] == "succeeded":
                                break
                            await asyncio.sleep(0.05)
                finally:
                    stopping.set()

            assert await scope.submit() == {"refresh_status": "queued"}
            assert (await scope.view())["profile"] is None
            assert model.prompts == []

            stopping = asyncio.Event()
            async with asyncio.TaskGroup() as workers:
                workers.create_task(
                    worker._serve(
                        RuntimeSettings(database.runtime),
                        worker._WorkerConcurrency(write=1),
                        stopping,
                        None,
                    )
                )
                try:
                    async with asyncio.timeout(8):
                        while (view := await scope.view())[
                            "refresh_status"
                        ] != "succeeded":
                            await asyncio.sleep(0.05)
                finally:
                    stopping.set()

            facts = (
                await scope.client.get(base + "/facts", headers=scope.headers)
            ).json()["items"]
            assert len(facts) == 1
            identifier = facts[0]["fact_id"]
            assert view["profile"] == {
                **_EMPTY,
                "basic": [{"text": "希望用中文回复", "fact_ids": [identifier]}],
            }
            assert view["updated_at"] is not None
            assert model.prompts == [["以后请用中文回复"]]
            removal = await scope.client.delete(
                base + f"/facts/{identifier}", headers=scope.headers
            )
            assert removal.status_code == 204
            assert (await scope.view())["profile"] is None

    run(exercise())


def test_worker_refreshes_different_profiles_concurrently(
    isolated_database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        database = isolated_database
        both_entered, release = asyncio.Event(), asyncio.Event()

        async def hold_inference() -> None:
            if len(model.prompts) == 2:
                both_entered.set()
            await release.wait()

        model = _Model(before_response=hold_inference)
        clients = InferenceClients(llm=model, embedder=FakeEmbedder())
        monkeypatch.setattr(
            worker, "open_inference", lambda *args, **kwargs: nullcontext(clients)
        )

        async with _http_space(database) as first, _http_space(database) as second:
            async with pool(database.runtime) as connections:
                for scope, identifier in ((first, "first"), (second, "second")):
                    storage = PostgresStorage(connections, owner_id=scope.owner)
                    await _write_fact(scope, storage, identifier)
                    assert await scope.submit() == {"refresh_status": "queued"}

            stopping = asyncio.Event()
            async with asyncio.TaskGroup() as workers:
                workers.create_task(
                    worker._serve(
                        RuntimeSettings(database.runtime),
                        worker._WorkerConcurrency(profile=2),
                        stopping,
                        None,
                    )
                )
                try:
                    async with asyncio.timeout(10):
                        await both_entered.wait()
                        assert (await first.view())["refresh_status"] == "running"
                        assert (await second.view())["refresh_status"] == "running"
                        release.set()
                        while True:
                            results = await asyncio.gather(first.view(), second.view())
                            if all(
                                result["refresh_status"] == "succeeded"
                                for result in results
                            ):
                                break
                            await asyncio.sleep(0.02)
                finally:
                    release.set()
                    stopping.set()

            assert sorted(model.prompts) == [["first"], ["second"]]
            for result, identifier in zip(results, ("first", "second"), strict=True):
                assert result["profile"] == {
                    **_EMPTY,
                    "basic": [{"text": model.text, "fact_ids": [identifier]}],
                }

    run(exercise())


@pytest.mark.parametrize("purge", [False, True])
def test_late_result_cannot_override_a_new_worker(
    database: PgDatabase, purge: bool
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "original")
            await scope.submit()
            entered, release = asyncio.Event(), asyncio.Event()

            async def pause() -> None:
                entered.set()
                await release.wait()

            async with asyncio.TaskGroup() as workers:
                old = workers.create_task(
                    scope.runner(
                        connections, _Model("旧结果", before_response=pause)
                    ).run_once()
                )
                try:
                    async with asyncio.timeout(5):
                        await entered.wait()
                        assert await scope.submit() == {"refresh_status": "running"}
                        if purge:
                            response = await scope.client.post(
                                scope.path.removesuffix("/profile") + "/purge",
                                headers={**scope.headers, "Idempotency-Key": "erase"},
                            )
                            assert response.status_code == 204
                            assert (await scope.view())["refresh_status"] == "idle"
                            await _write_fact(scope, storage, "after-purge")
                            await scope.submit()
                        else:
                            _make_claimable(database, scope.id)
                        assert await scope.runner(
                            connections, _Model("新结果")
                        ).run_once()
                finally:
                    release.set()
            assert old.result()
            view = await scope.view()
            assert view["refresh_status"] == "succeeded"
            assert view["profile"] == {
                **_EMPTY,
                "basic": [
                    {
                        "text": "新结果",
                        "fact_ids": ["after-purge" if purge else "original"],
                    }
                ],
            }

    run(exercise())


@pytest.mark.parametrize(
    "change", ["new_fact", "new_group", "erase_session", "append_evidence"]
)
def test_profile_publication_rechecks_the_snapshot(
    database: PgDatabase, change: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            original = await _write_fact(scope, storage, "source")
            if change == "new_group":
                await scope.set_auto_refresh(True)
            else:
                await scope.submit()

            async def mutate() -> None:
                async with asyncio.timeout(3):
                    if change == "new_fact":
                        await _write_fact(scope, storage, "new", valid_at=80.0)
                        return
                    if change == "new_group":
                        await _write_fact(
                            scope, storage, "new", groups=(ProfileGroup.PREFERENCES,)
                        )
                        return
                    async with storage.session(scope.id, write=True) as writer:
                        if change == "erase_session":
                            await writer.delete_session_data(SESSION)
                        else:
                            second = message(await writer.get_space(), "additional")
                            await writer.append_messages((second,))
                            await writer.save_facts(
                                (
                                    replace(
                                        original,
                                        provenance=(*original.provenance, second.id),
                                    ),
                                ),
                                changed_at=25.0,
                                support_groups={
                                    original.id: (original.provenance, (second.id,))
                                },
                            )

            stale = _Model("第一次生成", before_response=mutate)
            assert await scope.runner(connections, stale).run_once()
            view = await scope.view()
            if change == "append_evidence":
                assert view["refresh_status"] == "succeeded"
                assert view["profile"] == {
                    **_EMPTY,
                    "basic": [{"text": "第一次生成", "fact_ids": ["source"]}],
                }
            else:
                assert view["refresh_status"] == "queued"
                assert view["profile"] is None
                if change == "new_group":
                    assert _auto_deadline(database, scope.id) is not None
                    assert view["next_retry_at"] is None
                else:
                    assert isinstance(view["next_retry_at"], str)
                _make_claimable(database, scope.id)
                fresh = _Model("从新快照生成")
                assert await scope.runner(connections, fresh).run_once()
                view = await scope.view()
                assert view["refresh_status"] == "succeeded"
                if change == "new_fact":
                    assert fresh.prompts == [["new", "source"]]
                    assert view["profile"] == {
                        **_EMPTY,
                        "basic": [{"text": "从新快照生成", "fact_ids": ["new"]}],
                    }
                elif change == "new_group":
                    assert fresh.prompts == [["source", "new"]]
                    assert view["profile"] == {
                        **_EMPTY,
                        "basic": [{"text": fresh.text, "fact_ids": ["source"]}],
                        "preferences": [{"text": fresh.text, "fact_ids": ["new"]}],
                    }
                else:
                    assert fresh.prompts == []
                    assert view["profile"] == _EMPTY

    run(exercise())


@pytest.mark.parametrize("change", ["labels", "append_source"])
def test_profile_retains_entries_after_evidence_metadata_update(
    database: PgDatabase, change: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            source = await _write_fact(scope, storage, "source")
            assert await scope.runner(connections, _Model()).run_once()
            before = await scope.view()
            assert before["profile"] is not None
            revisions = _group_revisions(database, scope.id)
            async with storage.session(scope.id, write=True) as writer:
                support = None
                if change == "labels":
                    updated = replace(source, labels=(Label("user_preferences"),))
                else:
                    extra = message(await writer.get_space(), "extra")
                    await writer.append_messages((extra,))
                    updated = replace(source, provenance=(*source.provenance, extra.id))
                    support = {source.id: (source.provenance, (extra.id,))}
                await writer.save_facts(
                    (updated,), changed_at=21.0, support_groups=support
                )
            assert await scope.view() == before
            assert _group_revisions(database, scope.id) == revisions

    run(exercise())


def test_scheduled_expiry_preserves_the_last_profile_until_the_fact_expires(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(at=_database_now(database).timestamp())
    monkeypatch.setattr(profiles, "time", clock.now)

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            expires = await _write_fact(scope, storage, "expiring")
            current = await _write_fact(scope, storage, "regular")
            stored = Profile(
                scope.id,
                {
                    "basic": (
                        ProfileEntry("需要两个引用", (expires.id, current.id)),
                        ProfileEntry("有独立支撑", (current.id,)),
                    )
                },
                clock.at,
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_profile(stored)
            before = await scope.view()
            assert before["profile"] == {
                **_EMPTY,
                "basic": [
                    {"text": "需要两个引用", "fact_ids": ["expiring", "regular"]},
                    {"text": "有独立支撑", "fact_ids": ["regular"]},
                ],
            }
            cutoff = clock.at + 120
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_facts(
                    (replace(expires, invalid_at=cutoff, expired_at=clock.at),),
                    changed_at=clock.at,
                )
            await scope.submit()
            pending = await scope.view()
            assert pending["profile"] == before["profile"]
            assert pending["updated_at"] == before["updated_at"]
            model = _Model(failure=InvalidStructuredOutput("正文不得进入失败响应"))
            assert await scope.runner(connections, model, clock=clock).run_once()
            failed = await scope.view()
            assert failed["profile"] == pending["profile"]
            assert failed["updated_at"] == pending["updated_at"]
            assert failed["refresh_status"] == "failed"
            assert failed["refresh_error"] == {"code": "invalid_model_output"}
            assert model.prompts == [["expiring", "regular"]]
            clock.at = cutoff
            expired = await scope.view()
            assert expired["refresh_status"] == "failed"
            assert expired["updated_at"] == pending["updated_at"]
            assert expired["profile"] == {
                **_EMPTY,
                "basic": [{"text": "有独立支撑", "fact_ids": ["regular"]}],
            }
            async with storage.session(scope.id) as reader:
                assert await reader.get_profile() == stored

    run(exercise())


def test_profile_retries_then_starts_a_new_version_after_failure(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "eligible")
            await scope.submit()
            model = _Model(failure=TimeoutError())
            runner = scope.runner(connections, model)
            for expected_delay in (5, 10):
                start = datetime.now(UTC)
                assert await runner.run_once()
                now = datetime.now(UTC)
                state = await scope.view()
                assert state["refresh_status"] == "queued"
                retry_at = state["next_retry_at"]
                assert isinstance(retry_at, str)
                due = datetime.fromisoformat(retry_at)
                assert (due - start).total_seconds() >= expected_delay - 1
                assert (due - now).total_seconds() <= expected_delay + 1
                assert not await runner.run_once()
                _make_claimable(database, scope.id)
            assert await runner.run_once()
            assert (await scope.view())["refresh_error"] == {
                "code": "operation_timeout"
            }
            assert len(model.prompts) == 3
            assert not await runner.run_once()
            await scope.submit()
            assert await scope.runner(connections, _Model()).run_once()
            assert (await scope.view())["refresh_status"] == "succeeded"

    run(exercise())


@pytest.mark.parametrize("commit_succeeded", [True, False])
def test_profile_recovers_an_uncertain_commit_without_partial_publication(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, commit_succeeded: bool
) -> None:
    real_transaction = pg_storage._session_transaction
    injected = False

    @asynccontextmanager
    async def transaction(
        conn: AsyncConnection[TupleRow], *, write: bool
    ) -> AsyncIterator[None]:
        nonlocal injected
        fail = write and not injected
        async with real_transaction(conn, write=write):
            yield
            if fail:
                injected = True
            if fail and not commit_succeeded:
                raise CommitOutcomeUnknown("测试回滚")
        if fail:
            raise CommitOutcomeUnknown("测试提交确认丢失")

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "source")
            await _write_fact(
                scope, storage, "preference", groups=(ProfileGroup.PREFERENCES,)
            )
            await scope.submit()
            pending = _group_revisions(database, scope.id)
            assert pending.keys() == set(ProfileGroup)
            model = _Model()
            runner = scope.runner(connections, model)
            with monkeypatch.context() as patch:
                patch.setattr(pg_storage, "_session_transaction", transaction)
                assert await runner.run_once()
            view = await scope.view()
            if commit_succeeded:
                assert view["refresh_status"] == "succeeded"
                assert view["next_retry_at"] is None
            else:
                assert view["refresh_status"] == "queued"
                assert view["profile"] is None
                assert isinstance(view["next_retry_at"], str)
                assert _group_revisions(database, scope.id) == pending
                _make_claimable(database, scope.id)
                assert await runner.run_once()
                view = await scope.view()
            assert view["refresh_status"] == "succeeded"
            assert view["profile"] == {
                **_EMPTY,
                "basic": [{"text": model.text, "fact_ids": ["source"]}],
                "preferences": [{"text": model.text, "fact_ids": ["preference"]}],
            }
            assert _group_revisions(database, scope.id) == {
                group: (target, target) for group, (target, _) in pending.items()
            }
            assert model.prompts == [["source", "preference"]] * (
                1 if commit_succeeded else 2
            )
            assert not await runner.run_once()

    run(exercise())


def test_followup_profile_reads_all_evidence_for_the_affected_group(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "existing", valid_at=95.0)
            await scope.submit()
            first = _Model()
            assert await scope.runner(connections, first).run_once()
            assert first.prompts == [["existing"]]
            await _write_fact(scope, storage, "new", valid_at=80.0, predicate="format")
            await scope.submit()
            followup = _Model()
            assert await scope.runner(connections, followup).run_once()
            assert followup.prompts == [["existing", "new"]]

    run(exercise())


@pytest.mark.parametrize("change", ["move", "sensitive", "erase"])
def test_only_affected_groups_refresh_for_shared_evidence(
    database: PgDatabase,
    change: str,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            communication, constraints = (
                ProfileGroup.COMMUNICATION_PREFERENCES,
                ProfileGroup.CONSTRAINTS,
            )
            await _write_fact(
                scope, storage, "unrelated", groups=(ProfileGroup.PREFERENCES,)
            )
            shared = await _write_fact(
                scope, storage, "shared", groups=(communication, constraints)
            )
            await scope.set_auto_refresh(True)
            first = _Model("已有画像")
            assert await scope.runner(connections, first).run_once()
            assert first.prompts == [["shared", "unrelated"]]
            before = await scope.view()
            before_revisions = _group_revisions(database, scope.id)
            before_profile = cast(dict[str, object], before["profile"])
            assert (
                before_profile[communication]
                == before_profile[constraints]
                == [
                    {"text": "已有画像", "fact_ids": ["shared"]},
                ]
            )

            async with storage.session(scope.id, write=True) as writer:
                if change == "move":
                    await writer.save_facts(
                        (replace(shared, profile_groups=(constraints,)),),
                        changed_at=21.0,
                    )
                elif change == "sensitive":
                    await writer.save_facts(
                        (replace(shared, sensitive=True),), changed_at=21.0
                    )
                else:
                    await writer.delete_facts((shared.id,))
            pending = await scope.view()
            assert pending["refresh_status"] == "queued"
            revisions = _group_revisions(database, scope.id)
            for group in (communication, constraints):
                target, published = revisions[group]
                assert target > published == before_revisions[group][1]
            assert (
                revisions[ProfileGroup.PREFERENCES]
                == before_revisions[ProfileGroup.PREFERENCES]
            )
            assert cast(dict[str, object], pending["profile"])[communication] == []
            if change != "move":
                async with storage.session(scope.id) as reader:
                    stored = await reader.get_profile()
                    assert stored is not None
                    assert shared.id not in stored.source_fact_ids
            _make_claimable(database, scope.id)
            model = _Model("刷新后的约束")
            assert await scope.runner(connections, model).run_once()
            assert model.prompts == ([["shared"]] if change == "move" else [])
            view = await scope.view()
            profile = cast(dict[str, object], view["profile"])
            assert profile[communication] == []
            assert (profile[constraints] == []) == (change != "move")
            assert (
                profile[ProfileGroup.PREFERENCES]
                == before_profile[ProfileGroup.PREFERENCES]
            )
            assert view["refresh_status"] == "succeeded"

    run(exercise())


@pytest.mark.parametrize("failure", ["row_limit", "context_limit", "timeout"])
def test_profile_failure_keeps_all_published_groups_until_recovery(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    class ContextLimitedModel(_Model):
        def structured_prompt_fits(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> bool:
            # 各组单独都能放入预算，合并后需要减少一条证据。
            return len(json.loads(prompt)["facts"]) <= 3

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            communication, preferences = (
                ProfileGroup.COMMUNICATION_PREFERENCES,
                ProfileGroup.PREFERENCES,
            )
            old = await _write_fact(
                scope, storage, "old-communication", groups=(communication,)
            )
            await _write_fact(scope, storage, "old-preference", groups=(preferences,))
            await scope.set_auto_refresh(True)
            assert await scope.runner(connections, _Model("上版")).run_once()
            before = await scope.view()
            before_revisions = _group_revisions(database, scope.id)
            extra = tuple(
                replace(old, id=FactId(f"extra-{index:03}"), text=f"extra-{index:03}")
                for index in range(MAX_PROFILE_FACTS if failure == "row_limit" else 1)
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_facts(extra, changed_at=21.0)
            await _write_fact(
                scope, storage, "new-preference", valid_at=90.0, groups=(preferences,)
            )
            pending = _group_revisions(database, scope.id)
            for group in (communication, preferences):
                target, published = pending[group]
                assert target > published == before_revisions[group][1]

            deadline: asyncio.Timeout | None = None

            def enter_attempt_timeout(_: float) -> asyncio.Timeout:
                nonlocal deadline
                deadline = asyncio.timeout(None)
                return deadline

            async def expire_during_model_call() -> None:
                assert deadline is not None
                deadline.reschedule(asyncio.get_running_loop().time())
                await asyncio.Event().wait()

            if failure == "timeout":
                model = _Model("半份结果", before_response=expire_during_model_call)
                runner = scope.runner(connections, model)
                with monkeypatch.context() as patch:
                    patch.setattr(profile_refresh, "timeout", enter_attempt_timeout)
                    for attempt in range(1, 4):
                        _make_claimable(database, scope.id)
                        assert await runner.run_once()
                        state = await scope.view()
                        assert state["profile"] == before["profile"]
                        assert _group_revisions(database, scope.id) == pending
                        if attempt < 3:
                            assert state["refresh_status"] == "queued"
                            assert isinstance(state["next_retry_at"], str)
                assert len(model.prompts) == 3
                expected_error = "operation_timeout"
            else:
                model = (
                    ContextLimitedModel() if failure == "context_limit" else _Model()
                )
                runner = scope.runner(connections, model)
                _make_claimable(database, scope.id)
                assert await runner.run_once()
                assert model.prompts == []
                expected_error = "profile_input_too_large"

            failed = await scope.view()
            assert failed == {
                **before,
                "refresh_status": "failed",
                "refresh_error": {"code": expected_error},
            }
            assert _group_revisions(database, scope.id) == pending
            assert not await runner.run_once()

            if failure == "timeout":
                await scope.submit()
            else:
                async with storage.session(scope.id, write=True) as writer:
                    await writer.delete_facts((extra[0].id,))
            _make_claimable(database, scope.id)
            recovered_model = (
                ContextLimitedModel("新画像")
                if failure == "context_limit"
                else _Model("新画像")
            )
            assert await scope.runner(connections, recovered_model).run_once()
            [prompt] = recovered_model.prompts
            assert "new-preference" in prompt
            if failure == "row_limit":
                assert len(prompt) == MAX_PROFILE_FACTS + 2
            recovered = await scope.view()
            assert recovered["refresh_status"] == "succeeded"
            assert recovered["refresh_error"] is None
            profile = cast(dict[str, list[dict[str, object]]], recovered["profile"])
            revisions = _group_revisions(database, scope.id)
            for group in (communication, preferences):
                assert profile[group][0]["text"] == recovered_model.text
                target, published = revisions[group]
                assert target == published > before_revisions[group][1]

    run(exercise())


def test_automatic_group_refresh_wakes_on_activation_and_expiry(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock(at=datetime.now(UTC).timestamp())
    monkeypatch.setattr(profiles, "time", clock.now)

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            starts, ends = clock.at + 120, clock.at + 240
            groups = (ProfileGroup.COMMUNICATION_PREFERENCES, ProfileGroup.CONSTRAINTS)
            await _write_fact(
                scope, storage, "timed", groups=groups, valid_at=starts, invalid_at=ends
            )
            assert _space_transition(database, scope.id) == starts
            model = _Model()
            runner = scope.runner(connections, model, clock=clock)
            assert not await runner.run_once()
            clock.at = starts
            assert await runner.run_once()
            assert model.prompts == [["timed"]]
            assert _space_transition(database, scope.id) == ends
            active = cast(dict[str, object], (await scope.view())["profile"])
            assert all(active[group] for group in groups)
            clock.at = ends
            assert not await runner.run_once()
            expired = await scope.view()
            assert expired["refresh_status"] == "queued"
            assert expired["profile"] is None
            assert _space_transition(database, scope.id) is None
            _make_claimable(database, scope.id)
            assert await runner.run_once()
            assert model.prompts == [["timed"]]
            assert (await scope.view())["profile"] == _EMPTY

    run(exercise())


def test_reenabled_auto_profile_can_claim_a_missed_transition(
    database: PgDatabase,
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            starts = _database_now(database).timestamp() + 120
            await scope.set_auto_refresh(True)
            await _write_fact(scope, storage, "future", valid_at=starts)
            await scope.set_auto_refresh(False)
            queue = PostgresProfileRefresh(connections)
            await queue.wake_due(starts)
            assert (await scope.view())["refresh_status"] == "idle"
            assert _space_transition(database, scope.id) == starts

            await scope.set_auto_refresh(True)
            await queue.wake_due(starts)
            assert (await scope.view())["refresh_status"] == "queued"
            assert _space_transition(database, scope.id) is None

    run(exercise())


@pytest.mark.parametrize("advance", ["publish", "wake"])
def test_cancelled_profile_transition_is_removed_without_spurious_refresh(
    database: PgDatabase, advance: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await scope.set_auto_refresh(True)
            starts = _database_now(database).timestamp() + 120
            future = await _write_fact(scope, storage, "future", valid_at=starts)
            async with storage.session(scope.id, write=True) as writer:
                await writer.delete_facts((future.id,))
            assert _space_transition(database, scope.id) == starts
            assert (await scope.view())["refresh_status"] == "idle"

            if advance == "publish":
                await scope.submit()
                model = _Model()
                assert await scope.runner(connections, model).run_once()
                assert model.prompts == []
                assert (await scope.view())["refresh_status"] == "succeeded"
            else:
                queue = PostgresProfileRefresh(connections)
                await queue.wake_due(starts)
                assert (await scope.view())["refresh_status"] == "idle"
            assert _space_transition(database, scope.id) is None

    run(exercise())


def test_due_profile_query_stays_bounded_as_disabled_spaces_grow(
    database: PgDatabase,
) -> None:
    with psycopg.connect(database.admin) as conn, conn.transaction(force_rollback=True):
        conn.execute("""
            INSERT INTO mnemotrace.spaces
                (space_id, owner_id, name, created_at, profile_auto_refresh,
                 profile_next_transition_at)
            VALUES ('poll-enabled', 'fixture', 'fixture', 0, true, 300)
            """)
        conn.execute("""
            INSERT INTO mnemotrace.profile_group_refreshes (space_id, group_name, transition_at)
            VALUES ('poll-enabled', 'basic', 300)
            """)

        def seed_disabled(start: int, end: int) -> None:
            conn.execute(
                """
                WITH inserted AS (
                    INSERT INTO mnemotrace.spaces
                        (space_id, owner_id, name, created_at, profile_next_transition_at)
                    SELECT 'poll-disabled-' || i, 'fixture', 'fixture', 0, 100
                    FROM generate_series(%s::integer, %s::integer) AS i
                    RETURNING space_id
                )
                INSERT INTO mnemotrace.profile_group_refreshes
                    (space_id, group_name, transition_at)
                SELECT space_id, 'basic', 100 FROM inserted
                """,
                (start, end),
            )
            conn.execute("ANALYZE mnemotrace.spaces")

        def empty_poll_buffers() -> int:
            row = conn.execute(
                sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ")
                + sql.SQL(_NEXT_DUE_SPACE),
                (200.0,),
            ).fetchone()
            assert row is not None
            plan = row[0][0]["Plan"]
            assert plan["Actual Rows"] == 0
            assert '"Index Name": "spaces_profile_next_transition"' in json.dumps(plan)
            return int(plan["Shared Hit Blocks"] + plan["Shared Read Blocks"])

        seed_disabled(1, 1000)
        small = empty_poll_buffers()
        seed_disabled(1001, 10000)
        assert empty_poll_buffers() <= small + 2
        assert conn.execute(_NEXT_DUE_SPACE, (300.0,)).fetchone() == ("poll-enabled",)


def test_due_refresh_skips_a_space_with_a_busy_writer(database: PgDatabase) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as first,
            _http_space(database) as second,
            runtime_pool(RuntimeSettings(database.runtime), max_size=3) as connections,
        ):
            first_storage = PostgresStorage(connections, owner_id=first.owner)
            second_storage = PostgresStorage(connections, owner_id=second.owner)
            await first.set_auto_refresh(True)
            await second.set_auto_refresh(True)
            now = _database_now(database).timestamp()
            await _write_fact(first, first_storage, "first", valid_at=now + 120)
            await _write_fact(second, second_storage, "second", valid_at=now + 240)
            queue = PostgresProfileRefresh(connections)

            async with first_storage.session(first.id, write=True):
                await queue.wake_due(now + 240)
                assert (await second.view())["refresh_status"] == "queued"
                assert (await first.view())["refresh_status"] == "idle"

            await queue.wake_due(now + 240)
            assert (await first.view())["refresh_status"] == "queued"

    run(exercise())


def test_profile_get_checks_expiry_after_opening_read_session(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 100.0
    monkeypatch.setattr(profiles, "time", lambda: now)

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            evidence = await _write_fact(scope, storage, "expiring", invalid_at=101.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_profile(
                    Profile(
                        scope.id,
                        {"basic": (ProfileEntry("画像", (evidence.id,)),)},
                        now,
                    )
                )
            assert (await scope.view())["profile"] is not None

            open_session = PostgresStorage._session

            @asynccontextmanager
            async def open_after_expiry(
                storage: PostgresStorage, space_id: SpaceId, write: bool
            ) -> AsyncIterator[SpaceReader]:
                nonlocal now
                assert not write
                now = 101.0
                async with open_session(storage, space_id, write) as reader:
                    yield reader

            with monkeypatch.context() as patch:
                patch.setattr(PostgresStorage, "_session", open_after_expiry)
                assert (await scope.view())["profile"] is None

    run(exercise())


@pytest.mark.parametrize(
    ("change", "expiry_processed"),
    [
        ("delete", False),
        ("delete", True),
        ("erase", False),
        ("move", False),
        ("sensitive", False),
        ("shorten", False),
    ],
)
def test_fact_write_preserves_unprocessed_profile_expiry(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expiry_processed: bool,
) -> None:
    clock = FakeClock()
    monkeypatch.setattr(profiles, "time", clock.now)

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            communication = ProfileGroup.COMMUNICATION_PREFERENCES
            constraints = ProfileGroup.CONSTRAINTS
            preferences = ProfileGroup.PREFERENCES
            for group in (communication, constraints, preferences):
                await _write_fact(
                    scope, storage, group.value, valid_at=80.0, groups=(group,)
                )
            origin_session = SessionId("timed-evidence")
            timed = await _write_fact(
                scope,
                storage,
                "timed",
                session=origin_session,
                groups=(communication, constraints),
                valid_at=95.0,
                invalid_at=120.0,
            )
            await scope.set_auto_refresh(True)
            initial = _Model()
            assert await scope.runner(connections, initial, clock=clock).run_once()
            before = await scope.view()
            before_revisions = _group_revisions(database, scope.id)
            previous = cast(dict[str, object], before["profile"])
            for group in (communication, constraints):
                assert previous[group] == [
                    {"text": initial.text, "fact_ids": [timed.id.value]}
                ]

            clock.at = 120.0
            model = _Model("到期后重算")
            runner = scope.runner(connections, model, clock=clock)
            if expiry_processed:
                assert not await runner.run_once()
                _make_claimable(database, scope.id)
                assert await runner.run_once()

            async with storage.session(scope.id, write=True) as writer:
                if change == "delete":
                    await writer.delete_facts((timed.id,))
                elif change == "erase":
                    assert await writer.delete_session_data(origin_session) == {
                        timed.id
                    }
                elif change == "move":
                    await writer.save_facts(
                        (replace(timed, profile_groups=(constraints,)),),
                        changed_at=clock.at,
                    )
                elif change == "sensitive":
                    await writer.save_facts(
                        (replace(timed, sensitive=True),), changed_at=clock.at
                    )
                else:
                    await writer.save_facts(
                        (timed.shorten_validity(110.0),), changed_at=clock.at
                    )

            pending = await scope.view()
            if expiry_processed:
                assert pending["refresh_status"] == "succeeded"
                assert not await runner.run_once()
            else:
                assert pending["refresh_status"] == "queued"
                _make_claimable(database, scope.id)
                assert await runner.run_once()
            assert model.prompts == [[communication.value, constraints.value]]

            refreshed = await scope.view()
            assert refreshed["refresh_status"] == "succeeded"
            profile = cast(dict[str, object], refreshed["profile"])
            for group in (communication, constraints):
                assert profile[group] == [
                    {"text": model.text, "fact_ids": [group.value]}
                ]
            revisions = _group_revisions(database, scope.id)
            assert revisions[preferences] == before_revisions[preferences]
            assert profile[preferences] == previous[preferences]

    run(exercise())


@pytest.mark.parametrize("growth", ["other_group", "same_group_history"])
def test_group_query_buffers_stay_bounded_as_facts_grow(
    database: PgDatabase, growth: str
) -> None:
    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            basic = await _write_fact(scope, storage, "driving-fact")
            with psycopg.connect(database.admin) as conn:
                same_group = growth == "same_group_history"

                def seed(start: int, end: int) -> None:
                    conn.execute(
                        """
                        INSERT INTO mnemotrace.facts
                            (space_id, fact_id, subject_id, predicate, object_value, text, valid_at,
                             invalid_at, expired_at, recorded_at, labels, sensitive, source,
                             profile_groups, lexemes)
                        SELECT %(space)s, 'growth-' || i, %(subject)s, 'tool', 'fixture', 'fixture',
                            CASE WHEN %(same_group)s THEN CASE WHEN i %% 2 = 0 THEN 20 ELSE 110 END END,
                            CASE WHEN %(same_group)s THEN CASE WHEN i %% 2 = 0 THEN 90 ELSE 120 END END,
                            CASE WHEN %(same_group)s THEN 10 END,
                            10, '{}', false, 'extracted', %(groups)s::text[], ''::tsvector
                        FROM generate_series(%(start)s::integer, %(end)s::integer) AS i
                        """,
                        {
                            "space": scope.id.value,
                            "subject": basic.subject.entity_id.value,
                            "same_group": same_group,
                            "groups": ["basic" if same_group else "preferences"],
                            "start": start,
                            "end": end,
                        },
                    )
                    conn.execute(
                        """
                        INSERT INTO mnemotrace.fact_provenance
                            (space_id, fact_id, position, message_id, support_group)
                        SELECT %s, 'growth-' || i, 0, %s, 0
                        FROM generate_series(%s::integer, %s::integer) AS i
                        """,
                        (scope.id.value, basic.provenance[0].value, start, end),
                    )
                    conn.execute("ANALYZE mnemotrace.facts")

                def evidence_buffers() -> int:
                    row = conn.execute(
                        sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ")
                        + _group_evidence_query(ProfileGroup.BASIC),
                        {
                            "space": scope.id.value,
                            "subject": basic.subject.entity_id.value,
                            "at": 100.0,
                            "limit": MAX_PROFILE_FACTS + 1,
                        },
                    ).fetchone()
                    assert row is not None
                    plan = row[0][0]["Plan"]
                    assert plan["Actual Rows"] == 1
                    return int(plan["Shared Hit Blocks"] + plan["Shared Read Blocks"])

                seed(1, 500)
                small = evidence_buffers()
                seed(501, 10000)
                assert evidence_buffers() <= small + 8

    run(exercise())
