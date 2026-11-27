"""把已认证账号绑定到 PostgreSQL 空间事务。"""

from asyncio import CancelledError, create_task, current_task, shield
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from hashlib import sha256
from typing import Literal, cast, overload

from psycopg import AsyncConnection, errors, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow, args_row
from psycopg_pool import AsyncConnectionPool

from ...domain.ids import SpaceId
from ...domain.space import Space
from ...ports.storage import (
    CommitOutcomeUnknown,
    SpaceReader,
    SpaceSession,
    SpaceUnavailable,
)
from .profile_schedule import cancel_auto_refresh, schedule_pending_profile
from .reader import PostgresReader
from .rows import INSERT_SPACE, space_from_row, space_insert_values
from .session import PostgresSession
from .session_summary_schedule import cancel_summaries

_AUTHORIZED_SPACE = sql.SQL("""
    SELECT session_summary_enabled FROM mnemotrace.spaces WHERE space_id = %s AND owner_id = %s
    """)
_UNCERTAIN_COMMIT_STATES = {"08000", "08003", "08006", "08007", "40003"}


@asynccontextmanager
async def _session_transaction(
    conn: AsyncConnection[TupleRow], *, write: bool
) -> AsyncIterator[None]:
    committing = False
    try:
        async with conn.transaction() as transaction:
            yield
            if write and conn.closed:
                raise errors.OperationalError("写事务在提交前失去连接")
            committing = write
    except CancelledError as exc:
        if committing:
            raise CommitOutcomeUnknown("提交确认等待被取消") from exc
        raise
    except (errors.OperationalError, ConnectionError, TimeoutError) as exc:
        if committing and (
            not isinstance(exc, errors.OperationalError)
            or exc.sqlstate is None
            or exc.sqlstate in _UNCERTAIN_COMMIT_STATES
        ):
            raise CommitOutcomeUnknown("未能确认空间写事务是否已提交") from exc
        raise
    if write and transaction.status is transaction.Status.FAILED:
        raise errors.OperationalError("写事务在提交前失去连接")


async def _return_committed_connection(scope: AsyncExitStack) -> None:
    # 提交已经确认。连接归还期间的取消不能改变调用方看到的写入结果。
    release = create_task(scope.aclose())
    while not release.done():
        try:
            await shield(release)
        except CancelledError:
            if release.cancelled():
                raise
            if (caller := current_task()) is not None:
                caller.uncancel()
    release.result()


