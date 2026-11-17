"""定义可区分类型的领域标识。"""

from dataclasses import dataclass

from ._validation import require_nonblank


@dataclass(frozen=True)
class _Id:
    """保存标识原值并校验其非空。"""

    value: str

    def __post_init__(self) -> None:
        require_nonblank(self.value, "value")


@dataclass(frozen=True)
class SpaceId(_Id):
    """标识一个空间。"""


@dataclass(frozen=True)
class SessionId(_Id):
    """标识空间内的会话。"""


@dataclass(frozen=True)
class MessageId(_Id):
    """标识空间内唯一的内部原文。"""


@dataclass(frozen=True)
class FactId(_Id):
    """标识一条事实。"""


@dataclass(frozen=True)
class EntityId(_Id):
    """标识一个实体。"""


@dataclass(frozen=True)
class DigestId(_Id):
    """标识一份摘要。"""
