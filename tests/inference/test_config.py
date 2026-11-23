"""验证部署配置和客户端的装配边界。"""

import asyncio

import httpx
import pytest

from mnemotrace.adapters.inference import InferenceRequestError, open_inference

from .sample import CHAT_ENV, EMBED_ENV, chat_response


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("LLM_MODEL", ""),
        ("LLM_API_KEY", ""),
        ("LLM_TEMPERATURE", "nan"),
        ("LLM_TEMPERATURE", "2.1"),
        ("EMBED_DIM", "0"),
        ("EMBED_REVISION", ""),
        ("EMBED_SEND_DIMENSIONS", "yes"),
        ("EMBED_TIMEOUT_SECONDS", "nan"),
        ("EMBED_BASE_URL", "https://fixture-user:fixture-password@vectors.example/v1"),
        ("EMBED_BASE_URL", "https://vectors.example/v1?fixture-token=value"),
        ("EMBED_BASE_URL", "http://vectors.example/v1"),
        ("EMBED_BASE_URL", "https://vectors.example:65536/v1"),
        ("EMBED_BASE_URL", "http://127.0.0.1:11434/v1"),
        ("EMBED_API_KEY", ""),
        ("INFERENCE_MAX_IN_FLIGHT", "0"),
    ],
)
def test_invalid_selected_configuration_prevents_client_creation(
    setting: str, value: str
) -> None:
    """任一所选角色配置错误时，装配在创建传输前失败。"""

    def transport() -> httpx.AsyncBaseTransport:
        raise AssertionError("应先检查两个角色的配置")

    async def exercise() -> None:
        env = {**CHAT_ENV, **EMBED_ENV, f"MNEMOTRACE_{setting}": value}
        with pytest.raises(ValueError, match=f"MNEMOTRACE_{setting}"):
            async with open_inference(
                env, llm=True, embed=True, transport_factory=transport
            ):
                pytest.fail("应在装配时失败")

    asyncio.run(exercise())


def test_roles_with_same_host_use_their_own_credentials() -> None:
    """同一端点上的不同角色凭据不会串用。"""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            assert request.headers["authorization"] == "Bearer fixture-chat-key"
            return chat_response()
        assert request.url.path == "/compatible-mode/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fixture-embed-key"
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    async def exercise() -> None:
        env = {
            **CHAT_ENV,
            **EMBED_ENV,
            "MNEMOTRACE_EMBED_BASE_URL": CHAT_ENV["MNEMOTRACE_LLM_BASE_URL"],
        }
        async with open_inference(
            env,
            llm=True,
            embed=True,
            transport_factory=lambda: httpx.MockTransport(respond),
        ) as clients:
            assert clients.llm is not None and clients.embedder is not None
            assert (
                await clients.llm.complete(system="回复", prompt="输入")
            ).text == "合成回答"
            assert await clients.embedder.embed(("输入",)) == ((1.0, 0.0),)

    asyncio.run(exercise())


def test_local_embeddings_omit_credentials_and_reject_redirects() -> None:
    """本地回环无密钥接入，重定向由适配器报告为响应错误。"""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:11434/v1/embeddings"
        assert "authorization" not in request.headers
        return httpx.Response(
            307, headers={"location": "https://unexpected.example/embeddings"}
        )

    async def exercise() -> None:
        env = {
            **EMBED_ENV,
            "MNEMOTRACE_EMBED_BASE_URL": "http://127.0.0.1:11434/v1",
            "MNEMOTRACE_EMBED_API_KEY": "",
        }
        async with open_inference(
            env, embed=True, transport_factory=lambda: httpx.MockTransport(respond)
        ) as clients:
            assert clients.embedder is not None
            with pytest.raises(InferenceRequestError) as captured:
                await clients.embedder.embed(("合成输入",))
            assert captured.value.status_code == 307

    asyncio.run(exercise())
