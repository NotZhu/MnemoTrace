"""Shared answer interpretation for the offline and persistent retrieval paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..types import Fact
from .lexical import bm25_query_terms, lexical_terms

def should_abstain(query, facts, semantic_scores, threshold):
    query_terms = bm25_query_terms(query)
    for fact in facts:
        predicate_terms = bm25_query_terms(fact.predicate)
        # A shared modifier alone does not identify a compound attribute.
        if (predicate_terms and predicate_terms <= query_terms
                or query_terms.intersection(lexical_terms(fact.object))):
            return False
    return max(semantic_scores, default=0.0) < threshold


def digest_match(query, text):
    """A lexical-only fallback must cover all informative query terms."""
    terms = bm25_query_terms(query)
    return len(terms) if terms and terms <= set(lexical_terms(text)) else 0


@dataclass
class SearchResult:
    query: str
    facts: list[Fact] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    via: str = "hybrid"  # "hybrid" | "multi-hop" | "abstain"
    abstained: bool = False
    _answer: Optional[str] = None
    limitations: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.limitations

    def answer(self) -> str:
        if self.abstained:
            return "I don't have that in memory."
        if self._answer is not None:
            return self._answer
        if not self.facts:
            return "I don't have that in memory."
        top = self.facts[0]
        return top.object or top.text

    def top(self) -> Optional[Fact]:
        return self.facts[0] if self.facts else None


def chain_terms(f: Fact) -> str:
    pred = f.predicate.replace("_", " ")
    return f"{f.subject} {pred} {f.object} {f.text}"
