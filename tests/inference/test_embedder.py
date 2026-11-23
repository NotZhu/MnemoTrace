"""验证嵌入端口的顺序、向量约束和身份。"""

import asyncio
import json

import httpx
import pytest

from mnemotrace.adapters.inference import InferenceRequestError, open_inference

from .sample import EMBED_ENV


def test_embed_preserves_order_across_batches() -> None:
    """跨批输入及批内乱序响应仍一一对应，空批不访问端点。"""
    batches: list[list[str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://vectors.example/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer fixture-embed-key"
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic-embedding"
        assert payload["dimensions"] == 2
        batches.append(payload["input"])
        if len(batches) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0, 1]},
                        {"index": 0, "embedding": [1, 0]},
                    ]
                },
            )
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [-1, 0]}]})

    async def exercise() -> None:
        env = {
            **EMBED_ENV,
            "MNEMOTRACE_EMBED_BATCH_SIZE": "2",
            "MNEMOTRACE_EMBED_SEND_DIMENSIONS": "1",
        }
        async with open_inference(
            env, embed=True, transport_factory=lambda: httpx.MockTransport(respond)
        ) as clients:
            embedder = clients.embedder
            assert embedder is not None
            assert await embedder.embed(()) == ()
            assert await embedder.embed(("茶", "  咖啡  ", "果汁")) == (
                (1.0, 0.0),
                (0.0, 1.0),
                (-1.0, 0.0),
            )
        assert batches == [["茶", "  咖啡  "], ["果汁"]]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": 0, "embedding": [1, 0]}],
        [{"embedding": [1, 0]}, {"index": 1, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": -1, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [True, 1]}, {"index": 1, "embedding": [0, 1]}],
        [
            {"index": 0, "embedding": [float("inf"), 1]},
            {"index": 1, "embedding": [0, 1]},
        ],
    ],
)
def test_malformed_embedding_batch_is_rejected(rows: list[dict[str, object]]) -> None:
    """索引、维度或分量无效时整批失败。"""

    async def exercise() -> None:
        async with open_inference(
            EMBED_ENV,
            embed=True,
            transport_factory=lambda: httpx.MockTransport(
                lambda _: httpx.Response(200, content=json.dumps({"data": rows}))
            ),
        ) as clients:
            assert clients.embedder is not None
            with pytest.raises(InferenceRequestError) as captured:
                await clients.embedder.embed(("茶", "果汁"))
            assert captured.value.kind == "protocol"

    asyncio.run(exercise())


def test_each_batch_gets_one_retry_and_a_failed_last_batch_fails_the_call() -> None:
    """后续子批有自己的补试次数，失败时不会返回前半批。"""
    statuses = iter((503, 200, 503, 503))

    def respond(request: httpx.Request) -> httpx.Response:
        assert "dimensions" not in json.loads(request.content)
        status = next(statuses, None)
        assert status is not None
        if status == 503:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    async def exercise() -> None:
        env = {**EMBED_ENV, "MNEMOTRACE_EMBED_BATCH_SIZE": "1"}
        async with open_inference(
            env, embed=True, transport_factory=lambda: httpx.MockTransport(respond)
        ) as clients:
            assert clients.embedder is not None
            with pytest.raises(InferenceRequestError, match="upstream"):
                await clients.embedder.embed(("茶", "果汁"))
        assert next(statuses, None) is None

    asyncio.run(exercise())


def test_embedding_space_tracks_model_identity_and_ignores_operational_settings() -> (
    None
):
    """同维模型变更也隔离向量，端点等价写法和运行设置保持身份。"""

    async def identity(**overrides: str) -> str:
        env = {
            **EMBED_ENV,
            **{f"MNEMOTRACE_EMBED_{key}": val for key, val in overrides.items()},
        }
        async with open_inference(env, embed=True) as clients:
            assert clients.embedder is not None
            return clients.embedder.embedding_space

    async def exercise() -> None:
        original = await identity()
        assert original.startswith("emb:v1:") and len(original) == 71
        assert (
            await identity(
                BASE_URL="https://VECTORS.example:443/v1/",
                API_KEY="fixture-rotated-key",
                BATCH_SIZE="2",
                TIMEOUT_SECONDS="20",
            )
            == original
        )
        assert (
            len(
                {
                    original,
                    await identity(BASE_URL="https://vectors.example/v2"),
                    await identity(MODEL="same-dimension-new-model"),
                    await identity(REVISION="r2"),
                    await identity(DIM="3"),
                    await identity(SEND_DIMENSIONS="1"),
                }
            )
            == 6
        )

    asyncio.run(exercise())
