"""在 PostgreSQL 中接收、领取和原子完成记忆写入任务。"""

from collections.abc import Sequence
from datetime import timedelta
from hashlib import sha256
from json import dumps
from typing import cast
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from ...application.operations import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    ClaimedOperation,
    IdempotencyConflict,
    LeaseLost,
    Operation,
    OperationNotFound,
    OperationStatus,
    QueueFull,
    WriteModelsUnavailable,
)
from ...application.remember import MessageInput, RememberResult, RememberStage
from ...domain.ids import SessionId, SpaceId
from ...ports.storage import FactChange, SessionUnavailable, SpaceSession
from .reader import PostgresReader
from .session import PostgresSession
from .storage import PostgresStorage

_TABLE = "mnemotrace_next.remember_operations"
_FIELDS = (
    "operation_id, session_id, status, created_at, updated_at, next_retry_at, "
    "message_ids, fact_change_count, error_code, error_stage"
)
_ACTIVE_LEASE = """
    space_id = %(space)s AND operation_id = %(id)s AND status = 'running'
    AND lease_token = %(token)s AND lease_until > clock_timestamp()
    """


def _operation(row: TupleRow) -> Operation:
    (
        operation_id,
        session_id,
        status,
        created_at,
        updated_at,
        next_retry_at,
        message_ids,
        change_count,
        code,
        stage,
    ) = row
    return Operation(
        operation_id,
        SessionId(session_id),
        status,
        created_at,
        updated_at,
        next_retry_at,
        tuple(message_ids) if message_ids is not None else None,
        change_count,
        code,
        stage,
    )


def _lease(job: ClaimedOperation) -> dict[str, str]:
    return {
        "space": job.space_id.value,
        "id": job.operation_id,
        "token": job.lease_token,
    }


