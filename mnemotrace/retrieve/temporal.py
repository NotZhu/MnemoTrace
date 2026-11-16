"""双时间轴查询助手:as-of 过滤与演化链历史(AGENTS.md 「对象与时间语义」)。"""
from __future__ import annotations
from ..util import name_key, time_key

from typing import Optional

from ..types import Fact


def live_at(facts: list[Fact], as_of: Optional[float]) -> list[Fact]:
    return [f for f in facts if f.is_live(as_of)]


def history(facts: list[Fact], user_id: str, subject: str, predicate: str) -> list[Fact]:
    """读取指定用户、主体和谓词的全部事实历史。

    Args:
        facts: 可查询的事实集合。
        user_id: 事实所属的用户 ID。
        subject: 事实主体。
        predicate: 事实谓词。

    Returns:
        按有效时间从旧到新排列的事实。
    """
    subject, predicate = name_key(subject), name_key(predicate)
    chain = [
        f
        for f in facts
        if f.user_id == user_id
        and name_key(f.subject) == subject
        and name_key(f.predicate) == predicate
    ]
    return sorted(chain, key=lambda f: time_key(f.valid_at))
