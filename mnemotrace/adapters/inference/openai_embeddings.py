"""按稳定嵌入身份生成与输入一一对应的向量。"""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256

from ...ports.inference import InferenceError, validate_embedding_batch
from ._http import (
    InferenceHTTP,
    InferenceRequestError,
    RequestBudget,
    require_response_object,
)


@dataclass(frozen=True)
class _EmbeddingOptions:
    model: str
    revision: str
    dim: int
    batch_size: int
    send_dimensions: bool
    timeout: float


class OpenAIEmbedder:
    """使用兼容的 Embeddings 路由生成余弦检索向量。

    Args:
        http: 由装配层共享的 HTTP 请求模块。
        base_url: 已规范化的 API 前缀。
        options: 启动时校验的模型及请求选项。
    """

    def __init__(
        self, http: InferenceHTTP, *, base_url: str, options: _EmbeddingOptions
    ) -> None:
        self._http = http
        self._options = options
        self._dimensions = options.dim if options.send_dimensions else None
        identity = json.dumps(
            {
                "endpoint": base_url,
                "model": options.model,
                "revision": options.revision,
                "dim": options.dim,
                "dimensions": self._dimensions,
                "input": "raw-text-v1",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._embedding_space = f"emb:v1:{sha256(identity.encode('utf-8')).hexdigest()}"

    @property
    def embedding_space(self) -> str:
        """标识本适配器生成的向量所属空间。

        Returns:
            由端点、模型修订、维度和输入规则派生的稳定标识。
        """
        return self._embedding_space

    @property
    def dim(self) -> int:
        """读取本适配器的向量维度配置。

        Returns:
            每条响应向量应包含的分量数。
        """
        return self._options.dim

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """返回完整且与输入顺序一致的向量批次。

        Args:
            texts: 按原样发送给上游的文本。

        Returns:
            按输入顺序排列的向量，空输入返回空序列。

        Raises:
            InferenceRequestError: 子批失败或上游返回无效向量。
            TimeoutError: 整次嵌入已超时。
        """
        if not texts:
            return ()
        deadline = asyncio.get_running_loop().time() + self._options.timeout
        size = self._options.batch_size
        vectors: list[tuple[float, ...]] = []
        async with asyncio.timeout_at(deadline):
            for start in range(0, len(texts), size):
                batch = list(texts[start : start + size])
                payload: dict[str, object] = {
                    "model": self._options.model,
                    "input": batch,
                }
                if self._dimensions is not None:
                    payload["dimensions"] = self._dimensions
                body = await self._http.post(
                    "embeddings",
                    payload,
                    budget=RequestBudget(deadline),
                    mode="embedding",
                )
                vectors.extend(_decode_vectors(body.get("data"), len(batch), self.dim))
        return tuple(vectors)


def _decode_vectors(
    value: object, size: int, dim: int
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, list):
        raise InferenceRequestError("protocol")
    by_index: dict[int, list[object]] = {}
    for item in value:
        record = require_response_object(item)
        index, embedding = record.get("index"), record.get("embedding")
        if type(index) is not int or not 0 <= index < size or index in by_index:
            raise InferenceRequestError("protocol")
        if not isinstance(embedding, list):
            raise InferenceRequestError("protocol")
        by_index[index] = embedding
    ordered = tuple(by_index[index] for index in range(size) if index in by_index)
    try:
        return validate_embedding_batch(ordered, expected_count=size, dimension=dim)
    except InferenceError:
        raise InferenceRequestError("protocol") from None
