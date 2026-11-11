"""Shared answer interpretation for the offline and persistent retrieval paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..types import Fact
from ..consolidate.fact_rules import object_key
from .events import EventEvidence
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
    sources: tuple[EventEvidence, ...] = ()

    @property
    def status(self):
        if self._answer is not None:
            return 'answered'
        return 'evidence_only' if self.facts or self.sources else 'no_evidence'

    @property
    def complete(self) -> bool:
        return not self.limitations

    def answer(self) -> str:
        if self._answer is not None:
            return self._answer
        return "" if self.status == "evidence_only" else "I don't have that in memory."

    def top(self) -> Optional[Fact]:
        return self.facts[0] if self.facts else None


def slot_result(query, facts):
    values = {object_key(fact) for fact in facts}
    return SearchResult(query, facts, via='slot', abstained=not facts,
                        _answer=facts[0].object if len(values) == 1 else None)


def chain_terms(f: Fact) -> str:
    pred = f.predicate.replace("_", " ")
    return f"{f.subject} {pred} {f.object} {f.text}"
