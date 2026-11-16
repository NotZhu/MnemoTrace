"""Shared answer interpretation for the offline and persistent retrieval paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..types import Fact
from ..consolidate.fact_rules import object_key
from .events import EventEvidence
from .lexical import bm25_query_terms, lexical_terms

def should_abstain(query, facts, semantic_scores, threshold):
    """判断候选是否缺少足够的词法或语义支持。

    Args:
        query: 查询文本。
        facts: 待判断的候选事实。
        semantic_scores: 查询与候选的语义相似度。
        threshold: 接受语义证据的最低分数。

    Returns:
        缺少完整谓词、对象词项和足够语义匹配时返回 True。
    """
    query_terms = bm25_query_terms(query)
    for fact in facts:
        predicate_terms = bm25_query_terms(fact.predicate)
        # A shared modifier alone does not identify a compound attribute.
        if (predicate_terms and predicate_terms <= query_terms
                or query_terms.intersection(lexical_terms(fact.object))):
            return False
    return max(semantic_scores, default=0.0) < threshold


def digest_match(query, text):
    """检查回退摘要是否覆盖全部有效查询词项。

    Args:
        query: 查询文本。
        text: 候选摘要文本。

    Returns:
        完全覆盖时返回查询词项数，否则返回 0。
    """
    terms = bm25_query_terms(query)
    return len(terms) if terms and terms <= set(lexical_terms(text)) else 0


@dataclass
class SearchResult:
    """候选证据、可选唯一值和覆盖限制。"""
    query: str
    facts: list[Fact] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    via: str = "hybrid"  # "hybrid" | "multi-hop" | "abstain"
    abstained: bool = False
    _answer: Optional[str] = None
    limitations: tuple[str, ...] = ()
    sources: tuple[EventEvidence, ...] = ()
    source_ids: tuple[str, ...] = ()

    @property
    def status(self):
        """返回已回答、仅证据或无证据的结果状态。"""
        if self._answer is not None:
            return 'answered'
        return 'evidence_only' if self.facts or self.sources else 'no_evidence'

    @property
    def complete(self) -> bool:
        """判断搜索是否没有已报告的覆盖限制。"""
        return not self.limitations

    def answer(self) -> str:
        """返回唯一值，或根据证据状态返回空文本或无记忆提示。"""
        if self._answer is not None:
            return self._answer
        return "" if self.status == "evidence_only" else "I don't have that in memory."

    def top(self) -> Optional[Fact]:
        """返回首条候选事实。候选为空时返回 None。"""
        return self.facts[0] if self.facts else None


def slot_result(query, facts):
    """从显式主体和谓词筛出的事实中解析唯一值。

    Args:
        query: 原始查询文本。
        facts: 符合结构化条件的事实。

    Returns:
        候选事实和可选的唯一对象值。
    """
    values = {object_key(fact) for fact in facts}
    return SearchResult(query, facts, via='slot', abstained=not facts,
                        _answer=facts[0].object if len(values) == 1 else None)


def chain_terms(f: Fact) -> str:
    """拼接事实字段，用于筛选与查询相关的取代链。

    Args:
        f: 待转换的事实。

    Returns:
        包含主体、谓词、对象和原文表述的文本。
    """
    pred = f.predicate.replace("_", " ")
    return f"{f.subject} {pred} {f.object} {f.text}"
