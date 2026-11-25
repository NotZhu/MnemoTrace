"""读取 HTTP 服务与写入 worker 共用的运行配置。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

_POSTGRES_DSN = "MNEMOTRACE_POSTGRES_DSN"


def read_postgres_dsn(env: Mapping[str, str]) -> str:
    """读取当前进程的 PostgreSQL 连接串并校验格式。"""
    dsn = env.get(_POSTGRES_DSN, "").strip()
    _validate_postgres_dsn(dsn)
    return dsn


def _validate_postgres_dsn(dsn: str) -> None:
    if not dsn.strip():
        raise ValueError(f"请设置 {_POSTGRES_DSN}")
    try:
        if "\x00" in dsn:
            raise ValueError
        conninfo_to_dict(dsn)
    except (ProgrammingError, ValueError):
        raise ValueError(f"{_POSTGRES_DSN} 格式无效") from None


@dataclass(frozen=True)
class DatabaseTimeouts:
    """运行角色的 PostgreSQL 锁等待与语句时限，单位为秒。"""

    lock: int = 1
    statement: int = 15
    maintenance: int = 60

    def __post_init__(self) -> None:
        if (
            not 0
            < self.lock
            < self.statement
            <= self.maintenance
            <= (2**31 - 1) // 1000
        ):
            raise ValueError(
                "数据库时限须为正整数，满足 DB_LOCK_TIMEOUT_SECONDS < "
                "DB_STATEMENT_TIMEOUT_SECONDS <= DB_MAINTENANCE_TIMEOUT_SECONDS，"
                "且可换算为 PostgreSQL 毫秒整数"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """读取 API 和 worker 共用的数据库时限。"""
        return cls(
            lock=_seconds(env, "MNEMOTRACE_DB_LOCK_TIMEOUT_SECONDS", 1),
            statement=_seconds(env, "MNEMOTRACE_DB_STATEMENT_TIMEOUT_SECONDS", 15),
            maintenance=_seconds(env, "MNEMOTRACE_DB_MAINTENANCE_TIMEOUT_SECONDS", 60),
        )


def _seconds(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 须为正整数秒数") from None


@dataclass(frozen=True)
class RuntimeSettings:
    """提供数据库连接串与推理配置。"""

    postgres_dsn: str = field(repr=False)
    inference_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    database_timeouts: DatabaseTimeouts = field(default_factory=DatabaseTimeouts)

    def __post_init__(self) -> None:
        _validate_postgres_dsn(self.postgres_dsn)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """解析两个进程共用的数据库与推理环境。"""
        inference_env = {
            key: value
            for key, value in env.items()
            if key.startswith(
                (
                    "MNEMOTRACE_LLM_",
                    "MNEMOTRACE_EMBED_",
                    "MNEMOTRACE_RERANK_",
                    "MNEMOTRACE_INFERENCE_",
                )
            )
        }
        return cls(
            env.get(_POSTGRES_DSN, "").strip(),
            inference_env,
            DatabaseTimeouts.from_env(env),
        )
