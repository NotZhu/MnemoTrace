"""验证 HTTP 骨架对外公开的契约。"""

import asyncio
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient, RemoteProtocolError, Response
from uvicorn import Config, Server

from mnemotrace.adapters.postgres.migrate import check_migration_readiness
from mnemotrace.application.operations import (
    IdempotencyConflict,
    Operation,
    OperationNotFound,
    QueueFull,
    WriteModelsUnavailable,
)
from mnemotrace.application.profile_refresh import ProfileRefreshState
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Role, Source
from mnemotrace.domain.fact import Fact
from mnemotrace.domain.ids import FactId, MessageId, SessionId, SpaceId
from mnemotrace.domain.message import Message
from mnemotrace.domain.profile import Profile, ProfileEntry
from mnemotrace.domain.space import Space
from mnemotrace.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace.runtime_settings import RuntimeSettings
from mnemotrace.server.__main__ import main as server_main
from mnemotrace.server.app import create_app
from mnemotrace.server.auth import authenticated_owner, authorized_storage
from mnemotrace.server.dependencies import (
    OperationAccess,
    operation_access,
    profile_reader,
    profile_refresh_submitter,
)
from mnemotrace.server.health import readiness_check
from mnemotrace.server.settings import Settings
from tests.ports.fakes import FakeClock, FakeEmbedder, FakeStorage


@pytest.fixture
def anyio_backend() -> str:
    """使用 Python 后端支持的 asyncio 运行 ASGI 测试。"""
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(RuntimeSettings("dbname=unused"), max_body_bytes=64))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def openapi_response(client: AsyncClient) -> Response:
    """读取服务发布的公开契约。"""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    return response


def _assert_problem(response: Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == status
    assert response.json()["code"] == code
    assert response.json()["request_id"] == response.headers["x-request-id"]


@pytest.mark.anyio
async def test_session_summary_patch_accepts_only_strict_booleans() -> None:
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    storage = AsyncMock()
    storage.update_space.return_value = Space(
        SpaceId("a" * 32), "owner", "已改名", 1.0, session_summary_enabled=True
    )
    app.dependency_overrides[authorized_storage] = lambda: storage
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for invalid in (None, "true", 1):
            response = await client.patch(
                f"/v1/spaces/{'a' * 32}", json={"session_summary_enabled": invalid}
            )
            _assert_problem(response, 422, "invalid_request")
            assert response.json()["errors"][0]["path"] == "/session_summary_enabled"
        storage.update_space.assert_not_awaited()
        response = await client.patch(
            f"/v1/spaces/{'a' * 32}",
            json={"name": "已改名", "session_summary_enabled": True},
        )
        assert response.status_code == 200
        assert response.json()["session_summary_enabled"] is True
        storage.update_space.assert_awaited_once_with(
            SpaceId("a" * 32),
            name="已改名",
            memory_decay=None,
            profile_auto_refresh=None,
            session_summary_enabled=True,
        )


@pytest.mark.parametrize(
    "dsn_template",
    ("postgresql://user:{}@[::1", "postgresql://user:{}%FF@localhost/db"),
)
def test_malformed_postgres_dsn_is_rejected_without_echoing_credentials(
    dsn_template: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """配置对象和命令行都在建池前拒绝错误的连接串。"""
    secret = "synthetic-dsn-secret"
    dsn = dsn_template.format(secret)
    error = "MNEMOTRACE_POSTGRES_DSN 格式无效"
    with pytest.raises(ValueError, match=f"^{error}$") as settings_error:
        RuntimeSettings(dsn)
    assert settings_error.value.__suppress_context__

    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", dsn)
    monkeypatch.setattr(sys, "argv", ["mnemotrace.server"])
    with pytest.raises(SystemExit) as exit_info:
        server_main()
    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert error in output.err
    assert secret not in output.out + output.err


@pytest.mark.anyio
async def test_health_and_bearer_auth(client: AsyncClient) -> None:
    """健康探针公开，空间路由执行 Bearer 认证。"""
    assert (await client.get("/livez")).json() == {"status": "ok"}
    _assert_problem(await client.get("/readyz"), 503, "database_unavailable")
    for headers in (
        {},
        {"Authorization": "Basic sample"},
        {"Authorization": "Bearer invalid"},
    ):
        response = await client.get("/v1/spaces", headers=headers)
        _assert_problem(response, 401, "invalid_api_key")
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    refresh = await client.post(f"/v1/spaces/{'a' * 32}/profile/refresh")
    _assert_problem(refresh, 401, "invalid_api_key")
    assert refresh.headers["www-authenticate"] == "Bearer"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "code"),
    (
        (TimeoutError, "database_unavailable"),
        (psycopg.OperationalError, "database_unavailable"),
        (psycopg.errors.UndefinedTable, "database_not_ready"),
    ),
)
async def test_readiness_maps_adapter_failures(
    failure: type[Exception], code: str
) -> None:
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    pool = MagicMock()
    pool.connection.return_value.__aenter__.side_effect = failure
    check = partial(check_migration_readiness, pool, {}, timeout_seconds=3)
    app.dependency_overrides[readiness_check] = lambda: check
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        _assert_problem(await client.get("/readyz"), 503, code)


