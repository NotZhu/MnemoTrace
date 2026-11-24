"""为 API 和 worker 的运行连接安装统一的数据库时限。"""

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...runtime_settings import RuntimeSettings

type ConnectionPool = AsyncConnectionPool[AsyncConnection[TupleRow]]


def runtime_pool(
    settings: RuntimeSettings, *, max_size: int, max_waiting: int = 0
) -> ConnectionPool:
    """创建尚未打开的连接池，并在每条连接建立后初始化时限。"""
    limits = settings.database_timeouts

    async def configure(conn: AsyncConnection[TupleRow]) -> None:
        await conn.execute(
            "SELECT set_config('lock_timeout', %s, false), "
            "set_config('statement_timeout', %s, false)",
            (f"{limits.lock}s", f"{limits.statement}s"),
        )
        await conn.commit()

    return AsyncConnectionPool(
        settings.postgres_dsn,
        open=False,
        min_size=0,
        max_size=max_size,
        max_waiting=max_waiting,
        timeout=3,
        kwargs={"connect_timeout": 3},
        configure=configure,
    )
