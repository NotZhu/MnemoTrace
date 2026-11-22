"""为在线用例签发领域标识。"""

from time import time
from uuid import uuid4

from ..domain.ids import FactId, MessageId, SessionId, SpaceId


class SystemClock:
    """使用系统时钟提供 UTC epoch 秒。"""

    def now(self) -> float:
        return time()


class UuidGenerator:
    """使用随机 UUID 分配部署范围内的领域标识。"""

    def new_space_id(self) -> SpaceId:
        return SpaceId(uuid4().hex)

    def new_session_id(self) -> SessionId:
        return SessionId(uuid4().hex)

    def new_message_id(self) -> MessageId:
        return MessageId(uuid4().hex)

    def new_fact_id(self) -> FactId:
        return FactId(uuid4().hex)
