"""验证异步记忆写入的 HTTP、租约和提交边界。"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection, AsyncTransaction
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from mnemotrace_next import worker
from mnemotrace_next.adapters.inference import InferenceClients, InferenceRequestError
from mnemotrace_next.adapters.postgres.identity import issue_key
from mnemotrace_next.adapters.postgres.operations import PostgresOperations
from mnemotrace_next.adapters.postgres.storage import PostgresStorage
from mnemotrace_next.adapters.runtime import SystemClock, UuidGenerator
from mnemotrace_next.application import remember as remember_app
from mnemotrace_next.application.operations import (
    MAX_ATTEMPTS,
    ClaimedOperation,
    OperationRunner,
)
from mnemotrace_next.application.remember import Remember, RememberResult
from mnemotrace_next.domain.ids import SessionId, SpaceId
from mnemotrace_next.ports.inference import LLM, InvalidStructuredOutput
from mnemotrace_next.ports.storage import SpaceSession, SpaceUnavailable
from mnemotrace_next.runtime_settings import RuntimeSettings
from mnemotrace_next.server.app import create_app
from mnemotrace_next.server.settings import Settings

from ..ports.fakes import FakeEmbedder, FakeLLM
from .conftest import PgDatabase
from .sample import pool, run

pytestmark = pytest.mark.postgres
_FACT = "项目 Atlas-903 默认导出 XLSX"


class _Model(FakeLLM):
    def __init__(self, *, relative_day: int | None = None) -> None:
        self.prompts: list[dict[str, object]] = []
        self._relative_day = relative_day

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        request = json.loads(prompt)
        self.prompts.append(request)
        (message,) = request["messages"]
        expected_text = _FACT if self._relative_day is None else f"今天起，{_FACT}"
        if message["content"] != expected_text:
            return {"facts": []}
        return {
            "facts": [
                {
                    "subject": {"kind": "entity", "value": "Atlas-903"},
                    "predicate": "export_format",
                    "object": {"kind": "literal", "value": "XLSX"},
                    "text": _FACT,
                    "valid_at_evidence": {
                        "kind": "message_time"
                        if self._relative_day is None
                        else "relative_date",
                        "source_number": message["number"],
                        "value": None,
                        "days": self._relative_day,
                    },
                    "invalid_at_evidence": None,
                    "source_numbers": [message["number"]],
                    "labels": [],
                    "sensitive": False,
                }
            ]
        }


@dataclass(frozen=True)
class _Scope:
    client: AsyncClient
    path: str
    owner: str
    headers: dict[str, str]
    foreign_headers: dict[str, str]
    connections: AsyncConnectionPool[AsyncConnection[TupleRow]]

    @property
    def space_id(self) -> SpaceId:
        return SpaceId(self.path.rsplit("/", 1)[1])

    async def new_session(self) -> str:
        response = await self.client.post(self.path + "/sessions", headers=self.headers)
        assert response.status_code == 201, response.text
        return response.headers["Location"]

    async def submit(self, session: str, key: str, text: str) -> Response:
        return await self.client.post(
            session + "/remember",
            headers={**self.headers, "Idempotency-Key": key},
            json={"messages": [{"role": "user", "content": text}]},
        )

    async def status(self, path: str) -> Response:
        return await self.client.get(path, headers=self.headers)

    def runner(self, model: LLM) -> OperationRunner:
        return OperationRunner(
            PostgresOperations(self.connections),
            lambda job: Remember(
                space_id=job.space_id,
                storage=PostgresStorage(self.connections, owner_id=job.owner_id),
                llm=model,
                embedder=FakeEmbedder(),
                clock=SystemClock(),
                ids=UuidGenerator(),
            ),
            worker._is_transient,
        )


@asynccontextmanager
async def _scope(
    database: PgDatabase, *, limit: int = 10, configured: bool = True
) -> AsyncIterator[_Scope]:
    owner = uuid4().hex
    with psycopg.connect(database.key_admin) as connection:
        key = issue_key(connection, owner)
        foreign = issue_key(connection, uuid4().hex)
    config = {
        "MNEMOTRACE_NEXT_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "MNEMOTRACE_NEXT_LLM_MODEL": "synthetic",
        "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_schema",
        "MNEMOTRACE_NEXT_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
        "MNEMOTRACE_NEXT_EMBED_MODEL": "synthetic",
        "MNEMOTRACE_NEXT_EMBED_REVISION": "test",
        "MNEMOTRACE_NEXT_EMBED_DIM": "2",
    }
    env = {
        "MNEMOTRACE_NEXT_POSTGRES_DSN": database.runtime,
        "MNEMOTRACE_NEXT_QUEUE_LIMIT": str(limit),
        **(config if configured else {}),
    }
    app = create_app(Settings.from_env(env))
    headers = {"Authorization": f"Bearer {key.token}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        pool(database.runtime) as connections,
    ):
        app.state.embedder = FakeEmbedder()
        created = await client.post(
            "/v1/spaces", headers=headers, json={"name": "任务测试"}
        )
        assert created.status_code == 201, created.text
        scope = _Scope(
            client,
            created.headers["Location"],
            owner,
            headers,
            {"Authorization": f"Bearer {foreign.token}"},
            connections,
        )
        try:
            yield scope
        finally:
            storage = PostgresStorage(connections, owner_id=owner)
            with suppress(SpaceUnavailable):
                async with storage.session(scope.space_id, write=True) as writer:
                    await writer.purge_space()


def test_http_admission_idempotency_capacity_and_publication(
    database: PgDatabase,
) -> None:
    """停机期间有界接收，启动处理后可从召回追溯本批原文。"""

    async def exercise() -> None:
        async with _scope(database, limit=2) as scope:
            session = await scope.new_session()
            one, replay = await asyncio.gather(
                scope.submit(session, "first", _FACT),
                scope.submit(session, "first", _FACT),
            )
            assert one.status_code == replay.status_code == 202, one.text
            path = one.headers["Location"]
            assert path == replay.headers["Location"]
            assert one.json() == replay.json()
            assert one.json()["status"] == "queued"
            assert "message_ids" not in one.json()
            two = await scope.submit(session, "second", "继续")
            assert two.status_code == 202, two.text
            full = await scope.submit(session, "third", "尚未接收")
            assert (full.status_code, full.json()["code"]) == (
                503,
                "remember_queue_full",
            )
            different = await scope.submit(session, "first", "异参重放")
            assert (different.status_code, different.json()["code"]) == (
                409,
                "idempotency_conflict",
            )
            assert (await scope.submit(session, "first", _FACT)).headers[
                "Location"
            ] == path
            denied = await scope.client.get(path, headers=scope.foreign_headers)
            assert (denied.status_code, denied.json()["code"]) == (
                404,
                "space_not_found",
            )
            before = await scope.client.post(
                scope.path + "/recall", headers=scope.headers, json={"query": _FACT}
            )
            assert before.json()["evidence"] == []

            model = _Model()
            runner = scope.runner(model)
            assert await runner.run_once()
            result = (await scope.status(path)).json()
            assert result["status"] == "succeeded"
            assert result["fact_change_count"] == 1
            audit_path = path + "/fact-changes"
            audit = (await scope.status(audit_path)).json()
            assert audit["status"] == "succeeded" and len(audit["items"]) == 1
            assert audit["items"][0]["before"] is None
            (message_id,) = result["message_ids"]
            recalled = await scope.client.post(
                scope.path + "/recall", headers=scope.headers, json={"query": _FACT}
            )
            assert recalled.json()["evidence"][0]["source_message_ids"] == [message_id]
            original = await scope.status(scope.path + "/messages/" + message_id)
            assert original.json()["content"] == _FACT
            assert datetime.fromisoformat(
                original.json()["event_time"]
            ) == datetime.fromisoformat(result["created_at"])
            assert (await scope.submit(session, "first", _FACT)).json() == result
            assert (await scope.status(audit_path)).json() == audit
            assert await runner.run_once()
            assert model.prompts[1]["previous"] == [
                {
                    "number": 1,
                    "role": "user",
                    "content": _FACT,
                    "reference_time": datetime.fromisoformat(
                        original.json()["event_time"]
                    ).isoformat(),
                }
            ]
            finished = (await scope.status(two.headers["Location"])).json()
            assert finished["status"] == "succeeded"
            assert finished["fact_change_count"] == 0
            assert len(finished["message_ids"]) == 1
            assert (
                await scope.status(two.headers["Location"] + "/fact-changes")
            ).json() == {
                "status": "succeeded",
                "items": [],
                "next_cursor": None,
            }
            async with scope.connections.connection() as conn:
                cursor = await conn.execute(
                    "SELECT count(*) FROM mnemotrace_next.remember_operations "
                    "WHERE space_id = %s AND messages IS NOT NULL",
                    (scope.space_id.value,),
                )
                assert await cursor.fetchone() == (0,)
            assert not await runner.run_once()

    run(exercise())


@pytest.mark.parametrize(
    ("zone", "valid_at"),
    [("UTC", "2026-11-21T00:00:00Z"), ("Asia/Shanghai", "2026-11-21T16:00:00Z")],
)
def test_http_write_worker_uses_extraction_timezone(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, zone: str, valid_at: str
) -> None:
    """午夜后的同一条原文按 worker 时区解析当日生效时刻。"""

    @asynccontextmanager
    async def inference(
        _env: Mapping[str, str], *, llm: bool, embed: bool
    ) -> AsyncIterator[InferenceClients]:
        yield InferenceClients(_Model(relative_day=0), FakeEmbedder())

    async def consume_one(runner: OperationRunner, stopping: asyncio.Event) -> None:
        assert await runner.run_once()
        stopping.set()

    monkeypatch.setattr(worker, "open_inference", inference)
    monkeypatch.setattr(worker, "_consume", consume_one)

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            admitted = await scope.client.post(
                session + "/remember",
                headers={**scope.headers, "Idempotency-Key": "today"},
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": f"今天起，{_FACT}",
                            "event_time": "2026-11-22T00:30:00+08:00",
                        }
                    ]
                },
            )
            assert admitted.status_code == 202
            await asyncio.wait_for(
                worker._run(
                    RuntimeSettings(database.runtime),
                    1,
                    extraction_timezone=ZoneInfo(zone),
                ),
                timeout=5,
            )
            result = (await scope.status(admitted.headers["Location"])).json()
            assert result["status"] == "succeeded"
            facts = await scope.client.get(
                scope.path + "/facts", params={"state": "all"}, headers=scope.headers
            )
            assert facts.status_code == 200
            (item,) = facts.json()["items"]
            assert item["valid_at"] == valid_at
            (message_id,) = result["message_ids"]
            original = await scope.status(f"{scope.path}/messages/{message_id}")
            assert original.status_code == 200
            assert original.json()["event_time"] == "2026-11-21T16:30:00Z"

    run(exercise())


def test_http_rejects_invalid_batch_or_missing_models(database: PgDatabase) -> None:
    """模型未配置时不接收原文，格式错误在入队前定位到请求字段。"""

    async def exercise() -> None:
        async with _scope(database, configured=False) as scope:
            session = await scope.new_session()
            missing = await scope.client.post(
                session + "/remember",
                headers=scope.headers,
                json={"messages": [{"role": "user", "content": "合成消息"}]},
            )
            assert missing.status_code == 422
            assert missing.json()["errors"][0]["location"] == "header"
            invalid = await scope.client.post(
                session + "/remember",
                headers={**scope.headers, "Idempotency-Key": "key"},
                json={
                    "messages": [{"role": "system", "content": " ", "event_time": 7}]
                },
            )
            assert invalid.status_code == 422
            assert {error["path"] for error in invalid.json()["errors"]} == {
                "/messages/0/role",
                "/messages/0/content",
                "/messages/0/event_time",
            }
            invalid_unicode = await scope.client.post(
                session + "/remember",
                headers={
                    **scope.headers,
                    "Idempotency-Key": "key",
                    "Content-Type": "application/json",
                },
                content=json.dumps(
                    {"messages": [{"role": "user", "content": chr(0xD800)}]}
                ).encode("ascii"),
            )
            assert invalid_unicode.status_code == 422
            assert invalid_unicode.json()["errors"] == [
                {
                    "location": "body",
                    "path": "/messages/0/content",
                    "code": "invalid_value",
                }
            ]
            wrong = await scope.submit(
                scope.path + "/sessions/missing", "key", "合成消息"
            )
            assert (wrong.status_code, wrong.json()["code"]) == (
                404,
                "session_not_found",
            )
            unavailable = await scope.submit(session, "key", "合成消息")
            assert (unavailable.status_code, unavailable.json()["code"]) == (
                503,
                "write_models_unavailable",
            )
            assert (
                await scope.status(scope.path + "/operations/missing")
            ).status_code == 404
            assert await PostgresOperations(scope.connections).claim() is None

    run(exercise())


def test_workers_can_process_different_sessions_at_the_same_time(
    database: PgDatabase,
) -> None:
    """首个模型请求尚在处理时，另一 worker 只领取其他会话的任务。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session, sibling = await scope.new_session(), await scope.new_session()
            first = await scope.submit(session, "head", _FACT)
            second = await scope.submit(session, "tail", "同会话后批")
            other = await scope.submit(sibling, "other", "另一会话")
            started = asyncio.Event()
            release = asyncio.Event()

            class Paused(_Model):
                async def complete_json(
                    self, *, system: str, prompt: str, schema: Mapping[str, object]
                ) -> Mapping[str, object]:
                    started.set()
                    await release.wait()
                    return await super().complete_json(
                        system=system, prompt=prompt, schema=schema
                    )

            async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
                running = tasks.create_task(scope.runner(Paused()).run_once())
                await started.wait()
                assert (await scope.status(first.headers["Location"])).json()[
                    "status"
                ] == "running"
                assert (await scope.status(scope.path + "/facts")).json()["items"] == []
                parallel = scope.runner(_Model())
                assert await parallel.run_once()
                assert (await scope.status(other.headers["Location"])).json()[
                    "status"
                ] == "succeeded"
                assert (await scope.status(second.headers["Location"])).json()[
                    "status"
                ] == "queued"
                assert not await parallel.run_once()
                release.set()
            assert running.result()
            assert await parallel.run_once()
            assert (await scope.status(second.headers["Location"])).json()[
                "status"
            ] == "succeeded"

    run(exercise())