class PostgresStorage:
    """使用进程共享的连接池访问已认证账号的数据。

    Args:
        pool: 由服务端装配层管理的异步连接池。
        owner_id: 服务端从认证上下文取得的账号标识。
        maintenance_timeout: 擦除操作的单语句时限，单位为秒。
    """

    def __init__(
        self,
        pool: AsyncConnectionPool[AsyncConnection[TupleRow]],
        *,
        owner_id: str,
        maintenance_timeout: int = 60,
    ) -> None:
        self._pool = pool
        self._owner_id = owner_id
        self._maintenance_timeout = maintenance_timeout

    async def create_space(self, space: Space) -> None:
        """为绑定账号创建独立空间和初始代际。

        Args:
            space: 所有者与认证账号一致的新空间。

        Raises:
            PermissionError: 空间所有者与认证账号不一致。
            ValueError: 空间标识已存在。
            CommitOutcomeUnknown: 创建事务进入提交阶段后无法确认结果。
        """
        if space.owner_id != self._owner_id:
            raise PermissionError("空间的所有者须与认证账号一致")
        try:
            async with AsyncExitStack() as connection:
                conn = await connection.enter_async_context(self._pool.connection())
                async with _session_transaction(conn, write=True):
                    await conn.execute(INSERT_SPACE, space_insert_values(space))
                await _return_committed_connection(connection.pop_all())
        except errors.UniqueViolation:
            raise ValueError("空间标识已存在") from None

    async def list_spaces(self, *, after_id: str, limit: int) -> list[Space]:
        """读取当前账号拥有的空间，包括尚未写入记忆的空间。"""
        async with (
            self._pool.connection() as conn,
            conn.cursor(row_factory=args_row(space_from_row)) as cursor,
        ):
            await cursor.execute(
                """
                SELECT space_id, owner_id, name, created_at, memory_decay, profile_auto_refresh,
                    session_summary_enabled
                FROM mnemotrace.spaces
                WHERE owner_id = %s AND space_id > %s
                ORDER BY space_id LIMIT %s
                """,
                (self._owner_id, after_id, limit),
            )
            return await cursor.fetchall()

    async def update_space(
        self,
        space_id: SpaceId,
        *,
        name: str | None,
        memory_decay: bool | None,
        profile_auto_refresh: bool | None = None,
        session_summary_enabled: bool | None = None,
    ) -> Space:
        """检查空间归属并更新提供的设置，None 表示保留已有值。"""
        async with (
            self._pool.connection() as conn,
            _session_transaction(conn, write=True),
            conn.cursor(row_factory=args_row(space_from_row)) as cursor,
        ):
            await cursor.execute(
                """
                UPDATE mnemotrace.spaces
                SET name = coalesce(%s, name), memory_decay = coalesce(%s, memory_decay),
                    profile_auto_refresh = coalesce(%s, profile_auto_refresh),
                    session_summary_enabled = coalesce(%s, session_summary_enabled)
                WHERE space_id = %s AND owner_id = %s
                RETURNING space_id, owner_id, name, created_at, memory_decay, profile_auto_refresh,
                    session_summary_enabled
                """,
                (
                    name,
                    memory_decay,
                    profile_auto_refresh,
                    session_summary_enabled,
                    space_id.value,
                    self._owner_id,
                ),
            )
            space = await cursor.fetchone()
            if space is None:
                raise SpaceUnavailable("空间不可访问")
            if profile_auto_refresh is False:
                await cancel_auto_refresh(conn, space_id)
            if profile_auto_refresh is True:
                await schedule_pending_profile(conn, space_id)
            if session_summary_enabled is False:
                await cancel_summaries(conn, space_id)
        return space

    async def purge_space(self, space_id: SpaceId, *, key: str) -> None:
        """按幂等键清空绑定账号的空间，提交后保留空间登记。"""
        async with self.maintenance_session(space_id) as writer:
            await cast(PostgresSession, writer).purge_space(
                key_hash=sha256(key.encode()).digest()
            )

    async def delete_space(self, space_id: SpaceId) -> None:
        """提交对绑定账号空间及其内容的完整删除。"""
        async with self.maintenance_session(space_id) as writer:
            await cast(PostgresSession, writer).delete_space()

    @asynccontextmanager
    async def maintenance_session(
        self, space_id: SpaceId
    ) -> AsyncIterator[SpaceSession]:
        """为擦除扩大单语句时限，归还连接前恢复池默认值。"""
        async with self.session(space_id, write=True) as writer:
            await cast(PostgresSession, writer)._conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{self._maintenance_timeout}s",),
            )
            yield writer

    @overload
    def session(
        self, space_id: SpaceId, *, write: Literal[True]
    ) -> AbstractAsyncContextManager[SpaceSession]: ...

    @overload
    def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AbstractAsyncContextManager[SpaceReader]: ...

    def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AbstractAsyncContextManager[SpaceReader]:
        """授权空间并打开一致读快照或串行写事务。

        Args:
            space_id: 待授权的空间标识。
            write: 为 True 时持有空间行锁并打开写会话。

        Returns:
            管理连接和事务生命周期的异步上下文管理器。

        Raises:
            SpaceUnavailable: 进入会话时，绑定账号无法访问指定空间。
            RuntimeError: 退出写会话时，事务因已捕获的 SQL 错误而无法提交。
            CommitOutcomeUnknown: 写事务进入提交阶段后无法确认结果。
        """
        return self._session(space_id, write)

    @asynccontextmanager
    async def _session(
        self, space_id: SpaceId, write: bool
    ) -> AsyncIterator[SpaceReader]:
        async with AsyncExitStack() as connection:
            conn = await connection.enter_async_context(self._pool.connection())
            async with _session_transaction(conn, write=write):
                statement: sql.SQL | sql.Composed = _AUTHORIZED_SPACE
                if write:
                    await conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE"
                    )
                    statement = _AUTHORIZED_SPACE + sql.SQL(" FOR UPDATE")
                else:
                    await conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                authorized = await conn.execute(
                    statement, (space_id.value, self._owner_id)
                )
                row = await authorized.fetchone()
                if row is None:
                    raise SpaceUnavailable("空间不可访问")
                reader_type = PostgresSession if write else PostgresReader
                reader = reader_type(conn, space_id, session_summary_enabled=row[0])
                try:
                    yield reader
                    if (
                        write
                        and conn.info.transaction_status == TransactionStatus.INERROR
                    ):
                        raise RuntimeError("写事务已中止，无法提交")
                finally:
                    reader._close()
            if write:
                await _return_committed_connection(connection.pop_all())
