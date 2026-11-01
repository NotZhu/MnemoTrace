"""双时间轴查询助手:as-of 过滤与演化链历史(AGENTS.md 「对象与时间语义」)。"""
from __future__ import annotations
from ..util import time_key

from typing import Optional

from ..types import Fact


def live_at(facts: list[Fact], as_of: Optional[float]) -> list[Fact]:
    return [f for f in facts if f.is_live(as_of)]


def history(facts: list[Fact], user_id: str, subject: str, predicate: str) -> list[Fact]:
    """一个槽位随时间的完整演化(live + 已被取代的),按时间从旧到新。"""
    chain = [
        f
        for f in facts
        if f.user_id == user_id
        and f.subject.lower() == subject.lower()
        and f.predicate == predicate
    ]
    return sorted(chain, key=lambda f: time_key(f.valid_at))