@pytest.mark.parametrize("erase_after_snapshot", [False, True])
def test_erasing_another_session_preserves_only_work_without_an_old_snapshot(
    database: PgDatabase, erase_after_snapshot: bool
) -> None:
    """已排队的其他会话可从新快照开始，已读旧快照的写入仍失效。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            erased, retained = await scope.new_session(), await scope.new_session()
            old = await scope.submit(erased, "old", "合成旧原文")
            assert await scope.runner(_Model()).run_once()
            assert (await scope.status(old.headers["Location"])).json()[
                "status"
            ] == "succeeded"
            queued = await scope.submit(retained, "next", _FACT)
            storage = PostgresStorage(scope.connections, owner_id=scope.owner)

            async def erase() -> None:
                async with storage.session(scope.space_id, write=True) as writer:
                    await writer.delete_session_data(
                        SessionId(erased.rsplit("/", 1)[1])
                    )

            class EraseWhileExtracting(_Model):
                async def complete_json(
                    self, *, system: str, prompt: str, schema: Mapping[str, object]
                ) -> Mapping[str, object]:
                    await erase()
                    return await super().complete_json(
                        system=system, prompt=prompt, schema=schema
                    )

            if not erase_after_snapshot:
                await erase()
            model = EraseWhileExtracting() if erase_after_snapshot else _Model()
            assert await scope.runner(model).run_once()
            assert (await scope.status(old.headers["Location"])).status_code == 404
            result = (await scope.status(queued.headers["Location"])).json()
            assert len(model.prompts) == 1
            if erase_after_snapshot:
                assert result["status"] == "failed"
                assert result["error"]["code"] == "write_invalidated"
                assert (await scope.status(scope.path + "/facts")).json()["items"] == []
            else:
                assert result["status"] == "succeeded"
                assert result["fact_change_count"] == 1
                assert (await scope.submit(retained, "next", _FACT)).json() == result

    run(exercise())


@pytest.mark.parametrize(
    "chat_config",
    [
        {"MNEMOTRACE_NEXT_LLM_API_KEY": "fixture-key"},
        {
            "MNEMOTRACE_NEXT_LLM_BASE_URL": "https://models.example.invalid/v1",
            "MNEMOTRACE_NEXT_LLM_MODEL": "fixture",
            "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_schema",
        },
    ],
)
def test_incomplete_llm_config_keeps_recall_and_existing_operations_available(
    database: PgDatabase, chat_config: dict[str, str]
) -> None:
    """HTTP 缺少补全必填项时可召回、查询和重放已接收任务。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            accepted = await scope.submit(session, "accepted", _FACT)
            assert accepted.status_code == 202
            assert await scope.runner(_Model()).run_once()
            operation = accepted.headers["Location"]
            app = create_app(
                Settings.from_env(
                    {
                        "MNEMOTRACE_NEXT_POSTGRES_DSN": database.runtime,
                        "MNEMOTRACE_NEXT_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
                        "MNEMOTRACE_NEXT_EMBED_MODEL": "fixture",
                        "MNEMOTRACE_NEXT_EMBED_REVISION": "test",
                        "MNEMOTRACE_NEXT_EMBED_DIM": "2",
                        **chat_config,
                    }
                )
            )
            async with (
                app.router.lifespan_context(app),
                AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert app.state.embedder is not None
                app.state.embedder = FakeEmbedder()
                assert (await client.get("/readyz")).status_code == 200
                result = await client.get(operation, headers=scope.headers)
                assert result.json()["status"] == "succeeded"
                recall = await client.post(
                    scope.path + "/recall", headers=scope.headers, json={"query": _FACT}
                )
                assert recall.json()["evidence"][0]["text"] == _FACT
                for key, code in (("accepted", 202), ("new", 503)):
                    response = await client.post(
                        session + "/remember",
                        headers={**scope.headers, "Idempotency-Key": key},
                        json={"messages": [{"role": "user", "content": _FACT}]},
                    )
                    assert response.status_code == code
                    if key == "accepted":
                        assert response.headers["Location"] == operation
                        assert response.json() == result.json()
                    else:
                        assert response.json()["code"] == "write_models_unavailable"

    run(exercise())


def _set_due(database: PgDatabase, operation_id: str) -> None:
    with psycopg.connect(database.admin) as conn:
        conn.execute(
            """
            UPDATE mnemotrace_next.remember_operations
            SET lease_until = CASE WHEN status = 'running' THEN clock_timestamp() END,
                next_retry_at = CASE WHEN status = 'queued' THEN clock_timestamp() END
            WHERE operation_id = %s
            """,
            (operation_id,),
        )


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        pytest.param(TimeoutError(), "operation_timeout", id="attempt-timeout"),
        pytest.param(
            InferenceRequestError("rate_limit", status_code=429),
            "inference_failed",
            id="model-rate-limit",
        ),
        pytest.param(
            InferenceRequestError("connection"),
            "inference_failed",
            id="model-disconnect",
        ),
        pytest.param(
            InferenceRequestError("upstream", status_code=503),
            "inference_failed",
            id="model-unavailable",
        ),
        pytest.param(
            psycopg.OperationalError(), "database_unavailable", id="postgres-disconnect"
        ),
    ],
)
def test_retry_holds_session_head_and_terminal_failure_releases_it(
    database: PgDatabase, failure: Exception, code: str
) -> None:
    """瞬时错误退避时阻塞同会话队尾，其他会话和终态后的任务仍继续。"""

    class Unreliable(_Model):
        async def complete_json(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> Mapping[str, object]:
            (message,) = json.loads(prompt)["messages"]
            if message["content"] == "失败的前批":
                raise failure
            return await super().complete_json(
                system=system, prompt=prompt, schema=schema
            )

    async def exercise() -> None:
        async with _scope(database) as scope:
            session, sibling = await scope.new_session(), await scope.new_session()
            first = await scope.submit(session, "first", "失败的前批")
            tail = await scope.submit(session, "tail", "同会话后批")
            other = await scope.submit(sibling, "parallel", "另一会话")
            first_path, tail_path, other_path = (
                response.headers["Location"] for response in (first, tail, other)
            )
            runner = scope.runner(Unreliable())
            assert await runner.run_once()
            pending = (await scope.status(first_path)).json()
            assert pending["status"] == "queued"
            assert datetime.fromisoformat(
                pending["next_retry_at"]
            ) > datetime.fromisoformat(pending["updated_at"])
            assert await runner.run_once()
            assert (await scope.status(other_path)).json()["status"] == "succeeded"
            assert (await scope.status(tail_path)).json()["status"] == "queued"
            assert not await runner.run_once()
            for _ in range(1, MAX_ATTEMPTS):
                _set_due(database, pending["operation_id"])
                assert await runner.run_once()
            failed = (await scope.status(first_path)).json()
            assert failed["status"] == "failed"
            assert failed["error"] == {
                "code": code,
                "stage": "extraction",
            }
            assert "message_ids" not in failed and "next_retry_at" not in failed
            assert (await scope.submit(session, "first", "失败的前批")).json() == failed
            rerun = await scope.submit(session, "new-key", "新键重新提交")
            assert rerun.headers["Location"] != first_path
            assert await runner.run_once()
            assert (await scope.status(tail_path)).json()["status"] == "succeeded"
            assert (await scope.status(rerun.headers["Location"])).json()[
                "status"
            ] == "queued"

    run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            InferenceRequestError("auth", status_code=401), id="authentication"
        ),
        pytest.param(InferenceRequestError("quota", status_code=429), id="quota"),
    ],
)
def test_permanent_inference_failure_skips_retry(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
    failure: InferenceRequestError,
) -> None:
    """凭据和账户额度错误首次失败即可终结任务。"""
    model = _Model()
    monkeypatch.setattr(model, "complete_json", AsyncMock(side_effect=failure))

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            first = await scope.submit(session, "first", _FACT)
            tail = await scope.submit(session, "tail", "后续任务")
            assert await scope.runner(model).run_once()
            failed = (await scope.status(first.headers["Location"])).json()
            assert failed["status"] == "failed"
            assert failed["error"] == {
                "code": "inference_failed",
                "stage": "extraction",
            }
            assert "next_retry_at" not in failed
            assert await scope.runner(_Model()).run_once()
            assert (await scope.status(tail.headers["Location"])).json()[
                "status"
            ] == "succeeded"

    run(exercise())


