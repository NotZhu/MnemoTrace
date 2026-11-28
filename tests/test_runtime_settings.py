"""验证数据库运行配置如何传入连接池。"""

import pytest
from psycopg.conninfo import conninfo_to_dict

from mnemotrace.adapters.postgres.pool import runtime_pool
from mnemotrace.runtime_settings import RuntimeSettings


@pytest.mark.parametrize(
    ("dsn", "connect_timeout"),
    (
        ("dbname=unused", "3"),
        ("dbname=unused connect_timeout=12", "12"),
        ("postgresql:///unused?connect_timeout=17", "17"),
    ),
)
def test_connection_pool_keeps_connect_timeout_independent(
    dsn: str, connect_timeout: str
) -> None:
    for pool_env, pool_timeout in (
        ({}, 3),
        ({"MNEMOTRACE_DB_POOL_TIMEOUT_SECONDS": "8"}, 8),
    ):
        settings = RuntimeSettings.from_env(
            {"MNEMOTRACE_POSTGRES_DSN": dsn, **pool_env}
        )
        connections = runtime_pool(settings, max_size=1)
        pool_dsn, pool_kwargs = connections.conninfo, connections.kwargs
        assert isinstance(pool_dsn, str)
        assert pool_kwargs is None or isinstance(pool_kwargs, dict)
        effective = conninfo_to_dict(pool_dsn, **(pool_kwargs or {}))
        assert effective["connect_timeout"] == connect_timeout
        assert connections.timeout == pool_timeout


@pytest.mark.parametrize("value", ("", "0", "-1", "1.5"))
def test_connection_pool_rejects_invalid_wait(value: str) -> None:
    with pytest.raises(ValueError, match="MNEMOTRACE_DB_POOL_TIMEOUT_SECONDS"):
        RuntimeSettings.from_env(
            {
                "MNEMOTRACE_POSTGRES_DSN": "dbname=unused",
                "MNEMOTRACE_DB_POOL_TIMEOUT_SECONDS": value,
            }
        )
