"""在真实 PostgreSQL 上验证 HTTP、凭据和空间隔离。"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from types import TracebackType
from typing import LiteralString
from uuid import uuid4

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncTransaction

from mnemotrace.adapters.postgres import migrate
from mnemotrace.adapters.postgres.identity import issue_key, revoke_key
from mnemotrace.admin import main as admin_main
from mnemotrace.runtime_settings import RuntimeSettings
from mnemotrace.server.app import create_app
from mnemotrace.server.settings import Settings

from .conftest import DatabaseFactory, PgDatabase
from .sample import run

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def _http(dsn: str) -> AsyncIterator[AsyncClient]:
    app = create_app(Settings(RuntimeSettings(dsn), max_body_bytes=128))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


def test_server_space_and_session_isolation(database: PgDatabase) -> None:
    """验证分页、空间归属、会话归属及撤销后的访问。"""
    owner = uuid4().hex
    with psycopg.connect(database.admin) as conn:
        first_key = issue_key(conn, owner)
        other_key = issue_key(conn, uuid4().hex)
        rotated_key = issue_key(conn, owner)

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {first_key.token}"}
        other = {"Authorization": f"Bearer {other_key.token}"}
        async with _http(database.runtime) as client:
            assert (await client.get("/readyz")).status_code == 200
            suffix = "A" if first_key.token[-1] != "A" else "B"
            tampered = {"Authorization": f"Bearer {first_key.token[:-1]}{suffix}"}
            assert (await client.get("/v1/spaces", headers=tampered)).status_code == 401
            response = await client.post(
                "/v1/spaces", headers=headers, json={"name": "  研究  "}
            )
            assert response.status_code == 201, response.text
            first = response.headers["location"]
            body = response.json()
            assert body["name"] == "研究"
            assert set(body) == {
                "space_id",
                "created_at",
                "name",
                "memory_decay",
                "profile_auto_refresh",
                "session_summary_enabled",
            }
            assert body["created_at"].endswith("Z")
            assert body["memory_decay"] is False
            assert body["profile_auto_refresh"] is False
            assert body["session_summary_enabled"] is False
            second = await client.post(
                "/v1/spaces", headers=headers, json={"name": "学习"}
            )
            assert second.status_code == 201

            response = await client.get("/v1/spaces?limit=1", headers=headers)
            cursor = response.json()["next_cursor"]
            expected = sorted((body["space_id"], second.json()["space_id"]))
            assert [item["space_id"] for item in response.json()["items"]] == expected[
                :1
            ]
            response = await client.get(
                "/v1/spaces", params={"limit": 1, "cursor": cursor}, headers=headers
            )
            assert [item["space_id"] for item in response.json()["items"]] == expected[
                1:
            ]
            assert response.json()["next_cursor"] is None
            assert (await client.get("/v1/spaces", headers=other)).json()["items"] == []

            created = await client.post(first + "/sessions", headers=headers)
            assert created.status_code == 201, created.text
            session_path = created.headers["location"]
            session_id = created.json()["session_id"]
            assert created.json()["created_at"].endswith("Z")
            assert (
                await client.get(session_path, headers=headers)
            ).json() == created.json()
            latest = await client.post(first + "/sessions", headers=headers)
            assert latest.status_code == 201
            sessions = sorted(
                (created.json(), latest.json()),
                key=lambda item: (
                    -datetime.fromisoformat(item["created_at"]).timestamp(),
                    item["session_id"],
                ),
            )
            page = await client.get(
                first + "/sessions", headers=headers, params={"limit": 1}
            )
            cursor = page.json()["next_cursor"]
            assert page.json()["items"] == sessions[:1]
            next_page = await client.get(
                first + "/sessions", headers=headers, params={"cursor": cursor}
            )
            assert next_page.json()["items"] == sessions[1:]
            assert next_page.json()["next_cursor"] is None
            mismatch = await client.get(
                second.headers["location"] + "/sessions",
                headers=headers,
                params={"cursor": cursor},
            )
            assert mismatch.status_code == 422
            assert mismatch.json()["code"] == "invalid_cursor"
            wrong_space = second.headers["location"] + f"/sessions/{session_id}"
            assert (await client.get(wrong_space, headers=headers)).json()["code"] == (
                "session_not_found"
            )

            for path in (first, first + "/sessions", session_path):
                response = await client.get(path, headers=other)
                assert response.status_code == 404
                assert response.json()["code"] == "space_not_found"
            assert (
                await client.post(first + "/sessions", headers=other)
            ).status_code == 404
            response = await client.patch(first, headers=other, json={"name": "越权"})
            assert response.status_code == 404
            response = await client.patch(
                first, headers=other, json={"session_summary_enabled": True}
            )
            assert response.json()["code"] == "space_not_found"
            for invalid in (None, "true", 1):
                response = await client.patch(
                    first, headers=headers, json={"session_summary_enabled": invalid}
                )
                assert response.status_code == 422
                assert (
                    response.json()["errors"][0]["path"] == "/session_summary_enabled"
                )
            response = await client.patch(
                first, headers=headers, json={"session_summary_enabled": True}
            )
            assert (
                response.status_code == 200
                and response.json()["session_summary_enabled"] is True
            )
            response = await client.patch(first, headers=headers, json={"name": "改名"})
            assert response.json()["name"] == "改名"
            assert response.json()["session_summary_enabled"] is True
            assert (await client.get(first, headers=headers)).json() == response.json()
            listed = {
                item["space_id"]: item["session_summary_enabled"]
                for item in (await client.get("/v1/spaces", headers=headers)).json()[
                    "items"
                ]
            }
            assert listed == {body["space_id"]: True, second.json()["space_id"]: False}

            with psycopg.connect(database.admin) as conn:
                assert revoke_key(conn, owner, first_key.info.key_id)
            assert (await client.get(first, headers=headers)).status_code == 401
            rotated = {"Authorization": f"Bearer {rotated_key.token}"}
            assert (await client.get(first, headers=rotated)).status_code == 200

    run(exercise())


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_session_pages_order_by_creation_time_and_id(
    database: PgDatabase, limit: int
) -> None:
    """不同创建时间按降序分页，同一时刻按会话标识排序。"""
    owner, space_id = uuid4().hex, uuid4().hex
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, owner)
        conn.execute(
            """
            INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
            VALUES (%s, %s, 'sessions', 1)
            """,
            (space_id, owner),
        )
        conn.execute(
            """
            INSERT INTO mnemotrace.sessions (space_id, session_id, created_at)
            VALUES (%s, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '2024-01-01T00:00:00Z'),
                (%s, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', '2024-01-01T00:00:00Z'),
                (%s, 'cccccccccccccccccccccccccccccccc', '2025-01-01T00:00:00Z'),
                (%s, 'dddddddddddddddddddddddddddddddd', '2025-01-01T00:00:00Z'),
                (%s, 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', '2025-01-01T00:00:00.000001Z')
            """,
            (space_id,) * 5,
        )

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {key.token}"}
        path = f"/v1/spaces/{space_id}/sessions"
        params: dict[str, str | int] = {"limit": limit}
        expected = [
            {"session_id": session_id, "created_at": created_at}
            for session_id, created_at in (
                ("e" * 32, "2025-01-01T00:00:00.000001Z"),
                ("c" * 32, "2025-01-01T00:00:00Z"),
                ("d" * 32, "2025-01-01T00:00:00Z"),
                ("a" * 32, "2024-01-01T00:00:00Z"),
                ("b" * 32, "2024-01-01T00:00:00Z"),
            )
        ]
        async with _http(database.runtime) as client:
            for start in range(0, len(expected), limit):
                response = await client.get(path, headers=headers, params=params)
                assert response.status_code == 200, response.text
                page = response.json()
                assert page["items"] == expected[start : start + limit]
                if start + limit >= len(expected):
                    assert page["next_cursor"] is None
                else:
                    assert page["next_cursor"]
                    params["cursor"] = page["next_cursor"]
            detail = await client.get(
                f"{path}/{expected[-2]['session_id']}", headers=headers
            )
            assert detail.json() == expected[-2]

    run(exercise())


def test_server_rejects_invalid_and_streamed_oversize_body(
    database: PgDatabase,
) -> None:
    """拒绝越界字段、无效游标和没有声明长度的过大请求。"""
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, uuid4().hex)

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"name":"'
        yield b"x" * 128
        yield b'"}'

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {key.token}"}
        async with _http(database.runtime) as client:
            for body in (
                {"name": "  "},
                {"name": "\x1c"},
                {"name": "x\x00"},
                {"name": "x", "owner_id": "y"},
            ):
                response = await client.post("/v1/spaces", headers=headers, json=body)
                assert response.status_code == 422
            response = await client.get(
                "/v1/spaces", headers=headers, params={"cursor": "\x00"}
            )
            assert response.status_code == 422
            response = await client.post(
                "/v1/spaces",
                headers={**headers, "Content-Type": "application/json"},
                content=chunks(),
            )
            assert response.status_code == 413, response.text
            assert response.json()["code"] == "request_too_large"
            assert (await client.get("/v1/spaces", headers=headers)).json()[
                "items"
            ] == []

    run(exercise())


def test_create_space_reports_lost_commit_acknowledgement(
    database: PgDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建空间已提交但确认丢失时，返回结果未知并允许核对已有空间。"""
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, uuid4().hex)
    original_exit = AsyncTransaction.__aexit__

    async def lose_acknowledgement(
        transaction: AsyncTransaction,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        result = await original_exit(transaction, exc_type, exc_value, traceback)
        if transaction.status is transaction.Status.COMMITTED:
            raise psycopg.errors.ConnectionFailure("合成提交确认丢失")
        return result

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {key.token}"}
        async with _http(database.runtime) as client:
            with monkeypatch.context() as fault:
                fault.setattr(AsyncTransaction, "__aexit__", lose_acknowledgement)
                response = await client.post(
                    "/v1/spaces", headers=headers, json={"name": "已提交空间"}
                )
            assert response.status_code == 503
            assert response.json()["code"] == "commit_outcome_unknown"
            spaces = await client.get("/v1/spaces", headers=headers)
            assert spaces.status_code == 200
            assert [space["name"] for space in spaces.json()["items"]] == ["已提交空间"]

    run(exercise())


def test_create_session_enforces_streamed_body_limit(database: PgDatabase) -> None:
    """不声明正文长度的超限请求不能创建会话。"""
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, uuid4().hex)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 80
        yield b"y" * 64

    async def exercise() -> None:
        headers = {"Authorization": f"Bearer {key.token}"}
        async with _http(database.runtime) as client:
            space = await client.post(
                "/v1/spaces", headers=headers, json={"name": "研究"}
            )
            assert space.status_code == 201
            path = space.headers["location"] + "/sessions"
            response = await client.post(path, headers=headers, content=chunks())
            assert "content-length" not in response.request.headers
            assert response.status_code == 413
            assert response.json()["code"] == "request_too_large"
            assert (await client.get(path, headers=headers)).json()["items"] == []
            allowed = await client.post(path, headers=headers, content=b"{}")
            assert allowed.status_code == 201

    run(exercise())


def test_admin_key_lifecycle(
    database: PgDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """管理命令只在签发时输出明文，撤销按账号限定。"""
    monkeypatch.setenv("MNEMOTRACE_POSTGRES_DSN", database.key_admin)
    owner_id = f" {uuid4().hex * 4} "
    space_id = uuid4().hex
    with psycopg.connect(database.admin) as conn:
        conn.execute(
            """
            INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
            VALUES (%s, %s, 'existing space', 1)
            """,
            (space_id, owner_id),
        )
    owner = ["--owner-id", owner_id]
    assert admin_main(["issue", *owner]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["token"].startswith("mtn_")
    key_id = issued["key_id"]

    async def verify_space_access() -> None:
        async with _http(database.runtime) as client:
            path = f"/v1/spaces/{space_id}"
            headers = {"Authorization": f"Bearer {issued['token']}"}
            response = await client.get(path, headers=headers)
            assert response.status_code == 200
            assert response.json()["space_id"] == space_id

            assert admin_main(["list", *owner]) == 0
            assert json.loads(capsys.readouterr().out) == [
                {
                    "key_id": key_id,
                    "created_at": issued["created_at"],
                    "revoked_at": None,
                }
            ]
            assert admin_main(["revoke", "--owner-id", "another-owner", key_id]) == 1
            capsys.readouterr()
            assert admin_main(["revoke", *owner, key_id]) == 0
            assert json.loads(capsys.readouterr().out) == {
                "key_id": key_id,
                "revoked": True,
            }
            assert admin_main(["list", *owner]) == 0
            assert json.loads(capsys.readouterr().out)[0]["revoked_at"] is not None

            response = await client.get(path, headers=headers)
            assert (response.status_code, response.json()["code"]) == (
                401,
                "invalid_api_key",
            )

    run(verify_space_access())
    with psycopg.connect(database.admin) as conn:
        row = conn.execute(
            "SELECT secret_hash FROM mnemotrace.api_keys WHERE key_id = %s",
            (key_id,),
        ).fetchone()
        assert row is not None and isinstance(row[0], bytes) and len(row[0]) == 32
    with (
        psycopg.connect(database.runtime) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("UPDATE mnemotrace.api_keys SET revoked_at = NULL")
    with (
        psycopg.connect(database.runtime) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        issue_key(conn, owner_id)
    with (
        psycopg.connect(database.key_admin) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("SELECT secret_hash FROM mnemotrace.api_keys")


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE mnemotrace.spaces ADD COLUMN restricted text",
        "UPDATE mnemotrace.schema_migrations SET checksum = 'changed'",
    ],
)
def test_runtime_cannot_modify_schema_or_migration_ledger(
    database: PgDatabase, statement: LiteralString
) -> None:
    """自管分权的运行角色不能执行迁移或改写迁移账本。"""
    with (
        psycopg.connect(database.runtime) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(statement)


@pytest.mark.parametrize(
    ("remove_prerequisite", "prerequisite_error"),
    [
        ("DROP SCHEMA mnemotrace", "请先初始化 mnemotrace schema"),
        ("DROP EXTENSION vector", "请先在 public schema 安装 vector 扩展"),
    ],
)
def test_readiness_requires_explicit_migration(
    database_factory: DatabaseFactory,
    remove_prerequisite: LiteralString,
    prerequisite_error: str,
) -> None:
    """缺少初始化或迁移时拒绝就绪，准备好数据库后才能迁移。"""
    pending = tuple(migrate.migration_checksums())

    async def exercise(dsn: str) -> None:
        async with _http(dsn) as client:
            assert (await client.get("/livez")).status_code == 200
            response = await client.get("/readyz")
            assert response.status_code == 503
            assert response.json()["code"] == "database_not_ready"
            with psycopg.connect(dsn) as conn:
                conn.execute(remove_prerequisite)
            with pytest.raises(migrate.MigrationError, match=prerequisite_error):
                migrate.apply_migrations(dsn)
            assert (await client.get("/readyz")).status_code == 503
            with psycopg.connect(dsn) as conn:
                conn.execute("CREATE SCHEMA IF NOT EXISTS mnemotrace")
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            assert migrate.apply_migrations(dsn) == pending
            assert (await client.get("/readyz")).status_code == 200

    with database_factory() as dsn:
        run(exercise(dsn))
