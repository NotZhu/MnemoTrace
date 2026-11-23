"""读取评测配置，校验端点并在请求时解析凭据。"""

import os
import tomllib
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, PositiveFloat, PositiveInt, field_validator, model_validator

from .contracts import BackendName, Benchmark, Record

ALL_BACKENDS: tuple[BackendName, ...] = ("mnemotrace", "full-context", "bm25")


class Endpoint(Record):
    """保存端点和凭据环境变量名。"""

    base_url: str
    key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """接受 HTTPS 和本机 HTTP 端点。"""
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or not (
                url.scheme == "https"
                or url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("端点须为无凭据的 HTTPS 或本机 HTTP 地址")
        _ = url.port
        return value.rstrip("/")

    def key(self) -> str | None:
        """读取当前进程的凭据，且不将其写入配置。"""
        if self.key_env is None:
            return None
        if not (value := os.environ.get(self.key_env)):
            raise ValueError(f"缺少凭据环境变量 {self.key_env}")
        return value


class Model(Endpoint):
    """固定模型版本、输出上限及可选价格。"""

    model: str = Field(min_length=1)
    max_output_tokens: PositiveInt = 512
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    disable_thinking: Literal["deepseek"] | None = None
    input_usd_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_usd_per_million: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )


class Memory(Endpoint):
    """标识独立评测实例及其写入和嵌入模型版本。"""

    version: str = Field(min_length=1)
    write_model: str = Field(min_length=1)
    embedding_model: str = Field(min_length=1)


class Config(Record):
    """定义一次评测中所有组共享的运行协议。"""

    benchmark: Benchmark
    data_dir: Path
    reader: Model
    judge: Model | None = None
    mnemotrace: Memory | None = None
    backends: tuple[BackendName, ...] = ALL_BACKENDS
    reader_context_tokens: PositiveInt
    tokenizer: str = "o200k_base"
    evidence_tokens: PositiveInt = 4096
    top_k: int = Field(default=20, ge=1, le=100)
    timeout_seconds: PositiveFloat = 90
    write_timeout_seconds: PositiveFloat = 240
    max_body_bytes: PositiveInt = 65536
    question_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_protocol(self) -> Self:
        """拒绝不完整的对照配置和重复题目。"""
        if not self.backends or len(set(self.backends)) != len(self.backends):
            raise ValueError("对照组须非空且唯一")
        if len(set(self.question_ids)) != len(self.question_ids):
            raise ValueError("选题包含重复 ID")
        if self.benchmark != "personamem-v2" and self.judge is None:
            raise ValueError("该基准需要配置裁判模型")
        if "mnemotrace" in self.backends and self.mnemotrace is None:
            raise ValueError("缺少 mnemotrace 实例配置")
        if self.reader.max_output_tokens >= self.reader_context_tokens:
            raise ValueError("回答输出上限须小于模型上下文窗口")
        return self


def load_config(path: Path) -> Config:
    """读取 TOML 并将数据路径固定为绝对路径。"""
    with path.open("rb") as file:
        config = Config.model_validate(tomllib.load(file))
    return config.model_copy(
        update={"data_dir": (path.parent / config.data_dir).resolve()}
    )
