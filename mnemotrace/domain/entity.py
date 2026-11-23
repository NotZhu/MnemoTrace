"""定义按空间识别的普通实体和固定用户。"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Self
from uuid import uuid5

from ._validation import blank_as_none, require_nonblank, require_type
from .errors import InvalidEntity
from .ids import EntityId, SpaceId
from .values import ENTITY_ID_NAMESPACE, normalize_name

_SPACE_USER_NAMESPACE = uuid5(ENTITY_ID_NAMESPACE, "space_user")
_SPACE_USER_NAME = "用户"


class EntityKind(StrEnum):
    """区分按名称识别的实体和空间绑定的用户。"""

    NAMED = "named"
    SPACE_USER = "space_user"


@dataclass(frozen=True)
class Entity:
    """保存空间内的实体身份、名称和可选业务类型。"""

    space_id: SpaceId
    name: str
    type: str | None = None
    kind: EntityKind = field(default=EntityKind.NAMED, kw_only=True)
    name_key: str = field(init=False)
    id: EntityId = field(init=False)

    def __post_init__(self) -> None:
        require_type(self.space_id, SpaceId, "space_id", InvalidEntity)
        require_type(self.name, str, "name", InvalidEntity)
        require_type(self.kind, EntityKind, "kind", InvalidEntity)
        name_key = normalize_name(self.name)
        require_nonblank(name_key, "name", InvalidEntity)
        if self.kind is EntityKind.SPACE_USER and self.name != _SPACE_USER_NAME:
            raise InvalidEntity("空间用户须使用固定显示名")
        entity_type = blank_as_none(self.type, "type", InvalidEntity)
        try:
            if self.kind is EntityKind.SPACE_USER:
                identity = uuid5(_SPACE_USER_NAMESPACE, self.space_id.value)
            else:
                space_namespace = uuid5(ENTITY_ID_NAMESPACE, self.space_id.value)
                identity = uuid5(space_namespace, name_key)
        except UnicodeEncodeError as exc:
            raise InvalidEntity("space_id 和 name 必须能编码为 UTF-8") from exc
        object.__setattr__(self, "type", entity_type)
        object.__setattr__(self, "name_key", name_key)
        object.__setattr__(self, "id", EntityId(str(identity)))

    @classmethod
    def space_user(cls, space_id: SpaceId) -> Self:
        """返回该空间绑定的固定用户实体。

        Args:
            space_id: 用户所属的空间标识。

        Returns:
            使用固定身份及显示名的实体。
        """
        return cls(space_id, _SPACE_USER_NAME, kind=EntityKind.SPACE_USER)

    def with_type(self, new_type: str | None) -> Self:
        """生成同 ID 的新实体快照。

        Args:
            new_type: 新的类型，空白值表示缺省。

        Returns:
            使用新类型的实体。
        """
        return replace(self, type=new_type)