@pytest.mark.anyio
async def test_openapi_routes_and_operation_ids(
    client: AsyncClient, openapi_response: Response
) -> None:
    """公开契约发布稳定的 /v1 路由、操作 ID 和认证声明。"""
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        _assert_problem(await client.get(path), 404, "not_found")
    spec = openapi_response.json()
    assert spec["openapi"].startswith("3.1.")
    assert spec["components"]["securitySchemes"]["ApiKey"]["scheme"] == "bearer"
    for path, methods in spec["paths"].items():
        for operation in methods.values():
            for status in ("400", "413", "500"):
                assert operation["responses"][status]["content"].keys() == {
                    "application/problem+json"
                }
            if path.startswith("/v1/"):
                assert operation["security"] == [{"ApiKey": []}]
                assert operation["responses"]["401"]["content"].keys() == {
                    "application/problem+json"
                }
            else:
                assert path in {"/livez", "/readyz"}
                assert "security" not in operation
    assert {
        path: {method: op["operationId"] for method, op in methods.items()}
        for path, methods in spec["paths"].items()
        if path.startswith("/v1/")
    } == {
        "/v1/spaces": {"get": "listSpaces", "post": "createSpace"},
        "/v1/spaces/{space_id}": {
            "get": "getSpace",
            "patch": "updateSpace",
            "delete": "deleteSpace",
        },
        "/v1/spaces/{space_id}/purge": {"post": "purgeSpace"},
        "/v1/spaces/{space_id}/profile": {"get": "getProfile"},
        "/v1/spaces/{space_id}/profile/refresh": {"post": "refreshProfile"},
        "/v1/spaces/{space_id}/sessions": {
            "get": "listSessions",
            "post": "createSession",
        },
        "/v1/spaces/{space_id}/sessions/{session_id}": {
            "get": "getSession",
            "delete": "deleteSession",
        },
        "/v1/spaces/{space_id}/sessions/{session_id}/remember": {"post": "remember"},
        "/v1/spaces/{space_id}/sessions/{session_id}/messages": {
            "get": "listSessionMessages"
        },
        "/v1/spaces/{space_id}/operations": {"get": "listOperations"},
        "/v1/spaces/{space_id}/operations/{operation_id}": {"get": "getOperation"},
        "/v1/spaces/{space_id}/operations/{operation_id}/fact-changes": {
            "get": "listOperationFactChanges"
        },
        "/v1/spaces/{space_id}/recall": {"post": "recall"},
        "/v1/spaces/{space_id}/facts": {"get": "listFacts"},
        "/v1/spaces/{space_id}/facts/{fact_id}": {
            "get": "getFact",
            "delete": "deleteFact",
        },
        "/v1/spaces/{space_id}/facts/{fact_id}/changes": {"get": "listFactChanges"},
        "/v1/spaces/{space_id}/messages/{message_id}": {"get": "getMessage"},
    }


