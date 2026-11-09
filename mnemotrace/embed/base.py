"""Interface for optional semantic embedding backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class Embedder(ABC):
    """文本嵌入器接口。"""

    # 嵌入维度
    dim: int

    # 计算文本嵌入向量
    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class EmbeddingUnavailable(RuntimeError):
    """A configured embedding backend could not be loaded or used."""
