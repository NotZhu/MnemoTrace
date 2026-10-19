"""确定性、零依赖的哈希嵌入器（feature-hashing / "hashing trick"）。

它无法与训练出来的嵌入模型相提并论 —— 只能捕捉词汇层面的重合。它的职责是让整条流水线跑起来、
让测试在离线状态下通过。跑基准测试时请换成真正的 Embedder。
"""
from __future__ import annotations

import hashlib
import math

from .base import Embedder


def _h(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")


class HashingEmbedder(Embedder):
    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        from ..util import stems

        vec = [0.0] * self.dim
        toks = stems(text)
        for t in toks:
            h = _h(t)
            vec[h % self.dim] += 1.0 if (h >> 12) & 1 else -1.0
        # 加入轻量的 bigram 信号，让词序有一点影响
        for a, b in zip(toks, toks[1:]):
            h = _h(a + "\x1f" + b)
            vec[h % self.dim] += 0.5 if (h >> 12) & 1 else -0.5
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
