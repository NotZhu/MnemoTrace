"""定义断言的值对象和文本归一化规则。"""

import re
from dataclasses import dataclass
from hashlib import sha256
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


def normalize_text(text: str) -> str:
    """将正文转为 NFKC，并去除首尾及折叠连续空白。

    Args:
        text: 原始正文。

    Returns:
        保留大小写的规范正文。
    """
    return " ".join(normalize("NFKC", text).split())


def normalize_name(name: str) -> str:
    """将名称转为 NFKC、折叠大小写并规整空白。

    Args:
        name: 原始名称或谓词。

    Returns:
        规范化后的名称。
    """
    return " ".join(normalize("NFKC", name).casefold().split())


def content_hash(text: str) -> str:
    """计算规范正文的 SHA-256 指纹。

    Args:
        text: 原始正文。

    Returns:
        UTF-8 正文哈希的小写十六进制字符串。

    Raises:
        UnicodeEncodeError: 规范化后的正文无法编码为 UTF-8。
    """
    return sha256(normalize_text(text).encode("utf-8")).hexdigest()


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
class ContentHash:
    """保存 64 位小写十六进制正文指纹。"""

    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.value) is None
        ):
            raise DomainError("value 必须是 64 位小写十六进制")


@dataclass(frozen=True)
class Label:
    """保存受控分类中的一项标签。"""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or self.value not in KNOWN_LABELS:
            raise DomainError("value 必须是已知标签")
