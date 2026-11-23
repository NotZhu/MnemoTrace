"""签发和验证与账号绑定的高熵 API key。"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from uuid import uuid4

from psycopg import AsyncConnection, Connection
from psycopg.rows import TupleRow, class_row
from psycopg_pool import AsyncConnectionPool

_TOKEN = re.compile(r"mtn_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class ApiKeyInfo:
    """可列出的凭据元数据。"""

    key_id: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class IssuedApiKey:
    """新凭据及仅在签发时提供的明文。"""

    info: ApiKeyInfo
    token: str = field(repr=False)


async def authenticate(
    pool: AsyncConnectionPool[AsyncConnection[TupleRow]], token: str
) -> str | None:
    """验证凭据并返回服务端记录的账号，凭据不可用时返回 None。"""
    match = _TOKEN.fullmatch(token)
    if match is None:
        return None
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                """
                SELECT owner_id, secret_hash FROM mnemotrace.api_keys
                WHERE key_id = %s AND revoked_at IS NULL
                """,
                (match[1],),
            )
        ).fetchone()
    if row is None:
        return None
    owner_id, secret_hash = row
    return (
        str(owner_id)
        if compare_digest(sha256(token.encode()).digest(), secret_hash)
        else None
    )


def issue_key(conn: Connection[TupleRow], owner_id: str) -> IssuedApiKey:
    """为账号签发 key，数据库仅持久化校验值。"""
    key_id = uuid4().hex
    token = f"mtn_{key_id}.{token_urlsafe(32)}"
    with conn.cursor(row_factory=class_row(ApiKeyInfo)) as cursor:
        cursor.execute(
            """
            INSERT INTO mnemotrace.api_keys (key_id, owner_id, secret_hash)
            VALUES (%s, %s, %s)
            RETURNING key_id, created_at, revoked_at
            """,
            (key_id, owner_id, sha256(token.encode()).digest()),
        )
        info = cursor.fetchone()
        assert info is not None
    return IssuedApiKey(info, token)


def list_keys(conn: Connection[TupleRow], owner_id: str) -> list[ApiKeyInfo]:
    """列出指定账号的 key 元数据，包括已撤销项。"""
    with conn.cursor(row_factory=class_row(ApiKeyInfo)) as cursor:
        return cursor.execute(
            """
            SELECT key_id, created_at, revoked_at FROM mnemotrace.api_keys
            WHERE owner_id = %s ORDER BY key_id
            """,
            (owner_id,),
        ).fetchall()


def revoke_key(conn: Connection[TupleRow], owner_id: str, key_id: str) -> bool:
    """撤销本账号的指定凭据。重复调用保留首次撤销时间。"""
    cursor = conn.execute(
        """
        UPDATE mnemotrace.api_keys
        SET revoked_at = coalesce(revoked_at, statement_timestamp())
        WHERE owner_id = %s AND key_id = %s
        """,
        (owner_id, key_id),
    )
    return cursor.rowcount == 1
