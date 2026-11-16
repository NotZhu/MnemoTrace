"""Reciprocal Rank Fusion。一种对量纲鲁棒的组合方式,把多个排序信号(semantic、lexical、graph、
recency、salience)合并,而无需归一化不可比的分数区间。实现 AGENTS.md 「检索与模块」。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..config import Config
from ..types import Fact
from .lexical import literal_value_match


def weighted_rrf(rankings: dict[str, list[str]], weights: dict[str, float], k: int = 60) -> dict[str, float]:
    """对各路候选排名执行加权倒数排名融合。

    Args:
        rankings: 各信号的候选 ID 排名，最优项在前。
        weights: 各信号的权重，省略的信号权重为 1。
        k: 控制名次影响的平滑常数。

    Returns:
        候选 ID 到融合分数的映射。
    """
    fused: dict[str, float] = {}
    for name, ordered in rankings.items():
        w = weights.get(name, 1.0)
        for rank, doc_id in enumerate(ordered):
            fused[doc_id] = fused.get(doc_id, 0.0) + w * (1.0 / (k + rank + 1))
    return fused


def order_by_score(scores: dict[str, float]) -> list[str]:
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def rank_facts(
    query: str,
    facts: Iterable[Fact],
    config: Config,
    *,
    semantic: Mapping[str, float],
    lexical: Mapping[str, float],
    graph: Mapping[str, float],
    recency: Mapping[str, float],
    salience: Mapping[str, float],
) -> tuple[list[tuple[Fact, float]], dict[str, float]]:
    """融合各路信号排序，优先保留匹配字面值的事实。

    Args:
        query: 用于检查字面值匹配的文本。
        facts: 待排序的事实。
        config: 融合权重及参数。
        semantic: 各事实的语义分数。
        lexical: 各事实的词法分数。
        graph: 各事实的图分数。
        recency: 各事实的新近度分数。
        salience: 各事实的显著度分数。

    Returns:
        排序后的事实与融合分数，以及按事实 ID 索引的分数。
    """
    def rank_signal(scores: Mapping[str, float], *, evidence: bool = False) -> list[str]:
        ids = (fact_id for fact_id in scores if not evidence or scores[fact_id] > 0.0)
        return sorted(ids, key=lambda fact_id: (-scores[fact_id], fact_id))

    rankings = {
        "sem": rank_signal(semantic, evidence=True),
        "lex": rank_signal(lexical, evidence=True),
        "graph": rank_signal(graph, evidence=True),
        "rec": rank_signal(recency),
        "sal": rank_signal(salience),
    }
    weights = {
        "sem": config.w_sem,
        "lex": config.w_lex,
        "graph": config.w_graph,
        "rec": config.w_rec,
        "sal": config.w_sal,
    }
    fused = weighted_rrf(rankings, weights, config.rrf_k)
    ordered = sorted(facts, key=lambda fact: (
        not literal_value_match(query, fact.object), -fused[fact.id], fact.id,
    ))
    return [(fact, fused[fact.id]) for fact in ordered], fused
