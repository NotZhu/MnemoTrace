"""为 API 和 worker 的运行连接安装统一的数据库时限。"""

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from ...runtime_settings import RuntimeSettings

type ConnectionPool = AsyncConnectionPool[AsyncConnection[TupleRow]]
_DEFAULT_CONNECT_TIMEOUT = 3


def runtime_pool(
    settings: RuntimeSettings, *, max_size: int, max_waiting: int = 0
) -> ConnectionPool:
    """创建尚未打开的连接池，并在每条连接建立后初始化时限。"""
    limits = settings.database_timeouts
    dsn = settings.postgres_dsn
    if "connect_timeout" not in conninfo_to_dict(dsn):
        dsn = make_conninfo(dsn, connect_timeout=_DEFAULT_CONNECT_TIMEOUT)

    async def configure(conn: AsyncConnection[TupleRow]) -> None:
        await conn.execute(
            "SELECT set_config('lock_timeout', %s, false), "
            "set_config('statement_timeout', %s, false)",
            (f"{limits.lock}s", f"{limits.statement}s"),
        )
        await conn.commit()

    return AsyncConnectionPool(
        dsn,
        open=False,
        min_size=0,
        max_size=max_size,
        max_waiting=max_waiting,
        timeout=limits.pool_wait,
        configure=configure,
    )
