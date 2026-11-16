"""Cross-encoder 重排器(AGENTS.md「检索与模块」)。bi-encoder(BGE)提供廉价的候选池;
cross-encoder 对 (query, passage) 联合重打分,大幅提高"哪些 session/chunk 真正回答了问题"的精度。
这是对 LongMemEval _S 杠杆最大的检索升级。"""
from __future__ import annotations

from typing import Optional


class CrossEncoderReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-base", device: Optional[str] = None,
                 max_length: int = 512) -> None:
        """加载用于查询和候选文本联合打分的模型。

        Args:
            model_name: 重排模型名称。
            device: 可选的推理设备。
            max_length: 模型输入的最大序列长度。
        """
        from sentence_transformers import CrossEncoder  # 重依赖,懒加载

        self.model = CrossEncoder(model_name, max_length=max_length, device=device)

    def rerank(self, query: str, candidates: list[tuple[str, str]], top_k: int) -> list[tuple[str, float]]:
        """对查询和候选文本联合打分并重排。

        Args:
            query: 查询文本。
            candidates: 候选 ID 和文本的二元组列表。
            top_k: 返回候选数量上限。

        Returns:
            按分数降序排列的候选 ID 和分数。
        """
        if not candidates:
            return []
        scores = self.model.predict([(query, text) for _, text in candidates], show_progress_bar=False)
        order = sorted(range(len(candidates)), key=lambda i: float(scores[i]), reverse=True)
        return [(candidates[i][0], float(scores[i])) for i in order[:top_k]]
