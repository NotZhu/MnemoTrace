"""读取 HTTP 服务与写入 worker 共用的运行配置。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, tzinfo
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

_POSTGRES_DSN = "MNEMOTRACE_POSTGRES_DSN"
_POOL_TIMEOUT_ENV = "MNEMOTRACE_DB_POOL_TIMEOUT_SECONDS"


def read_postgres_dsn(env: Mapping[str, str]) -> str:
    """读取并校验当前进程的 PostgreSQL 连接串。"""
    dsn = env.get(_POSTGRES_DSN, "").strip()
    _validate_postgres_dsn(dsn)
    return dsn


def _validate_postgres_dsn(dsn: str) -> None:
    if not dsn.strip():
        raise ValueError(f"请设置 {_POSTGRES_DSN}")
    try:
        if "\x00" in dsn:
            raise ValueError
        params = conninfo_to_dict(dsn)
    except (ProgrammingError, ValueError):
        raise ValueError(f"{_POSTGRES_DSN} 格式无效") from None

    timeout = params.get("connect_timeout")
    if timeout is None:
        return
    value = str(timeout).strip(" \t\n\r\v\f")
    try:
        if (
            not re.fullmatch(r"[+-]?[0-9]+", value)
            or not -(2**31) <= int(value) < 2**31
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            f"{_POSTGRES_DSN} 的 connect_timeout 须为 32 位有符号整数秒数"
        ) from None


@dataclass(frozen=True)
class DatabaseTimeouts:
    """运行进程的取连接、数据库锁等待与语句时限，单位为秒。"""

    lock: int = 1
    statement: int = 15
    maintenance: int = 60
    pool_wait: int = 3

    def __post_init__(self) -> None:
        if type(self.pool_wait) is not int or self.pool_wait <= 0:
            raise ValueError(f"{_POOL_TIMEOUT_ENV} 须为正整数秒数")
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
            pool_wait=_seconds(env, _POOL_TIMEOUT_ENV, cls.pool_wait),
        )


def _seconds(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 须为正整数秒数") from None


@dataclass(frozen=True)
class RuntimeSettings:
    """提供数据库连接串、推理环境和抽取时区。"""

    postgres_dsn: str = field(repr=False)
    inference_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    database_timeouts: DatabaseTimeouts = field(default_factory=DatabaseTimeouts)
    extraction_timezone: tzinfo = UTC

    def __post_init__(self) -> None:
        _validate_postgres_dsn(self.postgres_dsn)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """解析两个进程共用的数据库、推理和抽取配置。"""
        timezone = env.get("MNEMOTRACE_EXTRACTION_TIMEZONE")
        try:
            extraction_timezone = UTC if timezone is None else ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                "MNEMOTRACE_EXTRACTION_TIMEZONE 须为有效的 IANA 时区名称"
            ) from None
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
            extraction_timezone,
        )