@pytest.mark.anyio
async def test_openapi_request_and_response_schemas(openapi_response: Response) -> None:
    """公开契约描述分页、写入与擦除参数，以及响应结构。"""
    spec = openapi_response.json()
    schemas = spec["components"]["schemas"]
    resource_pattern = "^[0-9a-f]{32}$"
    for path in spec["paths"].values():
        for operation in path.values():
            for parameter in operation.get("parameters", ()):
                if parameter["in"] == "path":
                    assert parameter["schema"]["pattern"] == resource_pattern
    operation_list = spec["paths"]["/v1/spaces/{space_id}/operations"]["get"]
    session_filter = next(
        item for item in operation_list["parameters"] if item["name"] == "session_id"
    )
    assert any(
        item.get("pattern") == resource_pattern
        for item in session_filter["schema"]["anyOf"]
    )
    assert set(schemas["ProfileData"]["required"]) == {
        "basic",
        "communication_preferences",
        "preferences",
        "constraints",
    }
    assert set(schemas["ProfileResponse"]["properties"]) == {
        "profile",
        "updated_at",
        "refresh_status",
        "next_retry_at",
        "refresh_error",
    }
    profile_refresh = spec["paths"]["/v1/spaces/{space_id}/profile/refresh"]["post"]
    assert "requestBody" not in profile_refresh
    assert "Location" in profile_refresh["responses"]["202"]["headers"]
    for schema, field in (
        ("FactResponse", "recorded_at"),
        ("SpaceResponse", "created_at"),
        ("MessageResponse", "recorded_at"),
        ("FactChangeResponse", "changed_at"),
        ("CutoffTriggerResponse", "valid_end"),
    ):
        properties = schemas[schema]["properties"]
        assert properties[field]["type"] == "string"
        assert properties[field]["format"] == "date-time"
        assert field in schemas[schema]["required"]
    for schema, field in (
        ("EvidenceResponse", "valid_at"),
        ("MessageResponse", "event_time"),
    ):
        assert {
            item["type"] for item in schemas[schema]["properties"][field]["anyOf"]
        } == {"string", "null"}
    assert set(schemas["OperationFactChangePage"]["properties"]) == {
        "status",
        "items",
        "next_cursor",
    }
    for schema in ("OperationSummary", "OperationResponse"):
        attempts = schemas[schema]["properties"]["attempts"]
        assert attempts["type"] == "integer"
        assert attempts["minimum"] == 0
        assert "attempts" in schemas[schema]["required"]
    for path in (
        "/v1/spaces",
        "/v1/spaces/{space_id}/sessions",
        "/v1/spaces/{space_id}/facts",
        "/v1/spaces/{space_id}/operations",
        "/v1/spaces/{space_id}/operations/{operation_id}/fact-changes",
        "/v1/spaces/{space_id}/facts/{fact_id}/changes",
        "/v1/spaces/{space_id}/sessions/{session_id}/messages",
    ):
        operation = spec["paths"][path]["get"]
        parameters = {p["name"] for p in operation["parameters"] if p["in"] == "query"}
        assert {"cursor", "limit"} <= parameters
        assert "after_id" not in parameters
    recall = spec["paths"]["/v1/spaces/{space_id}/recall"]["post"]
    recall_request = schemas["RecallRequest"]
    assert "prefer_salient" not in recall_request["properties"]
    assert {
        item["type"] for item in recall_request["properties"]["rerank"]["anyOf"]
    } == {"boolean", "null"}
    assert "rerank" not in recall_request["required"]
    assert "rerank_applied" in schemas["RecallResponse"]["required"]
    assert "truncated" in schemas["RecallResponse"]["required"]
    budget = recall_request["properties"]["max_evidence_chars"]["anyOf"]
    assert any(item == {"type": "integer", "minimum": 1} for item in budget)
    assert "max_evidence_chars" not in recall_request["required"]
    labels = schemas["RecallFilterRequest"]["properties"]["labels"]["items"]
    assert "technology" in labels["enum"]
    fact_list = spec["paths"]["/v1/spaces/{space_id}/facts"]["get"]
    assert {"labels", "source"} <= {
        item["name"] for item in fact_list["parameters"] if item["in"] == "query"
    }
    for option, default in (("prefer_recent", False), ("track_usage", True)):
        schema = recall_request["properties"][option]
        assert schema["type"] == "boolean" and schema["default"] is default
        assert option not in recall_request["required"]
    assert (
        spec["paths"]["/v1/spaces/{space_id}"]["patch"]["operationId"] == "updateSpace"
    )
    assert schemas["SpacePatch"]["properties"]["memory_decay"]["type"] == "boolean"
    assert (
        schemas["SpacePatch"]["properties"]["profile_auto_refresh"]["type"] == "boolean"
    )
    assert "profile_auto_refresh" in schemas["SpaceResponse"]["required"]
    assert (
        schemas["SpacePatch"]["properties"]["session_summary_enabled"]["type"]
        == "boolean"
    )
    assert "session_summary_enabled" in schemas["SpaceResponse"]["required"]
    assert "session_summary_enabled" not in schemas["SpaceName"]["properties"]
    validation = recall["responses"]["422"]["content"]["application/problem+json"]
    assert set(validation["schema"]["properties"]["errors"]["items"]["properties"]) == {
        "location",
        "path",
        "code",
    }
    remember = spec["paths"]["/v1/spaces/{space_id}/sessions/{session_id}/remember"][
        "post"
    ]
    assert any(
        item["name"] == "Idempotency-Key"
        and item["in"] == "header"
        and item["required"]
        for item in remember["parameters"]
    )
    accepted = remember["responses"]["202"]
    assert "Location" in accepted["headers"]
    assert "application/json" in accepted["content"]
    purge = spec["paths"]["/v1/spaces/{space_id}/purge"]["post"]
    assert any(
        item["name"] == "Idempotency-Key"
        and item["in"] == "header"
        and item["required"]
        for item in purge["parameters"]
    )
    for operation in (
        purge,
        spec["paths"]["/v1/spaces/{space_id}"]["delete"],
        spec["paths"]["/v1/spaces/{space_id}/sessions/{session_id}"]["delete"],
        spec["paths"]["/v1/spaces/{space_id}/facts/{fact_id}"]["delete"],
    ):
        assert "requestBody" not in operation
        assert "content" not in operation["responses"]["204"]
    for path in ("/v1/spaces", "/v1/spaces/{space_id}/sessions"):
        responses = spec["paths"][path]["post"]["responses"]
        created = responses["201"]
        assert "Location" in created["headers"]
        assert "application/json" in created["content"]
        assert "application/problem+json" in responses["413"]["content"]


