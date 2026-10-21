"""Reciprocal Rank Fusion。一种对量纲鲁棒的组合方式,把多个排序信号(semantic、lexical、graph、
recency、salience)合并,而无需归一化不可比的分数区间。实现 AGENTS.md 「检索与模块」。"""
from __future__ import annotations


def weighted_rrf(rankings: dict[str, list[str]], weights: dict[str, float], k: int = 60) -> dict[str, float]:
    """rankings:{signal_name: [doc_id, ...]},最优在前。weights:{signal_name: 权重}。"""
    fused: dict[str, float] = {}
    for name, ordered in rankings.items():
        w = weights.get(name, 1.0)
        for rank, doc_id in enumerate(ordered):
            fused[doc_id] = fused.get(doc_id, 0.0) + w * (1.0 / (k + rank + 1))
    return fused


def order_by_score(scores: dict[str, float]) -> list[str]:
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]
