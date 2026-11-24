"""定义离线推理测试的合成配置和响应。"""

from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager

import httpx

from mnemotrace.adapters.inference import open_inference
from mnemotrace.ports.inference import LLM

CHAT_ENV = {
    "MNEMOTRACE_LLM_BASE_URL": "https://models.example/compatible-mode/v1",
    "MNEMOTRACE_LLM_API_KEY": "fixture-chat-key",
    "MNEMOTRACE_LLM_MODEL": "synthetic-chat",
    "MNEMOTRACE_LLM_JSON_MODE": "json_schema",
    "MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS": "32768",
}
EMBED_ENV = {
    "MNEMOTRACE_EMBED_BASE_URL": "https://vectors.example/v1",
    "MNEMOTRACE_EMBED_API_KEY": "fixture-embed-key",
    "MNEMOTRACE_EMBED_MODEL": "synthetic-embedding",
    "MNEMOTRACE_EMBED_REVISION": "r1",
    "MNEMOTRACE_EMBED_DIM": "2",
}

type Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


@asynccontextmanager
async def configured_chat(
    handler: Handler, env: Mapping[str, str] = CHAT_ENV
) -> AsyncIterator[LLM]:
    """通过公开装配入口创建使用固定响应的补全端口。

    Args:
        handler: 模拟外部 HTTP 端点。
        env: 端口的合成部署配置。

    Yields:
        已装配的补全端口。
    """
    async with open_inference(
        env, llm=True, transport_factory=lambda: httpx.MockTransport(handler)
    ) as clients:
        assert clients.llm is not None
        yield clients.llm


def chat_response(
    content: str | None = "合成回答",
    *,
    finish_reason: str | None = "stop",
    refusal: str | None = None,
) -> httpx.Response:
    """构造一条 Chat Completions 响应。

    Args:
        content: 合成正文。
        finish_reason: 供应商报告的结束原因。
        refusal: 合成拒答信息。

    Returns:
        HTTP 200 及一条补全结果。
    """
    choice: dict[str, object] = {"message": {"content": content, "refusal": refusal}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return httpx.Response(200, json={"choices": [choice]})