@pytest.mark.anyio
async def test_request_errors_do_not_echo_input(client: AsyncClient) -> None:
    """拒绝非法 JSON、媒体类型和过大的声明长度。"""
    response = await client.post(
        "/v1/spaces",
        headers={"Content-Type": "application/json"},
        content=b"{synthetic",
    )
    _assert_problem(response, 400, "invalid_json")
    assert "errors" not in response.json()
    assert "synthetic" not in response.text

    response = await client.post("/v1/spaces", content=b'{"name":"synthetic"}')
    _assert_problem(response, 415, "unsupported_media_type")
    _assert_problem(
        await client.post("/v1/spaces", content=b" " * 65), 413, "request_too_large"
    )
    _assert_problem(
        await client.get("/v1/spaces", headers={"Content-Length": "-1"}),
        400,
        "invalid_content_length",
    )
    _assert_problem(
        await client.get("/v1/spaces", headers={"Content-Length": "65"}),
        413,
        "request_too_large",
    )

    async def oversize_body() -> AsyncIterator[bytes]:
        yield b" " * 33
        yield b" " * 32

    _assert_problem(
        await client.post(
            "/v1/spaces",
            headers={"Content-Type": "application/json"},
            content=oversize_body(),
        ),
        413,
        "request_too_large",
    )


