"""读取 HTTP 服务的运行配置和请求限制。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from ..runtime_settings import RuntimeSettings

_DEFAULT_MAX_BODY_BYTES = 64 * 1024
_DEFAULT_QUEUE_LIMIT = 100


def _int_setting(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} 须为正整数") from None


@dataclass(frozen=True)
class Settings:
    """保存 HTTP 服务启动所需的运行连接和请求限制。"""

    runtime: RuntimeSettings
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES
    queue_limit: int = _DEFAULT_QUEUE_LIMIT

    def __post_init__(self) -> None:
        if self.max_body_bytes < 1:
            raise ValueError("MNEMOTRACE_MAX_BODY_BYTES 须为正整数")
        if self.queue_limit < 1:
            raise ValueError("MNEMOTRACE_QUEUE_LIMIT 须为正整数")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        """解析运行配置，缺失或非法时拒绝启动。"""
        return cls(
            RuntimeSettings.from_env(env),
            max_body_bytes=_int_setting(
                env, "MNEMOTRACE_MAX_BODY_BYTES", _DEFAULT_MAX_BODY_BYTES
            ),
            queue_limit=_int_setting(
                env, "MNEMOTRACE_QUEUE_LIMIT", _DEFAULT_QUEUE_LIMIT
            ),
        )
