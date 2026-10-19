"""基于 LiteLLM 的 API 嵌入后端，默认 BAAI/bge-m3（1024 维）。
其他模型必须显式提供维度；需要在环境中配置对应 provider 的 API key。"""
from __future__ import annotations

from typing import Sequence

from .base import Embedder


class LiteLLMEmbedder(Embedder):
    def __init__(self, model: str = "openai/BAAI/bge-m3", dim: int | None = None) -> None:
        if dim is not None and (
            isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0
        ):
            raise ValueError("API embedding dimension must be a positive integer")
        model_name = model.removeprefix("openai/")
        if model_name == "BAAI/bge-m3":
            if dim is not None and dim != 1024:
                raise ValueError("BAAI/bge-m3 requires dimension 1024")
            dim = 1024
        elif dim is None:
            raise ValueError(f"API embedding model {model!r} requires an explicit dim")
        import litellm  # 延迟导入

        self._litellm = litellm
        self.model = model
        # manifest 在首次请求前校验，维度须从构造到重启保持稳定，不能随响应改写。
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._litellm.embedding(model=self.model, input=list(texts))
        vectors = [item["embedding"] for item in resp.data]
        for vector in vectors:
            if len(vector) != self.dim:
                raise ValueError(
                    f"API embedding dimension mismatch for {self.model!r}: "
                    f"expected {self.dim}, received {len(vector)}"
                )
        return vectors