@pytest.mark.anyio
async def test_unexpected_errors_keep_exception_messages_out_of_uvicorn_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    store = AsyncMock()
    store.create_space.side_effect = RuntimeError("synthetic internal detail")
    app.dependency_overrides[authenticated_owner] = lambda: "synthetic-owner"
    app.dependency_overrides[authorized_storage] = lambda: store

    @app.get("/v1/synthetic-stream")
    async def stream() -> StreamingResponse:
        async def broken_body() -> AsyncIterator[bytes]:
            yield b"synthetic prefix"
            raise RuntimeError("synthetic stream detail")

        return StreamingResponse(broken_body())

    server = Server(
        Config(
            app,
            http="h11",
            lifespan="off",
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=2,
        )
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                response = await client.post("/v1/spaces", json={"name": "合成空间"})
                async with client.stream("GET", "/v1/synthetic-stream") as started:
                    assert started.status_code == 200
                    assert started.headers["x-request-id"]
                    with pytest.raises(RemoteProtocolError):
                        await started.aread()
        finally:
            server.should_exit = True
            await serving

    _assert_problem(response, 500, "internal_error")
    assert response.json()["detail"] == "服务处理请求时发生错误。"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-request-id"] in caplog.text
    assert started.headers["x-request-id"] in caplog.text
    assert "RuntimeError" in caplog.text
    assert any(
        record.name == "uvicorn.error" and record.exc_info is not None
        for record in caplog.records
    )
    assert "synthetic internal detail" not in caplog.text
    assert "synthetic stream detail" not in caplog.text


@pytest.mark.anyio
async def test_resource_locations_and_injected_dependencies() -> None:
    now = datetime(2020, 1, 1, tzinfo=UTC)
    clock = FakeClock(at=now.timestamp())
    app = create_app(Settings(RuntimeSettings("dbname=unused")), clock=clock)
    store = FakeStorage()
    operations = AsyncMock(spec=OperationAccess)
    read_profile = AsyncMock()
    submit_refresh = AsyncMock(return_value="queued")
    owner = "synthetic-owner"
    app.dependency_overrides[authenticated_owner] = lambda: owner
    app.dependency_overrides[authorized_storage] = lambda: store
    app.dependency_overrides[operation_access] = lambda: operations
    app.dependency_overrides[profile_reader] = lambda: read_profile
    app.dependency_overrides[profile_refresh_submitter] = lambda: submit_refresh

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post("/v1/spaces", json={"name": "合成空间"})
        assert created.status_code == 201
        assert created.json()["created_at"] == "2020-01-01T00:00:00Z"
        space_id = SpaceId(created.json()["space_id"])
        space_path = created.headers["location"]
        assert space_path == f"/v1/spaces/{space_id.value}"
        assert (await client.get(space_path)).json() == created.json()

        session = await client.post(f"{space_path}/sessions")
        assert session.status_code == 201
        session_id = SessionId(session.json()["session_id"])
        session_path = session.headers["location"]
        assert session_path == f"{space_path}/sessions/{session_id.value}"
        assert (await client.get(session_path)).json() == session.json()

        operation = Operation(
            operation_id="e" * 32,
            session_id=session_id,
            status="queued",
            attempts=0,
            created_at=now,
            updated_at=now,
            next_retry_at=None,
            message_ids=None,
            fact_change_count=None,
            error_code=None,
            error_stage=None,
        )
        operations.submit.return_value = operation
        operations.get.return_value = operation
        operations.list.return_value = ((1, operation),)
        operations.list_fact_changes.return_value = (operation, ())
        accepted = await client.post(
            f"{session_path}/remember",
            headers={"Idempotency-Key": "synthetic-key"},
            json={"messages": [{"role": "user", "content": "合成原文"}]},
        )
        assert accepted.status_code == 202
        operation_path = accepted.headers["location"]
        assert operation_path == f"{space_path}/operations/{operation.operation_id}"
        assert (await client.get(operation_path)).json() == accepted.json()
        listed = await client.get(f"{space_path}/operations")
        assert listed.json()["items"][0]["operation_id"] == operation.operation_id
        changes = await client.get(f"{operation_path}/fact-changes")
        assert changes.json() == {"items": [], "next_cursor": None, "status": "queued"}

        evidence_id = FactId("d" * 32)
        profile = Profile(
            space_id,
            {
                "basic": (ProfileEntry("合成画像", (evidence_id,)),),
                "communication_preferences": (),
                "preferences": (),
                "constraints": (),
            },
            now.timestamp(),
        )
        read_profile.return_value = (
            profile,
            ProfileRefreshState("queued", now + timedelta(seconds=30)),
        )
        refresh = await client.post(f"{space_path}/profile/refresh")
        assert refresh.status_code == 202
        assert refresh.json() == {"refresh_status": "queued"}
        assert refresh.headers["location"] == f"{space_path}/profile"
        view = await client.get(refresh.headers["location"])
        assert view.status_code == 200
        assert view.json()["profile"]["basic"] == [
            {"text": "合成画像", "fact_ids": [evidence_id.value]}
        ]
        assert view.json()["next_retry_at"] == "2020-01-01T00:00:30Z"

    operations.get.assert_awaited_once_with(owner, space_id, operation.operation_id)
    read_profile.assert_awaited_once_with(owner, space_id, clock=clock)
    submit_refresh.assert_awaited_once_with(owner, space_id, configured=False)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "status", "code"),
    (
        (IdempotencyConflict, 409, "idempotency_conflict"),
        (QueueFull, 503, "remember_queue_full"),
        (WriteModelsUnavailable, 503, "write_models_unavailable"),
    ),
)
async def test_remember_maps_submission_failures(
    failure: type[Exception], status: int, code: str
) -> None:
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    operations = AsyncMock(spec=OperationAccess)
    operations.submit.side_effect = failure
    operations.get.side_effect = OperationNotFound
    app.dependency_overrides[authenticated_owner] = lambda: "synthetic-owner"
    app.dependency_overrides[operation_access] = lambda: operations
    space_path = f"/v1/spaces/{'a' * 32}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"{space_path}/sessions/{'b' * 32}/remember",
            headers={"Idempotency-Key": "synthetic-key"},
            json={"messages": [{"role": "user", "content": "合成原文"}]},
        )
        _assert_problem(response, status, code)
        _assert_problem(
            await client.get(f"{space_path}/operations/{'c' * 32}"),
            404,
            "operation_not_found",
        )


