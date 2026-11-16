"""Reciprocal Rank Fusion。一种对量纲鲁棒的组合方式,把多个排序信号(semantic、lexical、graph、
recency、salience)合并,而无需归一化不可比的分数区间。实现 AGENTS.md 「检索与模块」。"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..config import Config
from ..types import Fact
from .lexical import literal_value_match


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
    """Rank candidates using sparse evidence and a recency and salience score for each fact."""
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
