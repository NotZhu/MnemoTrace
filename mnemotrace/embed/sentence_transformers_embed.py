"""基于 sentence-transformers 的本地嵌入后端（如 BAAI/bge-*）。无需 API key —— 模型从 HuggingFace
下载一次后在本地运行。这是基准测试默认的嵌入器。"""
from __future__ import annotations

import threading
from typing import Optional, Sequence

from .base import Embedder


class SentenceTransformerEmbedder(Embedder):
    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        device: Optional[str] = None,
        normalize: bool = True,
        batch_size: int = 64,
    ) -> None:
        from sentence_transformers import SentenceTransformer  # 重依赖，保持延迟导入

        self.model_name = model_name
        self.model = SentenceTransformer(model_name, device=device)
        self.normalize = normalize
        self.batch_size = batch_size
        self.dim = int(self.model.get_sentence_embedding_dimension())
        # 单个 torch 模型不能安全地被多线程并发调用（GPU/MPS 后端在并行 encode 下会直接中止）。
        # 评测框架在线程池里跑问题，所以在这里把 encode 串行化。代价很小：批量 encode 很快，
        # 已不再是墙钟时间的瓶颈。
        self._lock = threading.Lock()

    def embed(self, text: str) -> list[float]:
        with self._lock:
            vec = self.model.encode([text], normalize_embeddings=self.normalize, show_progress_bar=False)[0]
        return vec.tolist()

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            arr = self.model.encode(
                list(texts),
                normalize_embeddings=self.normalize,
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
        return [row.tolist() for row in arr]
