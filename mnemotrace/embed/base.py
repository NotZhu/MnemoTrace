"""嵌入器接口。真实后端（BGE-m3、OpenAI 等）实现该接口；离线哈希嵌入器是零依赖默认实现，
保证 demo 和测试在没有网络时也能运行。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence


class Embedder(ABC):
    dim: int

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]