class PostgresOperations:
    """将任务接收绑定到账号，将状态更新绑定到有效租约。"""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[TupleRow]]) -> None:
        self._pool = pool

    async def submit(
        self,
        *,
        owner_id: str,
        space_id: SpaceId,
        session_id: SessionId,
        key: str,
        messages: tuple[MessageInput, ...],
        queue_limit: int,
        configured: bool,
    ) -> Operation:
        """授权并持久接收输入，或返回该幂等键对应的原任务。"""
        payload = [
            {"role": item.role, "content": item.content, "event_time": item.event_time}
            for item in messages
        ]
        request_hash = sha256(dumps((session_id.value, payload)).encode()).digest()
        key_hash = sha256(key.encode()).digest()
        storage = PostgresStorage(self._pool, owner_id=owner_id)
        async with storage.session(space_id, write=True) as writer:
            conn = cast(PostgresSession, writer)._conn
            cursor = await conn.execute(
                f"SELECT request_hash, {_FIELDS} FROM {_TABLE} WHERE space_id = %s AND key_hash = %s",
                (space_id.value, key_hash),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if existing[0] != request_hash:
                    raise IdempotencyConflict
                return _operation(existing[1:])
            if not await writer.has_session(session_id):
                raise SessionUnavailable
            if not configured:
                raise WriteModelsUnavailable
            cursor = await conn.execute(
                f"SELECT count(*) FROM {_TABLE} WHERE space_id = %s AND status IN ('queued', 'running')",
                (space_id.value,),
            )
            count = await cursor.fetchone()
            assert count is not None
            if count[0] >= queue_limit:
                raise QueueFull
            cursor = await conn.execute(
                f"""
                INSERT INTO {_TABLE}
                    (operation_id, space_id, session_id, key_hash, request_hash, messages)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING {_FIELDS}
                """,
                (
                    uuid4().hex,
                    space_id.value,
                    session_id.value,
                    key_hash,
                    request_hash,
                    Jsonb(payload),
                ),
            )
            row = await cursor.fetchone()
            assert row is not None
            return _operation(row)

    async def get(
        self, owner_id: str, space_id: SpaceId, operation_id: str
    ) -> Operation:
        """在已授权的空间快照内读取任务公开字段。"""
        storage = PostgresStorage(self._pool, owner_id=owner_id)
        async with storage.session(space_id) as reader:
            return await self._get(cast(PostgresReader, reader), operation_id)

    @staticmethod
    async def _get(reader: PostgresReader, operation_id: str) -> Operation:
        cursor = await reader._conn.execute(
            f"SELECT {_FIELDS} FROM {_TABLE} WHERE space_id = %s AND operation_id = %s",
            (reader.space_id.value, operation_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise OperationNotFound
        return _operation(row)

    async def list(
        self,
        owner_id: str,
        space_id: SpaceId,
        *,
        status: OperationStatus | None,
        session_id: SessionId | None,
        after: int | None,
        limit: int,
    ) -> Sequence[tuple[int, Operation]]:
        """按持久接受顺序分页读取所属空间的任务。"""
        storage = PostgresStorage(self._pool, owner_id=owner_id)
        conditions = ["space_id = %(space)s"]
        params: dict[str, object] = {"space": space_id.value, "limit": limit}
        if status is not None:
            conditions.append("status = %(status)s")
            params["status"] = status
        if session_id is not None:
            conditions.append("session_id = %(session)s")
            params["session"] = session_id.value
        if after is not None:
            conditions.append("enqueue_order < %(after)s")
            params["after"] = after
        async with storage.session(space_id) as reader:
            cursor = await cast(PostgresReader, reader)._conn.execute(
                f"""
                SELECT enqueue_order, {_FIELDS} FROM {_TABLE}
                WHERE {" AND ".join(conditions)}
                ORDER BY enqueue_order DESC LIMIT %(limit)s
                """,
                params,
            )
            return tuple(
                (row[0], _operation(row[1:])) for row in await cursor.fetchall()
            )

    async def list_fact_changes(
        self,
        owner_id: str,
        space_id: SpaceId,
        operation_id: str,
        *,
        after: int | None,
        limit: int,
        exclude_sensitive: bool,
    ) -> tuple[Operation, Sequence[FactChange]]:
        """在同一空间快照内读取任务状态及可见变更。"""
        storage = PostgresStorage(self._pool, owner_id=owner_id)
        async with storage.session(space_id) as session:
            reader = cast(PostgresReader, session)
            operation = await self._get(reader, operation_id)
            if operation.status != "succeeded":
                return operation, ()
            changes = await reader.list_operation_fact_changes(
                operation_id,
                after=after,
                limit=limit,
                exclude_sensitive=exclude_sensitive,
            )
            return operation, changes

    async def claim(self) -> ClaimedOperation | None:
        """领取可执行的会话队首任务，或结算已耗尽次数的过期任务。"""
        token = uuid4().hex
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"""
                WITH candidate AS (
                    SELECT op.operation_id
                    FROM {_TABLE} op
                    WHERE (
                        (op.status = 'queued' AND
                         (op.next_retry_at IS NULL OR op.next_retry_at <= statement_timestamp()))
                        OR (op.status = 'running' AND op.lease_until <= statement_timestamp())
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM {_TABLE} older
                        WHERE older.space_id = op.space_id AND older.session_id = op.session_id
                        AND older.enqueue_order < op.enqueue_order
                        AND older.status IN ('queued', 'running')
                    )
                    ORDER BY op.enqueue_order
                    LIMIT 1 FOR UPDATE OF op SKIP LOCKED
                )
                UPDATE {_TABLE} op SET
                    status = CASE WHEN op.attempts >= %(max)s THEN 'failed' ELSE 'running' END,
                    attempts = LEAST(op.attempts + 1, %(max)s),
                    updated_at = statement_timestamp(),
                    next_retry_at = NULL,
                    lease_token = CASE WHEN op.attempts < %(max)s THEN %(token)s END,
                    lease_until = CASE WHEN op.attempts < %(max)s
                        THEN statement_timestamp() + %(lease)s END,
                    messages = CASE WHEN op.attempts < %(max)s THEN op.messages END,
                    error_code = CASE WHEN op.attempts >= %(max)s THEN 'worker_lease_expired' END
                FROM candidate, mnemotrace_next.spaces space
                WHERE op.operation_id = candidate.operation_id AND space.space_id = op.space_id
                RETURNING op.operation_id, op.space_id, space.owner_id, op.session_id,
                    op.status, op.messages, op.created_at, op.attempts
                """,
                {
                    "max": MAX_ATTEMPTS,
                    "token": token,
                    "lease": timedelta(seconds=LEASE_SECONDS),
                },
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        (
            operation_id,
            space_id,
            owner_id,
            session_id,
            status,
            messages,
            created_at,
            attempts,
        ) = row
        if status == "failed":
            return None
        return ClaimedOperation(
            operation_id=operation_id,
            space_id=SpaceId(space_id),
            owner_id=owner_id,
            session_id=SessionId(session_id),
            messages=tuple(MessageInput(**message) for message in messages),
            received_at=created_at.timestamp(),
            attempt=attempts,
            lease_token=token,
        )

    async def complete(
        self, job: ClaimedOperation, writer: SpaceSession, result: RememberResult
    ) -> None:
        """在原文和事实的写事务中提交成功状态，租约无效时回滚整批。"""
        conn = cast(PostgresSession, writer)._conn
        cursor = await conn.execute(
            f"""
            UPDATE {_TABLE} SET status = 'succeeded', updated_at = clock_timestamp(),
                message_ids = %(message_ids)s, fact_change_count = %(change_count)s,
                messages = NULL,
                lease_token = NULL, lease_until = NULL
            WHERE {_ACTIVE_LEASE}
            """,
            {
                **_lease(job),
                "message_ids": [item.value for item in result.message_ids],
                "change_count": len(result.fact_changes),
            },
        )
        if cursor.rowcount != 1:
            raise LeaseLost
        if result.fact_changes:
            linked = await conn.execute(
                """
                UPDATE mnemotrace_next.fact_changes AS change SET operation_id = %(id)s
                FROM unnest(%(facts)s::text[], %(cursors)s::bigint[]) AS published(fact_id, cursor)
                WHERE change.space_id = %(space)s AND change.operation_id IS NULL
                  AND (change.fact_id, change.cursor) = (published.fact_id, published.cursor)
                """,
                {
                    "id": job.operation_id,
                    "space": job.space_id.value,
                    "facts": [change.after.id.value for change in result.fact_changes],
                    "cursors": [change.cursor for change in result.fact_changes],
                },
            )
            if linked.rowcount != len(result.fact_changes):
                raise RuntimeError("任务发布的事实审计关联数量不一致")

    async def record_attempt_failure(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        retry_delay: timedelta | None,
    ) -> None:
        """仅由当前租约安排重试或清除失败任务的原文副本。"""
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""
                UPDATE {_TABLE} SET
                    status = CASE WHEN %(delay)s::interval IS NULL THEN 'failed' ELSE 'queued' END,
                    updated_at = clock_timestamp(),
                    next_retry_at = clock_timestamp() + %(delay)s::interval,
                    lease_token = NULL, lease_until = NULL,
                    messages = CASE WHEN %(delay)s::interval IS NOT NULL THEN messages END,
                    error_code = CASE WHEN %(delay)s::interval IS NULL THEN %(code)s END,
                    error_stage = CASE WHEN %(delay)s::interval IS NULL THEN %(stage)s END
                WHERE {_ACTIVE_LEASE}
                """,
                {
                    **_lease(job),
                    "delay": retry_delay,
                    "code": code,
                    "stage": stage,
                },
            )

    async def has_active_lease(self, job: ClaimedOperation) -> bool:
        """通过新事务判断提交结果未知的尝试是否仍持有租约。"""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE {_ACTIVE_LEASE}", _lease(job)
            )
            return await cursor.fetchone() is not None
