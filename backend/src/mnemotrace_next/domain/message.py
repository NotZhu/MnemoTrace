"""定义不可变的会话原文记录。"""

from dataclasses import dataclass

from ._validation import optional_timestamp, require_nonblank, require_type, timestamp
from .enums import Role
from .errors import InvalidMessage
from .ids import MessageId, SessionId, SpaceId


@dataclass(frozen=True)
class Message:
    """保存单条原文及其事件和录入时刻。"""

    id: MessageId
    space_id: SpaceId
    session_id: SessionId
    role: Role
    content: str
    event_time: float | None
    recorded_at: float

    def __post_init__(self) -> None:
        require_type(self.id, MessageId, "id", InvalidMessage)
        require_type(self.space_id, SpaceId, "space_id", InvalidMessage)
        require_type(self.session_id, SessionId, "session_id", InvalidMessage)
        require_type(self.role, Role, "role", InvalidMessage)
        require_nonblank(self.content, "content", InvalidMessage)
        event_time = optional_timestamp(self.event_time, "event_time", InvalidMessage)
        recorded_at = timestamp(self.recorded_at, "recorded_at", InvalidMessage)
        object.__setattr__(self, "event_time", event_time)
        object.__setattr__(self, "recorded_at", recorded_at)
