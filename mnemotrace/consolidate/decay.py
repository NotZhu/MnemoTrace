"""显著度加权的遗忘（MemoryBank 的思路，但做得没那么粗暴；CLAUDE.md Bet E）。

未被强化的记忆随时间损失显著度（乘法衰减）；访问记忆会提升它（间隔重复式强化）。显著度是
检索得分的一项，之后也是分层存储淘汰冷记忆的信号，让热集在规模化时保持小而快。
"""
from __future__ import annotations

import math
from typing import Optional

from ..types import Fact
from ..util import DAY, now

# 免于遗忘的持久 fact（Mem0："偏好免于衰减"；OMEGA 豁免偏好/错误模式）。用户是谁、喜欢什么
# 不会因为最近没被提起就过时——只有偶发的 fact 才会淡出。放在这里（不从 retrieve/ 导入），
# 以避免 consolidate->retrieve 的依赖。
_DURABLE_PREDS = frozenset({
    "likes", "dislikes", "prefers", "avoids", "loves", "hates", "enjoys", "wants", "allergic_to",
    "interested_in", "name", "works_at", "lives_in", "born_in", "married_to", "occupation", "speaks",
})


def is_durable(predicate: str) -> bool:
    p = predicate.lower()
    return p in _DURABLE_PREDS or p.startswith("favorite")


def reinforce(fact: Fact, boost: float, t: Optional[float] = None) -> None:
    """间隔重复式强化：访问记忆会提升其显著度并重置衰减时钟（MemoryBank 的间隔效应）。
    在 fact 被检索进答案上下文时调用。"""
    t = now() if t is None else t
    fact.access_count += 1
    fact.last_access = t
    fact.salience += boost


def decay(fact: Fact, per_day: float, t: Optional[float] = None, exempt_durable: bool = True) -> None:
    """对未强化 fact 做乘法遗忘。持久的身份/偏好 fact 默认豁免——无论多久之前，它们都保持
    显著，所以很久以前说过的一次偏好仍会浮现。"""
    if exempt_durable and is_durable(fact.predicate):
        return
    t = now() if t is None else t
    days = max(0.0, (t - fact.last_access) / DAY)
    fact.salience *= math.exp(-per_day * days)
