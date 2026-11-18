"""把已认证账号绑定到 PostgreSQL 空间事务。"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal, overload

from psycopg import AsyncConnection, errors, sql
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...domain.ids import SpaceId
from ...domain.space import Space
from ...ports.storage import SpaceReader, SpaceSession
from .reader import PostgresReader
from .session import PostgresSession

_AUTHORIZED_SPACE = sql.SQL("""
    SELECT 1 FROM mnemotrace_next.spaces WHERE space_id = %s AND owner_id = %s
    """)


class PostgresStorage:
    """使用进程共享的连接池访问已认证账号的数据。

    Args:
        pool: 由服务端装配层管理的异步连接池。
        owner_id: 服务端从认证上下文取得的账号标识。
    """

    def __init__(
        self, pool: AsyncConnectionPool[AsyncConnection[TupleRow]], *, owner_id: str
    ) -> None:
        self._pool = pool
        self._owner_id = owner_id

    async def create_space(self, space: Space) -> None:
        """为绑定账号创建独立空间和初始代际。

        Args:
            space: 所有者与认证账号一致的新空间。

        Raises:
            PermissionError: 空间所有者与认证账号不一致。
            ValueError: 空间标识已存在。
        """
        if space.owner_id != self._owner_id:
            raise PermissionError("空间的所有者须与认证账号一致")
        try:
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO mnemotrace_next.spaces (space_id, owner_id, name, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (space.id.value, self._owner_id, space.name, space.created_at),
                )
        except errors.UniqueViolation:
            raise ValueError("空间标识已存在") from None

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
            LookupError: 进入会话时，绑定账号无法访问指定空间。
            RuntimeError: 退出写会话时，事务因已捕获的 SQL 错误而无法提交。
        """
        return self._session(space_id, write)

    @asynccontextmanager
    async def _session(
        self, space_id: SpaceId, write: bool
    ) -> AsyncIterator[SpaceReader]:
        async with self._pool.connection() as conn, conn.transaction():
            statement: sql.SQL | sql.Composed = _AUTHORIZED_SPACE
            reader: PostgresReader
            if write:
                await conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE"
                )
                statement = _AUTHORIZED_SPACE + sql.SQL(" FOR UPDATE")
                reader = PostgresSession(conn, space_id)
            else:
                await conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                reader = PostgresReader(conn, space_id)
            authorized = await conn.execute(statement, (space_id.value, self._owner_id))
            if await authorized.fetchone() is None:
                raise LookupError("空间不可访问")
            try:
                yield reader
                if write and conn.info.transaction_status == TransactionStatus.INERROR:
                    raise RuntimeError("写事务已中止，无法提交")
            finally:
                reader._close()