def test_expired_lease_cannot_mark_failure_or_discard_messages(
    database: PgDatabase,
) -> None:
    """过期 worker 的失败报告不能丢弃任务，后续领取可成功发布原文。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            submitted = await scope.submit(session, "first", _FACT)
            path = submitted.headers["Location"]
            queue = PostgresOperations(scope.connections)
            job = await queue.claim()
            assert (
                job is not None and job.operation_id == submitted.json()["operation_id"]
            )
            _set_due(database, job.operation_id)
            assert not await queue.has_active_lease(job)
            await queue.record_attempt_failure(
                job, "inference_failed", "extraction", None
            )
            updated = (await scope.status(path)).json()
            assert updated["status"] == "running"
            assert "error" not in updated

            assert await scope.runner(_Model()).run_once()
            recovered = (await scope.status(path)).json()
            assert recovered["status"] == "succeeded"
            (message_id,) = recovered["message_ids"]
            read = await scope.status(f"{scope.path}/messages/{message_id}")
            assert read.status_code == 200
            assert read.json()["content"] == _FACT

    run(exercise())


def test_expired_lease_rolls_back_then_recovers_and_has_a_crash_budget(
    database: PgDatabase,
) -> None:
    """过期 worker 的写入不发布，重新领取后可继续，反复崩溃最终放行。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            first = await scope.submit(session, "first", _FACT)
            path = first.headers["Location"]
            operation_id = first.json()["operation_id"]

            class MarkExpired(_Model):
                async def complete_json(
                    self, *, system: str, prompt: str, schema: Mapping[str, object]
                ) -> Mapping[str, object]:
                    _set_due(database, operation_id)
                    return await super().complete_json(
                        system=system, prompt=prompt, schema=schema
                    )

            assert await scope.runner(MarkExpired()).run_once()
            assert (await scope.status(path)).json()["status"] == "running"
            reader = PostgresStorage(scope.connections, owner_id=scope.owner)
            async with reader.session(scope.space_id) as snapshot:
                assert not await snapshot.last_messages(
                    SessionId(session.rsplit("/", 1)[1]), 10
                )
            assert (await scope.status(scope.path + "/facts")).json()["items"] == []
            assert (await scope.status(path + "/fact-changes")).json() == {
                "status": "running",
                "items": [],
                "next_cursor": None,
            }
            assert await scope.runner(_Model()).run_once()
            assert (await scope.status(path)).json()["status"] == "succeeded"
            assert (
                len((await scope.status(path + "/fact-changes")).json()["items"]) == 1
            )

            crashed = await scope.submit(session, "crash", "反复崩溃")
            tail = await scope.submit(session, "tail", "后批")
            queue = PostgresOperations(scope.connections)
            for attempt in range(1, MAX_ATTEMPTS + 1):
                job = await queue.claim()
                assert (
                    job is not None
                    and job.operation_id == crashed.json()["operation_id"]
                )
                assert job.attempt == attempt
                _set_due(database, job.operation_id)
            assert await queue.claim() is None
            state = (await scope.status(crashed.headers["Location"])).json()
            assert state["status"] == "failed"
            assert state["error"] == {"code": "worker_lease_expired"}
            assert (
                await scope.status(crashed.headers["Location"] + "/fact-changes")
            ).json() == {"status": "failed", "items": [], "next_cursor": None}
            assert await scope.runner(_Model()).run_once()
            assert (await scope.status(tail.headers["Location"])).json()[
                "status"
            ] == "succeeded"

    run(exercise())