@pytest.mark.anyio
async def test_validation_reports_safe_field_locations() -> None:
    """嵌套、列表元素和查询参数错误可定位，额外字段名和请求值不回显。"""
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    app.dependency_overrides[authenticated_owner] = lambda: "synthetic-owner"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/spaces/{'a' * 32}/recall",
            json={
                "query": "synthetic-query",
                "top_k": 101,
                "prefer_recent": "synthetic-boolean",
                "track_usage": "synthetic-boolean",
                "graph": {
                    "max_hops": 5,
                    "origin_entity_ids": [" ", "valid"],
                    "synthetic-extra": "synthetic-value",
                },
            },
        )
        _assert_problem(response, 422, "invalid_request")
        assert "synthetic-" not in response.text
        assert {
            (error["location"], error["path"], error["code"])
            for error in response.json()["errors"]
        } == {
            ("body", "/top_k", "out_of_range"),
            ("body", "/prefer_recent", "invalid_value"),
            ("body", "/track_usage", "invalid_value"),
            ("body", "/graph/max_hops", "out_of_range"),
            ("body", "/graph/origin_entity_ids/0", "invalid_value"),
            ("body", "/graph", "unknown_field"),
        }
        response = await client.get("/v1/spaces", params={"limit": 0})
        _assert_problem(response, 422, "invalid_request")
        assert response.json()["errors"] == [
            {"location": "query", "path": "/limit", "code": "out_of_range"}
        ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "path", "body", "pointer"),
    (
        ("POST", "/v1/spaces", rb'{"name":"\ud800"}', "/name"),
        ("PATCH", f"/v1/spaces/{'a' * 32}", rb'{"name":"\ud800"}', "/name"),
        ("POST", f"/v1/spaces/{'a' * 32}/recall", rb'{"query":"\ud800"}', "/query"),
        (
            "POST",
            f"/v1/spaces/{'a' * 32}/recall",
            rb'{"query":"valid","graph":{"predicates":["\ud800"]}}',
            "/graph/predicates/0",
        ),
        (
            "POST",
            f"/v1/spaces/{'a' * 32}/sessions/{'b' * 32}/remember",
            rb'{"messages":[{"role":"user","content":"\ud800"}]}',
            "/messages/0/content",
        ),
    ),
)
async def test_http_text_rejects_unpaired_surrogates(
    method: str, path: str, body: bytes, pointer: str
) -> None:
    """JSON 解码后无法表示为 UTF-8 的文本在 HTTP 装配层统一失败。"""
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    app.dependency_overrides[authenticated_owner] = lambda: "synthetic-owner"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            path,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "synthetic",
            },
        )
    _assert_problem(response, 422, "invalid_request")
    assert response.json()["errors"] == [
        {"location": "body", "path": pointer, "code": "invalid_value"}
    ]


