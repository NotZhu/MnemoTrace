"""Shared preference postprocessing; does not generate facts from raw conversations."""
from __future__ import annotations

import re

from ..types import Fact

EXPLICIT_PREFERENCE_PREDS = frozenset({"likes", "loves", "enjoys", "prefers", "dislikes", "hates", "avoids"})

# 从捕获的 object 尾部剥掉的赘词（"Moonshot AI too" -> "Moonshot AI"）
_FILLER = {"too", "now", "also", "currently", "anymore", "though", "well", "as",
           "right", "these", "days", "nowadays", "actually"}
_WEAK_PREFERENCE_OBJECTS = {
    "it", "this", "that", "these", "those", "them", "thing", "things", "stuff", "something",
    "anything", "everything", "one", "ones", "use", "process", "experience", "journey",
    "idea", "ideas", "suggestion", "suggestions", "tip", "tips", "option", "options",
}
_WEAK_DEICTIC_HEADS = ("these", "those", "this", "that")
_WEAK_DEICTIC_NOUNS = {"idea", "ideas", "suggestion", "suggestions", "tip", "tips", "option", "options"}
_PREFERENCE_OBJECT_PREFIXES = (
    r"(?:i\s+)?(?:really\s+)?(?:like|love|enjoy|prefer)s?\s+(?:the\s+)?sound of\s+",
    r"(?:the\s+)?sound of\s+",
    r"(?:i\s+)?(?:really\s+)?(?:like|love|enjoy|prefer)s?\s+(?:the\s+)?idea of\s+",
    r"(?:the\s+)?idea of\s+",
    r"(?:my|your|our|their)\s+idea of\s+",
    r"(?:the\s+)?concept of\s+",
)

_CLAUSE_SPLIT = re.compile(r"\n+|[.!?。！？]+(?:\s+|$)|,|;| and | but |—|--|\bthen\b", re.I)
_I_AM = r"i\s*(?:am|'m|m)?"


def _clean_obj(s: str) -> str:
    s = s.strip().strip(".!?;:, ").strip()
    toks = s.split()
    while toks and toks[-1].lower() in _FILLER:
        toks.pop()
    if toks and toks[0].lower() == "the":
        toks = toks[1:]
    return " ".join(toks)


def _clean_preference_obj(s: str) -> str:
    text = _clean_obj(s)
    text = re.split(r"\b(?:over|rather than|instead of|because|when|while)\b", text, 1, flags=re.I)[0]
    text = re.sub(r"^(?:to|that)\s+", "", text.strip(), flags=re.I)
    return _clean_obj(text)


def normalize_preference_object(obj: str) -> str:
    text = _clean_obj(obj)
    for prefix in _PREFERENCE_OBJECT_PREFIXES:
        new = re.sub(rf"^{prefix}", "", text, flags=re.I).strip()
        if new != text:
            text = _clean_obj(new)
            break
    return text


def preference_topic_object(predicate: str, obj: str, normalize: bool) -> str:
    """主题使用整合后将落库的取值；不提前改变事实，以保留既有过滤顺序。"""
    if normalize and predicate.lower() in EXPLICIT_PREFERENCE_PREDS:
        return normalize_preference_object(obj)
    return obj


def is_specific_preference_object(obj: str) -> bool:
    text = re.sub(r"\s+", " ", obj.strip().strip("\"'`*_ ")).lower()
    if not text:
        return False
    if text in _WEAK_PREFERENCE_OBJECTS:
        return False
    if text.startswith(("you mentioned", "what you", "how they", "how it", "how this", "how that")):
        return False
    toks = text.split()
    if toks and toks[0] in _WEAK_DEICTIC_HEADS and any(t in _WEAK_DEICTIC_NOUNS for t in toks[1:]):
        return False
    return True


def preference_reversal_objects(text: str) -> set[str]:
    """高置信的偏好更新，其自然含义是"我不再喜欢这个了"。"""
    out: set[str] = set()
    for raw in _CLAUSE_SPLIT.split(text):
        clause = raw.strip()
        if not clause:
            continue
        patterns = (
            r"\b(?:i|we)\s+no\s+longer\s+(?:like|love|enjoy)s?\s+(.+)",
            rf"\b{_I_AM}\s+no\s+longer\s+(?:into|fond of|(?:an? )?fan of)\s+(.+)",
            r"\b(?:i|we)\s+stopped\s+(?:liking|loving|enjoying)\s+(.+)",
        )
        for pattern in patterns:
            m = re.search(pattern, clause, re.I)
            if not m:
                continue
            obj = _clean_preference_obj(m.group(1))
            if obj:
                out.add(obj)
            break
    return out


def is_preference_reversal_fact(fact: Fact, source_text: str) -> bool:
    if fact.predicate.lower() not in {"dislikes", "hates", "avoids"}:
        return False
    obj = normalize_preference_object(fact.object)
    return any(normalize_preference_object(candidate).lower() == obj.lower()
               for candidate in preference_reversal_objects(source_text))
