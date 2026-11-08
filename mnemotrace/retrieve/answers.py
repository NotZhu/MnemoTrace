"""Shared answer interpretation for the offline and persistent retrieval paths."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..types import Fact
from ..util import time_key
from .lexical import overlap_terms, stems


# 过于泛化的词，单凭它们无法确认某个属性（"favorite food" 不能仅因都含 "favorite" 就匹配到
# "favorite programming language"）。
_GENERIC_ATTR_TERMS = {"favorite", "favourite", "name", "is", "are", "of", "the"}

# 答案类型对齐（#2/#3）：当问题要求结构化的值（id、日期、数字、邮箱、电话、url）时，答案的
# object 必须真的长得像该类型——否则高语义匹配是虚假的（例如问 "what's the project ID?" 却
# 检索到项目负责人的名字）。此时我们改为给出类型匹配的 fact，或拒答，而不是自信地返回类型
# 不符的 top fact。线索刻意取得很强（id/编号，而非单独的 "when"），以避免对自由文本答案误判拒答。
_ANSWER_TYPE_CUES = {
    "email": ("email", "e-mail", "邮箱", "邮件地址"),
    "url": ("url", "链接", "网址", "link to"),
    "phone": ("phone number", "telephone", "电话", "手机号", "联系电话", "phone"),
    "id": ("id", "编号", "工单号", "订单号", "identifier", "order number", "ticket number", "serial"),
    "date": ("what date", "which day", "date of", "日期", "什么时候", "哪天", "哪一天", "几号", "哪一年"),
    "number": ("how many", "how much", "number of", "多少", "几个", "数量", "几次", "几年", "几岁"),
}
_ANSWER_TYPE_MATCH = {
    "email": lambda o: bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", o)),
    "url": lambda o: bool(re.search(r"https?://|www\.", o)),
    "phone": lambda o: bool(re.search(r"\+?\d[\d\s().\-]{6,}\d", o)),
    # id 是含数字、无空格的字母数字编码（PROJ-1024、A12B）；普通名字不具备这些特征
    "id": lambda o: bool(re.search(r"\d", o)) and len(o.strip()) <= 40 and " " not in o.strip(),
    "date": lambda o: bool(re.search(r"\b\d{4}\b|\d{1,2}[-/]\d{1,2}|年|月|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", o.lower())),
    "number": lambda o: bool(re.search(r"\d", o)),  # 宽松：任意数字（数量/时长/年龄）
}
_TEMPORAL_HISTORY_RE = re.compile(
    r"\b(before|previous|previously|former|formerly|used\s+to|past|old(?:er)?|prior)\b|"
    r"以前|之前|曾经|过去|原来|从前|上一",
    re.IGNORECASE,
)
_PROCEDURAL_QUERY_RE = re.compile(
    r"\b(how\s+(?:do|should|can|to|would|am|is)|procedure|process|workflow|runbook|instruction|"
    r"rule|policy|protocol|steps?|checklist|always|never|remind|remember\s+to)\b|"
    r"怎么|如何|步骤|流程|规则|指令|操作|办法|提醒|记得",
    re.IGNORECASE,
)


def _expected_answer_type(query: str):
    q = query.lower()
    for t, cues in _ANSWER_TYPE_CUES.items():
        for c in cues:
            # ASCII 线索用词边界匹配，避免 "id" 误触发于 "did"/"said"；CJK 用子串匹配
            if c.isascii():
                if re.search(r"\b" + re.escape(c) + r"\b", q):
                    return t
            elif c in q:
                return t
    return None


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


def is_procedural_fact(f: Fact) -> bool:
    pred = f.predicate.lower()
    return pred in _INSTRUCTION_PREDS or any(
        pred.startswith(p + "_") for p in ("wants", "prefers", "remind")
    )


def rank_procedural(query, facts, k):
    q_is_proc = bool(_PROCEDURAL_QUERY_RE.search(query))
    rows: list[tuple[int, float, Fact]] = []
    for f in facts:
        if not is_procedural_fact(f):
            continue
        text = " ".join(
            part for part in (
                f.subject,
                f.predicate.replace("_", " "),
                f.object,
                f.display,
            )
            if part
        )
        exact = overlap_terms(query, text) - _GENERIC_ATTR_TERMS
        object_hits = overlap_terms(query, f.object)
        subject_hits = overlap_terms(query, f.subject)
        if not exact and not q_is_proc:
            continue
        if q_is_proc and not (exact or subject_hits or object_hits):
            continue
        score = len(exact) + (3 if object_hits else 0) + (2 if subject_hits else 0)
        if f.provenance:
            score += 1
        rows.append((score, time_key(f.valid_at), f))
    rows.sort(key=lambda row: (-row[0], -row[1], row[2].text.lower()))
    return [f for _, _, f in rows[:k]]


def chain_terms(f: Fact) -> str:
    pred = f.predicate.replace("_", " ")
    aliases = ""
    if f.predicate.lower() in {"works_at", "employer", "company"}:
        aliases = " work works worked employer employed employment company job"
    elif f.predicate.lower() in {"lives_in", "location", "city", "home"}:
        aliases = " live lives lived location city home"
    return f"{f.subject} {pred} {f.object} {f.text} {aliases}"


def mentions_value(query: str, value: str) -> bool:
    q = query.lower()
    v = value.lower().strip()
    if not v:
        return False
    if v in q:
        return True
    value_terms = set(stems(value))
    return bool(value_terms) and value_terms <= set(stems(query))


def history_answer(query, facts, view_time, is_live):
    if not facts:
        return None
    by_id = {f.id: f for f in facts}
    replacements: dict[str, list[Fact]] = {}
    for f in facts:
        if f.supersedes and f.supersedes in by_id:
            replacements.setdefault(f.supersedes, []).append(f)

    candidates: list[tuple[int, float, Fact, Fact]] = []
    for old, newers in ((by_id[old_id], ns) for old_id, ns in replacements.items()):
        visible_newers = [n for n in newers if (n.valid_at is not None and n.valid_at <= view_time)]
        if not visible_newers:
            continue
        newer = max(visible_newers, key=lambda f: (time_key(f.valid_at), f.created_at, f.id))
        if is_live(old):
            continue
        old_terms = chain_terms(old)
        newer_terms = chain_terms(newer)
        overlap = overlap_terms(query, old_terms) | overlap_terms(query, newer_terms)
        score = len(overlap)
        if mentions_value(query, newer.object):
            score += 6
        if mentions_value(query, old.subject):
            score += 2
        if overlap_terms(query, old.predicate.replace("_", " ")):
            score += 3
        if score <= 0:
            continue
        candidates.append((score, old.valid_at, old, newer))
    if not candidates:
        return None
    _, _, old, newer = max(candidates, key=lambda row: (row[0], row[1], row[2].created_at, row[2].id))
    return SearchResult(query=query, facts=[old, newer], scores=[1.0, 1.0], via="history", _answer=old.object)