@pytest.mark.parametrize("publish_committed", [True, False])
def test_lost_commit_acknowledgements_reconcile_by_key_and_operation_status(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, publish_committed: bool
) -> None:
    """接收与发布确认丢失时，同一任务和原文均只持久化一次。"""
    original_exit = AsyncTransaction.__aexit__
    complete = PostgresOperations.complete
    lose_ack = False
    roll_back = False
    publish_fault_injected = False

    async def drop_ack(
        transaction: AsyncTransaction,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        nonlocal lose_ack, roll_back
        if roll_back:
            roll_back = False
            failure = psycopg.errors.ConnectionFailure("合成提交阶段断连")
            await original_exit(transaction, type(failure), failure, None)
            raise failure
        result = await original_exit(transaction, exc_type, exc_value, traceback)
        if lose_ack and transaction.status is transaction.Status.COMMITTED:
            lose_ack = False
            raise psycopg.errors.ConnectionFailure("合成提交确认丢失")
        return result

    async def mark_publish(
        queue: PostgresOperations,
        job: ClaimedOperation,
        writer: SpaceSession,
        result: RememberResult,
    ) -> None:
        nonlocal lose_ack, roll_back, publish_fault_injected
        await complete(queue, job, writer, result)
        if not publish_fault_injected:
            publish_fault_injected = True
            lose_ack = publish_committed
            roll_back = not publish_committed

    async def exercise() -> None:
        nonlocal lose_ack
        async with _scope(database) as scope:
            session = await scope.new_session()
            with monkeypatch.context() as changes:
                changes.setattr(AsyncTransaction, "__aexit__", drop_ack)
                lose_ack = True
                uncertain = await scope.submit(session, "stable", _FACT)
                assert (uncertain.status_code, uncertain.json()["code"]) == (
                    503,
                    "commit_outcome_unknown",
                )
                accepted = await scope.submit(session, "stable", _FACT)
                assert accepted.status_code == 202
                audit_path = accepted.headers["Location"] + "/fact-changes"
                changes.setattr(PostgresOperations, "complete", mark_publish)
                runner = scope.runner(_Model())
                assert await runner.run_once()
                if not publish_committed:
                    pending = (await scope.status(accepted.headers["Location"])).json()
                    assert pending["status"] == "queued"
                    assert "next_retry_at" in pending
                    assert (await scope.status(audit_path)).json() == {
                        "status": "queued",
                        "items": [],
                        "next_cursor": None,
                    }
                    storage = PostgresStorage(scope.connections, owner_id=scope.owner)
                    async with storage.session(scope.space_id) as reader:
                        assert not await reader.last_messages(
                            SessionId(pending["session_id"]), 10
                        )
                    _set_due(database, pending["operation_id"])
                    assert await runner.run_once()
                assert not await runner.run_once()
                state = (await scope.status(accepted.headers["Location"])).json()
                assert state["status"] == "succeeded"
                replay = await scope.submit(session, "stable", _FACT)
                assert replay.json() == state
                audit = (await scope.status(audit_path)).json()
                assert audit["status"] == "succeeded" and len(audit["items"]) == 1
                storage = PostgresStorage(scope.connections, owner_id=scope.owner)
                async with storage.session(scope.space_id) as reader:
                    messages = await reader.last_messages(
                        SessionId(state["session_id"]), 10
                    )
                    assert [message.id.value for message in messages] == state[
                        "message_ids"
                    ]

    run(exercise())


@pytest.mark.parametrize("target", ["space_contents", "session", "space"])
def test_erasing_inference_in_progress_removes_the_pending_payload(
    database: PgDatabase,
    target: str,
) -> None:
    """HTTP 擦除清除队列副本并阻止迟到结果发布。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            queued = await scope.submit(session, "key", _FACT)
            waiting = await scope.submit(session, "waiting", "合成排队消息")

            class EraseDuringInference(_Model):
                async def complete_json(
                    self, *, system: str, prompt: str, schema: Mapping[str, object]
                ) -> Mapping[str, object]:
                    if target == "space_contents":
                        response = await scope.client.post(
                            scope.path + "/purge",
                            headers={**scope.headers, "Idempotency-Key": "erase"},
                        )
                    else:
                        path = session if target == "session" else scope.path
                        response = await scope.client.delete(
                            path, headers=scope.headers
                        )
                    assert response.status_code == 204
                    return await super().complete_json(
                        system=system, prompt=prompt, schema=schema
                    )

            assert await scope.runner(EraseDuringInference()).run_once()
            assert (await scope.status(queued.headers["Location"])).status_code == 404
            assert (await scope.status(waiting.headers["Location"])).status_code == 404
            facts = await scope.status(scope.path + "/facts")
            if target == "space":
                assert (facts.status_code, facts.json()["code"]) == (
                    404,
                    "space_not_found",
                )
            else:
                assert facts.json()["items"] == []
            assert (await scope.submit(session, "key", _FACT)).status_code == 404
            assert await PostgresOperations(scope.connections).claim() is None

    run(exercise())


@pytest.mark.parametrize("commit_fault", ["none", "before_commit", "after_commit"])
def test_idempotent_purge_preserves_memory_written_after_the_first_commit(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch, commit_fault: str
) -> None:
    """并发同键只清空一次，提交确认丢失后重放也保留之后的写入。"""
    original_exit = AsyncTransaction.__aexit__
    injected = False

    async def drop_ack(
        transaction: AsyncTransaction,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        nonlocal injected
        if not injected and not transaction.savepoint_name and exc_type is None:
            injected = True
            failure = psycopg.errors.ConnectionFailure("合成清空提交确认丢失")
            if commit_fault == "before_commit":
                await original_exit(transaction, type(failure), failure, None)
            else:
                await original_exit(transaction, exc_type, exc_value, traceback)
            raise failure
        return await original_exit(transaction, exc_type, exc_value, traceback)

    async def oversized_body() -> AsyncIterator[bytes]:
        yield b"x" * (64 * 1024)
        yield b"x"

    async def exercise() -> None:
        async with _scope(database) as scope:
            subject = await scope.status(scope.path)
            session = await scope.new_session()
            before = await scope.submit(session, "before", _FACT)
            runner = scope.runner(_Model())
            assert await runner.run_once()
            (old_message,) = (await scope.status(before.headers["Location"])).json()[
                "message_ids"
            ]
            path = scope.path + "/purge"
            headers = {**scope.headers, "Idempotency-Key": "stable-purge"}
            denied = await scope.client.post(
                path,
                headers={**scope.foreign_headers, "Idempotency-Key": "stable-purge"},
            )
            assert (denied.status_code, denied.json()["code"]) == (
                404,
                "space_not_found",
            )
            assert (
                await scope.client.post(path, headers=scope.headers)
            ).status_code == 422
            unexpected = await scope.client.post(path, headers=headers, content=b"{}")
            assert (unexpected.status_code, unexpected.json()["code"]) == (
                400,
                "unexpected_body",
            )
            rejected = await scope.client.post(
                path, headers=headers, content=oversized_body()
            )
            assert rejected.status_code == 413
            assert (await scope.status(session)).status_code == 200
            storage = PostgresStorage(scope.connections, owner_id=scope.owner)
            async with storage.session(scope.space_id) as snapshot:
                before_generation = int(await snapshot.get_erasure_token())
            if commit_fault != "none":
                with monkeypatch.context() as changes:
                    changes.setattr(AsyncTransaction, "__aexit__", drop_ack)
                    result = await scope.client.post(path, headers=headers)
                assert result.status_code == 503, result.text
                assert result.json()["code"] == "commit_outcome_unknown"
                if commit_fault == "before_commit":
                    assert (await scope.status(session)).status_code == 200
                    retried = await scope.client.post(path, headers=headers)
                    assert retried.status_code == 204
            else:
                for result in await asyncio.gather(
                    scope.client.post(path, headers=headers),
                    scope.client.post(path, headers=headers),
                ):
                    assert result.status_code == 204 and result.content == b""

            assert (await scope.status(scope.path)).json() == subject.json()
            for erased in (
                session,
                before.headers["Location"],
                scope.path + "/messages/" + old_message,
            ):
                assert (await scope.status(erased)).status_code == 404
            assert (await scope.status(scope.path + "/facts")).json()["items"] == []
            async with storage.session(scope.space_id) as snapshot:
                assert int(await snapshot.get_erasure_token()) == before_generation + 1
            new_session = await scope.new_session()
            after = await scope.submit(new_session, "after", _FACT)
            assert await runner.run_once()
            operation = after.headers["Location"]
            published = (await scope.status(operation)).json()
            assert published["status"] == "succeeded"
            replay = await scope.client.post(path, headers=headers)
            assert replay.status_code == 204 and replay.content == b""
            assert (await scope.status(operation)).json() == published
            (new_message,) = published["message_ids"]
            assert (
                await scope.status(scope.path + "/messages/" + new_message)
            ).status_code == 200
            assert (await scope.status(scope.path + "/facts")).json()["items"]
            fresh = await scope.client.post(
                path, headers={**scope.headers, "Idempotency-Key": "next-purge"}
            )
            assert fresh.status_code == 204
            assert (await scope.status(operation)).status_code == 404

    run(exercise())


@pytest.mark.parametrize(
    ("stage", "function"),
    [
        ("extraction", "extract_facts"),
        ("embedding", "prepare_facts"),
        ("adjudication", "compare_facts"),
        ("publish", "complete"),
    ],
)
def test_terminal_error_identifies_stage_and_publishes_nothing(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    function: str,
) -> None:
    """逐阶段注入终止错误，并验证连同待处理原文的副本也被清理。"""
    if stage == "publish":
        monkeypatch.setattr(
            PostgresOperations,
            function,
            AsyncMock(side_effect=RuntimeError("合成发布故障")),
        )
        code = "internal_error"
    else:
        monkeypatch.setattr(
            remember_app,
            function,
            AsyncMock(side_effect=InvalidStructuredOutput("合成模型故障")),
        )
        code = "invalid_model_output"

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            operation = await scope.submit(session, "stage", _FACT)
            assert await scope.runner(_Model()).run_once()
            result = (await scope.status(operation.headers["Location"])).json()
            assert result["status"] == "failed"
            assert result["error"] == {"code": code, "stage": stage}
            assert "message_ids" not in result
            assert (await scope.status(scope.path + "/facts")).json()["items"] == []
            storage = PostgresStorage(scope.connections, owner_id=scope.owner)
            async with storage.session(scope.space_id) as reader:
                assert not await reader.last_messages(
                    SessionId(result["session_id"]), 10
                )
            async with scope.connections.connection() as conn:
                cursor = await conn.execute(
                    "SELECT messages FROM mnemotrace_next.remember_operations "
                    "WHERE operation_id = %s",
                    (result["operation_id"],),
                )
                assert await cursor.fetchone() == (None,)

    run(exercise())
