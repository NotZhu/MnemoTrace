"""定义具有单一所有者的空间。"""

from dataclasses import dataclass

from ._validation import require_nonblank, require_type, timestamp
from .errors import InvalidSpace
from .ids import SpaceId


@dataclass(frozen=True)
class Space:
    """标识一个所有者名下的空间及其创建时刻。"""

    id: SpaceId
    owner_id: str
    name: str
    created_at: float
    memory_decay: bool = False
    profile_auto_refresh: bool = False

    def __post_init__(self) -> None:
        require_type(self.id, SpaceId, "id", InvalidSpace)
        require_nonblank(self.owner_id, "owner_id", InvalidSpace)
        require_nonblank(self.name, "name", InvalidSpace)
        require_type(self.memory_decay, bool, "memory_decay", InvalidSpace)
        require_type(
            self.profile_auto_refresh, bool, "profile_auto_refresh", InvalidSpace
        )
        created_at = timestamp(self.created_at, "created_at", InvalidSpace)
        object.__setattr__(self, "created_at", created_at)
