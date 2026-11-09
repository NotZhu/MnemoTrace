"""显著度加权的遗忘（MemoryBank 的思路，但做得没那么粗暴；AGENTS.md Bet E）。

未被强化的记忆随时间损失显著度（乘法衰减）；访问记忆会提升它（间隔重复式强化）。显著度是
检索得分的一项，之后也是分层存储淘汰冷记忆的信号，让热集在规模化时保持小而快。
"""
from __future__ import annotations

import math
from typing import Optional

from ..types import Fact
from ..util import DAY, now

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
    if exempt_durable and fact.retention == "durable":
        return
    t = now() if t is None else t
    days = max(0.0, (t - fact.last_access) / DAY)
    fact.salience *= math.exp(-per_day * days)
