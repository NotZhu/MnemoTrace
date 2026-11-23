"""定义单条事实的显著度及其强化规则。"""

from dataclasses import dataclass, replace
from math import exp2
from typing import Self

from ._validation import optional_timestamp, timestamp
from .errors import DomainError

_BASELINE = 1.0
_HALF_LIFE_SECONDS = 35 * 86_400
_ACCESS_BOOST = 0.5
_MAX_SALIENCE = 3.0


@dataclass(frozen=True)
class FactSalience:
    """保存独立于事实快照的访问强化状态。

    Attributes:
        salience: 上次强化后的显著度，取值为 1.0 至 3.0。
        last_access: 上次强化的 UTC epoch 秒，从未强化时为 None。
    """

    salience: float = _BASELINE
    last_access: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.salience, bool)
            or not isinstance(self.salience, (int, float))
            or not _BASELINE <= self.salience <= _MAX_SALIENCE
        ):
            raise DomainError(f"salience 必须介于 {_BASELINE} 和 {_MAX_SALIENCE} 之间")
        last_access = optional_timestamp(self.last_access, "last_access", DomainError)
        if last_access is None and self.salience != _BASELINE:
            raise DomainError("显著度加成必须有 last_access")
        object.__setattr__(self, "salience", float(self.salience))
        object.__setattr__(self, "last_access", last_access)

    def effective_at(self, reference_time: float) -> float:
        """计算参考时刻的有效显著度，加成每 35 天减半。

        Args:
            reference_time: 本次查询统一使用的有限 UTC epoch 秒。

        Returns:
            至少为 1.0 的显著度。参考时刻早于上次强化时，间隔按零计算。
        """
        if self.last_access is None:
            return _BASELINE
        elapsed = max(0.0, reference_time - self.last_access)
        return _BASELINE + (self.salience - _BASELINE) * exp2(
            -elapsed / _HALF_LIFE_SECONDS
        )

    def normalized_at(self, reference_time: float) -> float:
        """将有效显著度的加成映射到 0 至 1。

        Args:
            reference_time: 本次查询统一使用的有限 UTC epoch 秒。

        Returns:
            基线对应 0，显著度上限对应 1。
        """
        return (self.effective_at(reference_time) - _BASELINE) / (
            _MAX_SALIENCE - _BASELINE
        )

    def reinforce(self, at: float) -> Self:
        """将有效显著度提高 0.5，封顶 3.0。

        Args:
            at: 此次强化的 UTC epoch 秒。

        Returns:
            强化后的状态。时钟回退时保留已有的 last_access。

        Raises:
            DomainError: 强化时刻不是有限的 UTC epoch 秒。
        """
        at = timestamp(at, "at", DomainError)
        last_access = at if self.last_access is None else max(at, self.last_access)
        salience = min(_MAX_SALIENCE, self.effective_at(last_access) + _ACCESS_BOOST)
        return replace(self, salience=salience, last_access=last_access)