@pytest.mark.anyio
async def test_pre_epoch_times_round_trip_over_http() -> None:
    """读取资源和续页时保留事实与原文的早期日期。"""
    start = datetime(1965, 1, 1, tzinfo=UTC)
    end = datetime(1965, 1, 2, tzinfo=UTC)
    space = Space(SpaceId("a" * 32), "synthetic-owner", "旧空间", start.timestamp())
    session_id = SessionId("b" * 32)
    source = Message(
        MessageId("c" * 32),
        space.id,
        session_id,
        Role.USER,
        "档案中记录了旧格式",
        start.timestamp(),
        start.timestamp(),
    )
    subject = Entity(space.id, "旧档案")
    fact = Fact(
        id=FactId("d" * 32),
        space_id=space.id,
        subject=EntityRef(subject.id),
        predicate=Predicate("使用"),
        object=LiteralValue("旧格式"),
        text="旧档案使用旧格式",
        valid_at=start.timestamp(),
        invalid_at=end.timestamp(),
        recorded_at=end.timestamp(),
        expired_at=end.timestamp(),
        provenance=(source.id,),
        labels=(),
        sensitive=False,
        source=Source.EXTRACTED,
    )
    other = replace(fact, id=FactId("e" * 32), recorded_at=start.timestamp())
    storage = FakeStorage()
    await storage.create_space(space)
    async with storage.session(space.id, write=True) as writer:
        await writer.register_session(session_id)
        await writer.append_messages((source,))
        await writer.upsert_entity(subject)
        await writer.save_facts((fact, other), changed_at=end.timestamp())

    clock = FakeClock(at=end.timestamp())
    app = create_app(Settings(RuntimeSettings("dbname=unused")), clock=clock)
    app.dependency_overrides[authorized_storage] = lambda: storage
    app.state.embedder = FakeEmbedder()
    path = f"/v1/spaces/{space.id.value}"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["created_at"] == "1965-01-01T00:00:00Z"
        sessions = (await client.get(f"{path}/sessions")).json()["items"]
        session = await client.get(f"{path}/sessions/{sessions[0]['session_id']}")
        assert session.json()["session_id"] == session_id.value

        filters: dict[str, str | int] = {"state": "all", "limit": 1}
        first = await client.get(f"{path}/facts", params=filters)
        assert first.status_code == 200
        assert first.json()["evaluated_at"] == "1965-01-02T00:00:00Z"
        item = first.json()["items"][0]
        assert item["fact_id"] == fact.id.value
        assert item["valid_at"] == "1965-01-01T00:00:00Z"
        assert (
            item["invalid_at"]
            == item["recorded_at"]
            == item["expired_at"]
            == ("1965-01-02T00:00:00Z")
        )
        cursor = first.json()["next_cursor"]
        assert cursor
        clock.at += 86_400
        second = await client.get(f"{path}/facts", params={**filters, "cursor": cursor})
        assert second.json()["evaluated_at"] == first.json()["evaluated_at"]
        assert second.json()["items"][0]["fact_id"] == other.id.value
        as_of = "1965-01-01T12:00:00Z"
        as_of_page = await client.get(f"{path}/facts", params={"as_of": as_of})
        assert as_of_page.status_code == 200
        assert as_of_page.json()["evaluated_at"] == as_of
        detail = await client.get(f"{path}/facts/{item['fact_id']}")
        assert detail.json() == item
        original = await client.get(f"{path}/messages/{item['source_message_ids'][0]}")
        assert original.json()["message_id"] == source.id.value
        assert original.json()["recorded_at"] == "1965-01-01T00:00:00Z"

        recalled = await client.post(
            f"{path}/recall",
            json={"query": "旧档案", "as_of": as_of},
        )
        assert recalled.status_code == 200
        evidence = recalled.json()["evidence"]
        assert {entry["fact_id"] for entry in evidence} == {
            fact.id.value,
            other.id.value,
        }
        assert all(entry["valid_at"] == item["valid_at"] for entry in evidence)
