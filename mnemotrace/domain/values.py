"""定义断言的值对象和名称归一化规则。"""

from dataclasses import dataclass
from unicodedata import normalize
from uuid import UUID

from ._validation import blank_as_none, require_nonblank, require_type
from .errors import DomainError
from .ids import EntityId

ENTITY_ID_NAMESPACE = UUID("d86e7fa0-8bba-5a12-bb33-91784c719566")
KNOWN_LABELS = frozenset(
    {
        "personal_details",
        "family",
        "professional_details",
        "sports",
        "travel",
        "food",
        "music",
        "health",
        "technology",
        "hobbies",
        "fashion",
        "entertainment",
        "milestones",
        "user_preferences",
        "misc",
    }
)


def normalize_name(name: str) -> str:
    """将名称转为 NFKC、折叠大小写并规整空白。

    Args:
        name: 原始名称或谓词。

    Returns:
        规范化后的名称。
    """
    return " ".join(normalize("NFKC", name).casefold().split())


@dataclass(frozen=True)
class EntityRef:
    """引用由 ID 标识的实体。"""

    entity_id: EntityId

    def __post_init__(self) -> None:
        require_type(self.entity_id, EntityId, "entity_id")


@dataclass(frozen=True)
class LiteralValue:
    """保存事实对象的字面文本和可选种类。"""

    value: str
    kind: str | None = None

    def __post_init__(self) -> None:
        require_nonblank(self.value, "value")
        object.__setattr__(self, "kind", blank_as_none(self.kind, "kind"))


@dataclass(frozen=True)
class Predicate:
    """保存非空的规范谓词。"""

    value: str

    def __post_init__(self) -> None:
        require_type(self.value, str, "value")
        value = normalize_name(self.value)
        require_nonblank(value, "value")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True)
class Label:
    """保存受控分类中的一项标签。"""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or self.value not in KNOWN_LABELS:
            raise DomainError("value 必须是已知标签")
