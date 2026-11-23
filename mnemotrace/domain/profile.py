"""定义空间画像及条目级事实引用。"""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Self

from ._validation import require_nonblank, require_tuple, require_type, timestamp
from .errors import DomainError, InvalidProfile
from .ids import FactId, SpaceId


@dataclass(frozen=True)
class ProfileEntry:
    """保存一条画像文字及支撑它的事实。"""

    text: str
    fact_ids: tuple[FactId, ...]

    def __post_init__(self) -> None:
        require_nonblank(self.text, "text")
        require_tuple(self.fact_ids, FactId, "fact_ids")
        if not self.fact_ids:
            raise DomainError("fact_ids 必须包含支撑该条目的事实")
        object.__setattr__(self, "fact_ids", tuple(dict.fromkeys(self.fact_ids)))


@dataclass(frozen=True)
class Profile:
    """保存空间的只读画像快照。"""

    __hash__ = None  # type: ignore[assignment]

    space_id: SpaceId
    data: Mapping[str, tuple[ProfileEntry, ...]]
    updated_at: float
    source_fact_ids: tuple[FactId, ...] = field(init=False)

    def __post_init__(self) -> None:
        require_type(self.space_id, SpaceId, "space_id", InvalidProfile)
        require_type(self.data, Mapping, "data", InvalidProfile)
        updated_at = timestamp(self.updated_at, "updated_at", InvalidProfile)
        data = dict(self.data)
        for group, entries in data.items():
            require_nonblank(group, "data 的分组键", InvalidProfile)
            require_tuple(entries, ProfileEntry, f"data[{group!r}]", InvalidProfile)
        source_fact_ids = tuple(
            dict.fromkeys(
                fact_id
                for entries in data.values()
                for entry in entries
                for fact_id in entry.fact_ids
            )
        )
        object.__setattr__(self, "data", MappingProxyType(data))
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "source_fact_ids", source_fact_ids)

    def update(self, data: Mapping[str, tuple[ProfileEntry, ...]], at: float) -> Self:
        """替换整个画像并按新条目重建来源。

        Args:
            data: 新快照的分组与条目。
            at: 新快照的更新时间。

        Returns:
            经过校验的新画像快照。

        Raises:
            InvalidProfile: 分组或条目无效，或更新时间违反约束。
        """
        updated = replace(self, data=data, updated_at=at)
        if updated.updated_at < self.updated_at:
            raise InvalidProfile("更新时间不能早于当前画像的 updated_at")
        return updated

    def without_facts(self, unavailable: AbstractSet[FactId]) -> Self | None:
        """撤下依赖失效事实的条目，保留其余条目的原生成时间。

        Args:
            unavailable: 已失效的支撑事实标识。

        Returns:
            清理后的画像。有内容的快照被全部清空时返回 None。
        """
        if unavailable.isdisjoint(self.source_fact_ids):
            return self
        data = {
            group: tuple(
                entry for entry in entries if unavailable.isdisjoint(entry.fact_ids)
            )
            for group, entries in self.data.items()
        }
        return self.update(data, self.updated_at) if any(data.values()) else None
