"""Shared answer interpretation for the offline and persistent retrieval paths."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..types import Fact
from .lexical import overlap_terms


# 过于泛化的词，单凭它们无法确认某个属性（"favorite food" 不能仅因都含 "favorite" 就匹配到
# "favorite programming language"）。
_GENERIC_ATTR_TERMS = {"favorite", "favourite", "name", "is", "are", "of", "the"}

def should_abstain(query, facts, semantic_scores, threshold):
    for fact in facts:
        attributes = fact.predicate.replace("_", " ") + " " + fact.object
        if overlap_terms(query, attributes) - _GENERIC_ATTR_TERMS:
            return False
    return max(semantic_scores, default=0.0) < threshold


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


_INSTRUCTION_PREDS = frozenset({
    "wants", "wants_reminder", "instruction", "prefers", "prefers_format", "asks_to", "always",
    "never", "remind", "rule", "routine", "how_to", "procedure", "wants_me_to",
})


def chain_terms(f: Fact) -> str:
    pred = f.predicate.replace("_", " ")
    aliases = ""
    if f.predicate.lower() in {"works_at", "employer", "company"}:
        aliases = " work works worked employer employed employment company job"
    elif f.predicate.lower() in {"lives_in", "location", "city", "home"}:
        aliases = " live lives lived location city home"
    return f"{f.subject} {pred} {f.object} {f.text} {aliases}"
