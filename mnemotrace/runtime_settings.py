"""读取 HTTP 服务与写入 worker 共用的运行配置。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


@dataclass(frozen=True)
class RuntimeSettings:
    """提供数据库连接串与推理配置。"""

    postgres_dsn: str = field(repr=False)
    inference_env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.postgres_dsn.strip():
            raise ValueError("请设置 MNEMOTRACE_POSTGRES_DSN")
        try:
            if "\x00" in self.postgres_dsn:
                raise ValueError
            conninfo_to_dict(self.postgres_dsn)
        except (ProgrammingError, ValueError):
            raise ValueError("MNEMOTRACE_POSTGRES_DSN 格式无效") from None

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
            env.get("MNEMOTRACE_POSTGRES_DSN", "").strip(),
            inference_env,
        )
