"""定义按空间与规范名称识别的实体。"""

from dataclasses import dataclass, field, replace
from typing import Self
from uuid import uuid5

from ._validation import blank_as_none, require_nonblank, require_type
from .errors import InvalidEntity
from .ids import EntityId, SpaceId
from .values import ENTITY_ID_NAMESPACE, normalize_name


@dataclass(frozen=True)
class Entity:
    """以空间内的规范名称确定实体身份。"""

    space_id: SpaceId
    name: str
    type: str | None = None
    name_key: str = field(init=False)
    id: EntityId = field(init=False)

    def __post_init__(self) -> None:
        require_type(self.space_id, SpaceId, "space_id", InvalidEntity)
        require_type(self.name, str, "name", InvalidEntity)
        name_key = normalize_name(self.name)
        require_nonblank(name_key, "name", InvalidEntity)
        entity_type = blank_as_none(self.type, "type", InvalidEntity)
        try:
            space_namespace = uuid5(ENTITY_ID_NAMESPACE, self.space_id.value)
            entity_id = EntityId(str(uuid5(space_namespace, name_key)))
        except UnicodeEncodeError as exc:
            raise InvalidEntity("space_id 和 name 必须能编码为 UTF-8") from exc
        object.__setattr__(self, "type", entity_type)
        object.__setattr__(self, "name_key", name_key)
        object.__setattr__(self, "id", entity_id)

    def with_type(self, new_type: str | None) -> Self:
        """生成同 ID 的新实体快照。

        Args:
            new_type: 新的类型，空白值表示缺省。

        Returns:
            使用新类型的实体。
        """
        return replace(self, type=new_type)
