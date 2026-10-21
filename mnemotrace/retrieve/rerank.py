"""Cross-encoder 重排器(AGENTS.md「检索与模块」)。bi-encoder(BGE)提供廉价的候选池;
cross-encoder 对 (query, passage) 联合重打分,大幅提高"哪些 session/chunk 真正回答了问题"的精度。
这是对 LongMemEval _S 杠杆最大的检索升级。"""
from __future__ import annotations

from typing import Optional


class CrossEncoderReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-base", device: Optional[str] = None,
                 max_length: int = 512) -> None:
        from sentence_transformers import CrossEncoder  # 重依赖,懒加载

        self.model = CrossEncoder(model_name, max_length=max_length, device=device)

    def rerank(self, query: str, candidates: list[tuple[str, str]], top_k: int) -> list[tuple[str, float]]:
        """candidates:[(id, text), ...] -> [(id, 分数), ...] 最优在前,截断到 top_k。"""
        if not candidates:
            return []
        scores = self.model.predict([(query, text) for _, text in candidates], show_progress_bar=False)
        order = sorted(range(len(candidates)), key=lambda i: float(scores[i]), reverse=True)
        return [(candidates[i][0], float(scores[i])) for i in order[:top_k]]
