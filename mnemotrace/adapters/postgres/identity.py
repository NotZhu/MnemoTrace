"""签发和验证与账号绑定的高熵 API key。"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from secrets import choice
from string import ascii_letters, digits
from uuid import uuid4

from psycopg import AsyncConnection, Connection
from psycopg.rows import TupleRow, class_row
from psycopg_pool import AsyncConnectionPool

from ...domain.space import Space
from .rows import INSERT_SPACE, space_insert_values

DEFAULT_OWNER_ID = "owner"
_DEFAULT_OWNER_LOCK = (
    698771117284033231  # 与迁移锁分开，串行化默认账号的首次配置和签发。
)
_TOKEN_PREFIX = "mnemo_"
_TOKEN_ALPHABET = ascii_letters + digits
_TOKEN_RANDOM_LENGTH = 32
_TOKEN = re.compile(rf"{_TOKEN_PREFIX}[{_TOKEN_ALPHABET}]{{{_TOKEN_RANDOM_LENGTH}}}")


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


class DefaultOwnerAlreadyConfigured(ValueError):
    """默认账号已有密钥记录或空间。"""


async def authenticate(
    pool: AsyncConnectionPool[AsyncConnection[TupleRow]], token: str
) -> str | None:
    """验证凭据并返回服务端记录的账号，凭据不可用时返回 None。"""
    if _TOKEN.fullmatch(token) is None:
        return None
    digest = sha256(token.encode("ascii")).digest()
    async with pool.connection() as conn:
        cursor = await conn.execute(
            """
            SELECT owner_id FROM mnemotrace.api_keys
            WHERE secret_hash = %s AND revoked_at IS NULL
            """,
            (digest,),
        )
        row = await cursor.fetchone()
    return str(row[0]) if row is not None else None


def issue_key(conn: Connection[TupleRow], owner_id: str) -> IssuedApiKey:
    """为账号签发 key，数据库仅持久化校验值。"""
    if owner_id == DEFAULT_OWNER_ID:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DEFAULT_OWNER_LOCK,))
    return _insert_key(conn, owner_id)


def bootstrap_default_owner(conn: Connection[TupleRow], space: Space) -> IssuedApiKey:
    """在调用方的事务内为默认账号签发首把 key 并创建初始空间。"""
    if space.owner_id != DEFAULT_OWNER_ID:
        raise ValueError("初始空间须归默认账号")
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DEFAULT_OWNER_LOCK,))
    row = conn.execute(
        """
        SELECT EXISTS (SELECT 1 FROM mnemotrace.api_keys WHERE owner_id = %s)
            OR EXISTS (SELECT 1 FROM mnemotrace.spaces WHERE owner_id = %s)
        """,
        (DEFAULT_OWNER_ID, DEFAULT_OWNER_ID),
    ).fetchone()
    assert row is not None
    if row[0]:
        raise DefaultOwnerAlreadyConfigured
    issued = _insert_key(conn, DEFAULT_OWNER_ID)
    conn.execute(INSERT_SPACE, space_insert_values(space))
    return issued


def _insert_key(conn: Connection[TupleRow], owner_id: str) -> IssuedApiKey:
    key_id = uuid4().hex
    random_part = "".join(choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_RANDOM_LENGTH))
    token = _TOKEN_PREFIX + random_part
    with conn.cursor(row_factory=class_row(ApiKeyInfo)) as cursor:
        cursor.execute(
            """
            INSERT INTO mnemotrace.api_keys (key_id, owner_id, secret_hash)
            VALUES (%s, %s, %s)
            RETURNING key_id, created_at, revoked_at
            """,
            (key_id, owner_id, sha256(token.encode("ascii")).digest()),
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
