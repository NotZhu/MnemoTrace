"""定义固定消息组的摘要快照。"""

from dataclasses import dataclass, replace
from typing import Self

from ._validation import require_nonblank, require_tuple, require_type, timestamp
from .errors import InvalidDigest
from .ids import DigestId, MessageId, SessionId, SpaceId
from .values import MAX_DIGEST_CHARS


@dataclass(frozen=True)
class Digest:
    """保存固定的有序消息组及其最新摘要。"""

    id: DigestId
    space_id: SpaceId
    session_id: SessionId
    text: str
    source_message_ids: tuple[MessageId, ...]
    created_at: float
    updated_at: float

    def __post_init__(self) -> None:
        require_type(self.id, DigestId, "id", InvalidDigest)
        require_type(self.space_id, SpaceId, "space_id", InvalidDigest)
        require_type(self.session_id, SessionId, "session_id", InvalidDigest)
        require_nonblank(self.text, "text", InvalidDigest)
        require_tuple(
            self.source_message_ids, MessageId, "source_message_ids", InvalidDigest
        )
        if len(self.text) > MAX_DIGEST_CHARS:
            raise InvalidDigest(f"text 最多包含 {MAX_DIGEST_CHARS} 个字符")
        if not self.source_message_ids:
            raise InvalidDigest("source_message_ids 必须包含来源消息")
        created_at = timestamp(self.created_at, "created_at", InvalidDigest)
        updated_at = timestamp(self.updated_at, "updated_at", InvalidDigest)
        if updated_at < created_at:
            raise InvalidDigest("updated_at 必须晚于或等于 created_at")
        source_ids = tuple(dict.fromkeys(self.source_message_ids))
        object.__setattr__(self, "source_message_ids", source_ids)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    def update(self, text: str, at: float) -> Self:
        """替换同一消息组的摘要正文。

        Args:
            text: 新的摘要正文。
            at: 新快照的更新时间。

        Returns:
            经过校验的新摘要快照。

        Raises:
            InvalidDigest: 正文无效或更新时间违反约束。
        """
        updated = replace(self, text=text, updated_at=at)
        if updated.updated_at < self.updated_at:
            raise InvalidDigest("更新时间不能早于当前摘要的 updated_at")
        return updated
