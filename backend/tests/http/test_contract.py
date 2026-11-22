"""验证 HTTP 骨架对外公开的契约。"""

import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient, Response

from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Role, Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, MessageId, SessionId, SpaceId
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace_next.runtime_settings import RuntimeSettings
from mnemotrace_next.server.__main__ import main as server_main
from mnemotrace_next.server.app import create_app
from mnemotrace_next.server.auth import authenticated_owner, authorized_storage
from mnemotrace_next.server.settings import Settings
from tests.ports.fakes import FakeEmbedder, FakeStorage


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
    error = "MNEMOTRACE_NEXT_POSTGRES_DSN 格式无效"
    with pytest.raises(ValueError, match=f"^{error}$") as settings_error:
        RuntimeSettings(dsn)
    assert settings_error.value.__suppress_context__

    monkeypatch.setenv("MNEMOTRACE_NEXT_POSTGRES_DSN", dsn)
    monkeypatch.setattr(sys, "argv", ["mnemotrace_next.server"])
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
    for methods in spec["paths"].values():
        for operation in methods.values():
            if "security" in operation:
                assert operation["security"] == [{"ApiKey": []}]
                assert operation["responses"]["401"]["content"].keys() == {
                    "application/problem+json"
                }
    assert {
        path: {method: op["operationId"] for method, op in methods.items()}
        for path, methods in spec["paths"].items()
        if path.startswith("/v1/")
    } == {
        "/v1/spaces": {"get": "listSpaces", "post": "createSpace"},
        "/v1/spaces/{space_id}": {
            "get": "getSpace",
            "patch": "renameSpace",
            "delete": "deleteSpace",
        },
        "/v1/spaces/{space_id}/purge": {"post": "purgeSpace"},
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
    for schema, field in (
        ("FactResponse", "recorded_at"),
        ("SpaceResponse", "created_at"),
        ("MessageResponse", "recorded_at"),
    ):
        properties = schemas[schema]["properties"]
        assert {item["type"] for item in properties[field]["anyOf"]} == {
            "string",
            "null",
        }
        assert "out_of_range_fields" in properties
    assert "out_of_range_fields" in schemas["EvidenceResponse"]["properties"]
    assert set(schemas["OperationFactChangePage"]["properties"]) == {
        "status",
        "items",
        "next_cursor",
    }
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
        spec["paths"]["/v1/spaces/{space_id}"]["patch"]["operationId"] == "renameSpace"
    )
    assert schemas["SpacePatch"]["properties"]["memory_decay"]["type"] == "boolean"
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


@pytest.mark.anyio
async def test_validation_reports_safe_field_locations() -> None:
    """嵌套、列表元素和查询参数错误可定位，额外字段名和请求值不回显。"""
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    app.dependency_overrides[authenticated_owner] = lambda: "synthetic-owner"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/spaces/space/recall",
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
        ("PATCH", "/v1/spaces/space", rb'{"name":"\ud800"}', "/name"),
        ("POST", "/v1/spaces/space/recall", rb'{"query":"\ud800"}', "/query"),
        (
            "POST",
            "/v1/spaces/space/recall",
            rb'{"query":"valid","graph":{"predicates":["\ud800"]}}',
            "/graph/predicates/0",
        ),
        (
            "POST",
            "/v1/spaces/space/sessions/session/remember",
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
async def test_legacy_ids_and_pre_epoch_times_round_trip_over_http() -> None:
    """用存量长标识读取资源及续页，并保留事实与原文的早期日期。"""
    start = datetime(1965, 1, 1, tzinfo=UTC)
    end = datetime(1965, 1, 2, tzinfo=UTC)
    space = Space(SpaceId("s" * 257), "synthetic-owner", "旧空间", start.timestamp())
    session_id = SessionId("c" * 257)
    source = Message(
        MessageId("m" * 257),
        space.id,
        session_id,
        Role.USER,
        "档案中记录了旧格式",
        start.timestamp(),
        start.timestamp(),
    )
    subject = Entity(space.id, "旧档案")
    fact = Fact(
        id=FactId("old-format"),
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
    other = replace(fact, id=FactId("other"), recorded_at=start.timestamp())
    storage = FakeStorage()
    await storage.create_space(space)
    async with storage.session(space.id, write=True) as writer:
        await writer.register_session(session_id)
        await writer.append_messages((source,))
        await writer.upsert_entity(subject)
        await writer.save_facts((fact, other), changed_at=end.timestamp())

    app = create_app(Settings(RuntimeSettings("dbname=unused")))
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
        second = await client.get(f"{path}/facts", params={**filters, "cursor": cursor})
        assert second.json()["items"][0]["fact_id"] == other.id.value
        detail = await client.get(f"{path}/facts/{item['fact_id']}")
        assert detail.json() == item
        original = await client.get(f"{path}/messages/{item['source_message_ids'][0]}")
        assert original.json()["message_id"] == source.id.value
        assert original.json()["recorded_at"] == "1965-01-01T00:00:00Z"

        recalled = await client.post(
            f"{path}/recall",
            json={"query": "旧档案", "as_of": "1965-01-01T12:00:00Z"},
        )
        assert recalled.status_code == 200
        evidence = recalled.json()["evidence"]
        assert {entry["fact_id"] for entry in evidence} == {"old-format", "other"}
        assert all(entry["valid_at"] == item["valid_at"] for entry in evidence)
