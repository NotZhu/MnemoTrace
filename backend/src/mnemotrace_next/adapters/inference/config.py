"""读取部署配置并管理推理客户端的生命周期。"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from math import isfinite
from typing import Literal, cast

import httpx

from ...ports.inference import LLM, Embedder
from ...ports.reranker import Reranker
from ._http import InferenceHTTP
from .cohere_reranker import CohereReranker
from .openai_chat import (
    OpenAIChatLLM,
    _ChatOptions,
    _JSONMode,
    _ThinkingStyle,
    _TokenLimitField,
)
from .openai_embeddings import OpenAIEmbedder, _EmbeddingOptions

_PREFIX = "MNEMOTRACE_NEXT_"


class _MissingInferenceSetting(ValueError):
    """表示所选模型缺少必填配置。"""


@dataclass(frozen=True)
class _Endpoint:
    base_url: httpx.URL
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class InferenceClients:
    """持有本次装配所选的推理端口。

    Attributes:
        llm: 选择补全角色时提供的端口。
        embedder: 选择嵌入角色时提供的端口。
        reranker: 选择重排角色时提供的端口。
    """

    llm: LLM | None = None
    embedder: Embedder | None = None
    reranker: Reranker | None = None


@asynccontextmanager
async def open_inference(
    env: Mapping[str, str],
    *,
    llm: bool | Literal["optional"] = False,
    embed: bool = False,
    rerank: bool = False,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
) -> AsyncIterator[InferenceClients]:
    """校验所选角色的配置并管理其 HTTP 客户端。

    Args:
        env: 部署配置的环境变量映射。
        llm: True 时必须配置补全角色，optional 时允许缺少必填项。
        embed: 是否启用嵌入角色。
        rerank: 是否启用重排角色。
        transport_factory: 可选的 HTTP 传输工厂，便于用固定响应验证。

    Yields:
        由上下文管理生命周期的推理端口。

    Raises:
        ValueError: 所选角色的配置缺失或无效。
    """
    try:
        chat = _chat_config(env) if llm else None
    except _MissingInferenceSetting:
        if llm != "optional":
            raise
        chat = None
    embeddings = _embedding_config(env) if embed else None
    ranking = _rerank_config(env) if rerank else None
    if chat is None and embeddings is None and ranking is None:
        yield InferenceClients()
        return
    connect_timeout = _positive_seconds(env, "INFERENCE_CONNECT_TIMEOUT_SECONDS", 5)
    max_in_flight = _positive_int(env, "INFERENCE_MAX_IN_FLIGHT", 4)
    max_response_bytes = _positive_int(
        env, "INFERENCE_MAX_RESPONSE_BYTES", 8 * 1024 * 1024
    )

    async with AsyncExitStack() as stack:
        clients: dict[_Endpoint, InferenceHTTP] = {}

        async def get_http(endpoint: _Endpoint) -> InferenceHTTP:
            if endpoint in clients:
                return clients[endpoint]
            headers = (
                {"Authorization": f"Bearer {endpoint.api_key}"}
                if endpoint.api_key
                else {}
            )
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=endpoint.base_url,
                    headers=headers,
                    follow_redirects=False,
                    trust_env=False,
                    limits=httpx.Limits(
                        max_connections=max_in_flight,
                        max_keepalive_connections=max_in_flight,
                    ),
                    transport=transport_factory()
                    if transport_factory is not None
                    else None,
                )
            )
            clients[endpoint] = InferenceHTTP(
                http_client,
                connect_timeout=connect_timeout,
                max_in_flight=max_in_flight,
                max_response_bytes=max_response_bytes,
            )
            return clients[endpoint]

        yield InferenceClients(
            llm=OpenAIChatLLM(await get_http(chat[0]), chat[1]) if chat else None,
            embedder=OpenAIEmbedder(
                await get_http(embeddings[0]),
                base_url=str(embeddings[0].base_url),
                options=embeddings[1],
            )
            if embeddings
            else None,
            reranker=CohereReranker(
                await get_http(ranking[0]), model=ranking[1], timeout=ranking[2]
            )
            if ranking
            else None,
        )


def _chat_config(env: Mapping[str, str]) -> tuple[_Endpoint, _ChatOptions]:
    base_url = _required(env, "LLM_BASE_URL")
    mode = _required(env, "LLM_JSON_MODE")
    model = _required(env, "LLM_MODEL")
    endpoint = _endpoint(env, "LLM", base_url)
    if mode not in ("json_schema", "json_object", "prompt"):
        raise ValueError(
            f"{_PREFIX}LLM_JSON_MODE 须为 json_schema、json_object 或 prompt"
        )
    field = env.get(f"{_PREFIX}LLM_TOKEN_LIMIT_FIELD", "max_completion_tokens")
    if field not in ("max_completion_tokens", "max_tokens"):
        raise ValueError(
            f"{_PREFIX}LLM_TOKEN_LIMIT_FIELD 须为 max_completion_tokens 或 max_tokens"
        )
    thinking = env.get(f"{_PREFIX}LLM_DISABLE_THINKING")
    if thinking not in (None, "deepseek", "dashscope"):
        raise ValueError(f"{_PREFIX}LLM_DISABLE_THINKING 须为 deepseek 或 dashscope")
    return endpoint, _ChatOptions(
        model=model,
        json_mode=cast(_JSONMode, mode),
        temperature=_temperature(env),
        max_output_tokens=_positive_int(env, "LLM_MAX_OUTPUT_TOKENS", 2048),
        token_limit_field=cast(_TokenLimitField, field),
        disable_thinking=cast(_ThinkingStyle | None, thinking),
        timeout=_positive_seconds(env, "LLM_TIMEOUT_SECONDS", 45),
    )


def _temperature(env: Mapping[str, str]) -> float | None:
    key = f"{_PREFIX}LLM_TEMPERATURE"
    raw = env.get(key)
    if raw is None:
        return None
    try:
        temperature = float(raw)
    except ValueError:
        raise ValueError(f"{key} 须为 0 到 2 之间的有限数") from None
    if not 0 <= temperature <= 2:
        raise ValueError(f"{key} 须为 0 到 2 之间的有限数")
    return temperature


def _embedding_config(env: Mapping[str, str]) -> tuple[_Endpoint, _EmbeddingOptions]:
    endpoint = _endpoint(env, "EMBED", _required(env, "EMBED_BASE_URL"))
    send_dimensions = env.get(f"{_PREFIX}EMBED_SEND_DIMENSIONS", "0")
    if send_dimensions not in ("0", "1"):
        raise ValueError(f"{_PREFIX}EMBED_SEND_DIMENSIONS 须为 0 或 1")
    return endpoint, _EmbeddingOptions(
        model=_required(env, "EMBED_MODEL"),
        revision=_required(env, "EMBED_REVISION"),
        dim=_positive_int(env, "EMBED_DIM"),
        batch_size=_positive_int(env, "EMBED_BATCH_SIZE", 10),
        send_dimensions=send_dimensions == "1",
        timeout=_positive_seconds(env, "EMBED_TIMEOUT_SECONDS", 30),
    )


def _rerank_config(env: Mapping[str, str]) -> tuple[_Endpoint, str, float]:
    endpoint = _endpoint(env, "RERANK", _required(env, "RERANK_BASE_URL"))
    return (
        endpoint,
        _required(env, "RERANK_MODEL"),
        _positive_seconds(env, "RERANK_TIMEOUT_SECONDS", 5),
    )


def _endpoint(env: Mapping[str, str], role: str, configured_url: str) -> _Endpoint:
    name = f"{role}_BASE_URL"
    try:
        url = httpx.URL(configured_url)
    except httpx.InvalidURL:
        raise ValueError(f"{_PREFIX}{name} 须为合法 API 前缀") from None
    authority = configured_url.partition("://")[2].partition("/")[0]
    if (
        not url.is_absolute_url
        or url.scheme not in ("http", "https")
        or (url.port is not None and not 1 <= url.port <= 65535)
        or url.userinfo
        or "@" in authority
        or "?" in configured_url
        or "#" in configured_url
    ):
        raise ValueError(
            f"{_PREFIX}{name} 须为不含凭据、查询参数和片段的 HTTP(S) API 前缀"
        )
    key_name = f"{role}_API_KEY"
    key = env.get(f"{_PREFIX}{key_name}", "").strip()
    if url.scheme == "http":
        if url.host not in ("localhost", "127.0.0.1", "::1") or key:
            raise ValueError(f"{_PREFIX}{name} 的 HTTP 连接只允许无密钥的本机回环端点")
    elif not key or not all(33 <= ord(char) <= 126 for char in key):
        error = ValueError if key else _MissingInferenceSetting
        raise error(f"{_PREFIX}{key_name} 须为有效的 HTTPS 端点密钥")
    return _Endpoint(url.copy_with(raw_path=url.raw_path.rstrip(b"/") + b"/"), key)


def _required(env: Mapping[str, str], name: str) -> str:
    key = f"{_PREFIX}{name}"
    value = env.get(key, "").strip()
    if not value:
        raise _MissingInferenceSetting(f"请设置 {key}")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int | None = None) -> int:
    key = f"{_PREFIX}{name}"
    text = _required(env, name) if default is None else env.get(key, str(default))
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"{key} 须为正整数") from None
    if value <= 0:
        raise ValueError(f"{key} 须为正整数")
    return value


def _positive_seconds(env: Mapping[str, str], name: str, default: float) -> float:
    key = f"{_PREFIX}{name}"
    try:
        value = float(env.get(key, str(default)))
    except ValueError:
        raise ValueError(f"{key} 须为有限正数") from None
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{key} 须为有限正数")
    return value
