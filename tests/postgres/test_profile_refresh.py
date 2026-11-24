"""验证画像 HTTP、持久刷新与发布竞态。"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.rows import TupleRow

from mnemotrace import worker
from mnemotrace.adapters.inference import InferenceClients
from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres import storage as pg_storage
from mnemotrace.adapters.postgres.identity import issue_key
from mnemotrace.adapters.postgres.operations import PostgresOperations
from mnemotrace.adapters.postgres.pool import ConnectionPool
from mnemotrace.adapters.postgres.profile_refresh import PostgresProfileRefresh
from mnemotrace.adapters.postgres.session import PostgresSession
from mnemotrace.adapters.runtime import SystemClock, UuidGenerator
from mnemotrace.application.operations import OperationRunner
from mnemotrace.application.profile_refresh import (
    ProfileEvidenceVersion,
    ProfileRefreshRunner,
)
from mnemotrace.application.remember import Remember
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import FactId, SessionId, SpaceId
from mnemotrace.domain.profile import Profile, ProfileEntry
from mnemotrace.domain.space import Space
from mnemotrace.domain.values import Predicate
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
        return {
            **_EMPTY,
            "basic": [{"text": self.text, "evidence_numbers": [1]}],
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
    class MutableClock(FakeClock):
        at = 100.0

        def now(self) -> float:
            return self.at

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            clock = MutableClock()
            first = await _write_fact(scope, storage, "first")
            if change == "activation":
                await _write_fact(scope, storage, "later", valid_at=101.0)
            elif change == "expiry":
                await _write_fact(scope, storage, "expiring", invalid_at=101.0)
            await scope.submit()
            read_versions = 0
            get_version = PostgresProfileRefresh.evidence_version

            async def change_after_read(reader: SpaceReader) -> ProfileEvidenceVersion:
                nonlocal read_versions
                version = await get_version(reader)
                if not isinstance(reader, PostgresSession):
                    read_versions += 1
                    if read_versions == 2:
                        if change == "hard_delete":
                            async with storage.session(scope.id, write=True) as writer:
                                await writer.delete_facts((first.id,))
                        else:
                            clock.at = 101.0
                return version

            with monkeypatch.context() as patch:
                patch.setattr(
                    PostgresProfileRefresh,
                    "evidence_version",
                    staticmethod(change_after_read),
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
            assert (await scope.view())["refresh_status"] == "idle"
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
            if promote:
                assert view["refresh_status"] == "succeeded"
                assert view["profile"] != original
            else:
                assert view["refresh_status"] == "queued"
                assert view["profile"] == original
                assert await scope.runner(connections, _Model("重开后结果")).run_once()
                assert (await scope.view())["refresh_status"] == "succeeded"

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
                attempts = 3 if outcome == "expired" else 1
                for attempt in range(1, attempts + 1):
                    abandoned = await queue.claim()
                    assert abandoned is not None and abandoned.space_id == scope.id
                    assert abandoned.attempt == attempt
                    if attempt < attempts:
                        _make_claimable(database, scope.id)
                await change_facts()
                _make_claimable(database, scope.id)
                if outcome == "expired":
                    assert await queue.claim() is None
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
            if outcome in ("stale", "permanent_error", "expired"):
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
                assert await scope.view() == retrying
                _make_claimable(database, scope.id)
            if outcome != "unchanged":
                async with pool(database.runtime) as new_connections:
                    model = _Model()
                    assert await scope.runner(new_connections, model).run_once()
                    assert model.prompts == [["followup", "same-batch", "first"]]
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
            await _write_fact(scope, storage, "waiting")
            assert not await scope.runner(connections, _Model()).run_once()
            base = scope.path.removesuffix("/profile")
            response = await scope.client.post(
                base + "/purge",
                headers={**scope.headers, "Idempotency-Key": "purge-auto-profile"},
            )
            assert response.status_code == 204
            assert (await scope.view())["refresh_status"] == "idle"
            assert (await scope.view())["profile"] is None
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
                    evidence = fact(
                        Entity.space_user(scope.id), "rollback", provenance=(origin.id,)
                    )
                    await writer.save_facts((evidence,), changed_at=20.0)
                    raise RuntimeError("回滚整个事务")
            assert (await scope.view())["refresh_status"] == "idle"
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
                **_EMPTY,
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
                [claimed] = [
                    job
                    for job in await asyncio.gather(queue.claim(), queue.claim())
                    if job is not None
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
                        1,
                        stopping,
                        None,
                        extraction_timezone=ZoneInfo("UTC"),
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
                        1,
                        stopping,
                        None,
                        extraction_timezone=ZoneInfo("UTC"),
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


@pytest.mark.parametrize("change", ["new_fact", "erase_session", "append_evidence"])
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
            await scope.submit()

            async def mutate() -> None:
                async with asyncio.timeout(3):
                    if change == "new_fact":
                        await _write_fact(scope, storage, "new", valid_at=80.0)
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
                else:
                    assert fresh.prompts == []
                    assert view["profile"] == _EMPTY

    run(exercise())


def test_natural_expiry_hides_only_the_dependent_entries_during_failed_refresh(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = 99.0
    monkeypatch.setattr(profiles, "time", lambda: at)

    async def exercise() -> None:
        nonlocal at
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            expires = await _write_fact(scope, storage, "expiring", invalid_at=100.0)
            current = await _write_fact(scope, storage, "regular")
            stored = Profile(
                scope.id,
                {
                    "basic": (
                        ProfileEntry("需要两个引用", (expires.id, current.id)),
                        ProfileEntry("有独立支撑", (current.id,)),
                    )
                },
                90.0,
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
            at = 100.0
            await scope.submit()
            pending = await scope.view()
            assert pending["profile"] == {
                **_EMPTY,
                "basic": [{"text": "有独立支撑", "fact_ids": ["regular"]}],
            }
            assert pending["updated_at"] == before["updated_at"]
            model = _Model(failure=InvalidStructuredOutput("正文不得进入失败响应"))
            assert await scope.runner(connections, model).run_once()
            failed = await scope.view()
            assert failed == {
                **pending,
                "refresh_status": "failed",
                "refresh_error": {"code": "invalid_model_output"},
            }
            assert model.prompts == [["regular"]]
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

    @asynccontextmanager
    async def transaction(
        conn: AsyncConnection[TupleRow], *, write: bool
    ) -> AsyncIterator[None]:
        async with real_transaction(conn, write=write):
            yield
            if write and not commit_succeeded:
                raise CommitOutcomeUnknown("测试回滚")
        if write:
            raise CommitOutcomeUnknown("测试提交确认丢失")

    async def exercise() -> None:
        async with (
            _http_space(database) as scope,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner)
            await _write_fact(scope, storage, "source")
            await scope.submit()
            runner = scope.runner(connections, _Model())
            with monkeypatch.context() as patch:
                patch.setattr(pg_storage, "_session_transaction", transaction)
                assert await runner.run_once()
            view = await scope.view()
            if commit_succeeded:
                assert view["refresh_status"] == "succeeded"
                assert view["profile"] is not None
                assert not await runner.run_once()
            else:
                assert view["refresh_status"] == "queued"
                assert view["profile"] is None
                assert isinstance(view["next_retry_at"], str)
                _make_claimable(database, scope.id)
                assert await runner.run_once()
                assert (await scope.view())["refresh_status"] == "succeeded"

    run(exercise())


def test_followup_profile_prioritizes_changes_since_its_successful_snapshot(
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
            assert followup.prompts == [["new", "existing"]]

    run(exercise())
