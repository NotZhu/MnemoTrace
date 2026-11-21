"""读取 HTTP 进程和写入 worker 的运行配置。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


@dataclass(frozen=True)
class Settings:
    """保存 HTTP 服务启动所需的运行连接和请求限制。"""

    postgres_dsn: str = field(repr=False)
    max_body_bytes: int = 64 * 1024
    inference_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    queue_limit: int = 100

    def __post_init__(self) -> None:
        if not self.postgres_dsn.strip():
            raise ValueError("请设置 MNEMOTRACE_NEXT_POSTGRES_DSN")
        try:
            if "\x00" in self.postgres_dsn:
                raise ValueError
            conninfo_to_dict(self.postgres_dsn)
        except (ProgrammingError, ValueError):
            raise ValueError("MNEMOTRACE_NEXT_POSTGRES_DSN 格式无效") from None
        if self.queue_limit < 1:
            raise ValueError("MNEMOTRACE_NEXT_QUEUE_LIMIT 须为正整数")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """解析运行配置，缺失或非法时拒绝启动。"""
        dsn = env.get("MNEMOTRACE_NEXT_POSTGRES_DSN", "").strip()
        try:
            limit = int(env.get("MNEMOTRACE_NEXT_MAX_BODY_BYTES", "65536"))
            if limit < 1:
                raise ValueError
        except ValueError:
            raise ValueError("MNEMOTRACE_NEXT_MAX_BODY_BYTES 须为正整数") from None
        inference_env = {
            key: value
            for key, value in env.items()
            if key.startswith(
                (
                    "MNEMOTRACE_NEXT_LLM_",
                    "MNEMOTRACE_NEXT_EMBED_",
                    "MNEMOTRACE_NEXT_INFERENCE_",
                )
            )
        }
        try:
            queue_limit = int(env.get("MNEMOTRACE_NEXT_QUEUE_LIMIT", "100"))
        except ValueError:
            raise ValueError("MNEMOTRACE_NEXT_QUEUE_LIMIT 须为正整数") from None
        return cls(dsn, limit, inference_env, queue_limit)
