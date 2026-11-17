"""定义时钟和领域标识生成端口。"""

from typing import Protocol

from ..domain.ids import DigestId, FactId, MessageId, SessionId, SpaceId


class Clock(Protocol):
    """提供当前时刻。"""

    def now(self) -> float:
        """返回当前 UTC epoch 秒数。"""
        ...


class IdGenerator(Protocol):
    """生成由调用方传入领域构造器的标识。"""

    def new_space_id(self) -> SpaceId:
        """生成新的空间标识。"""
        ...

    def new_session_id(self) -> SessionId:
        """生成新的对话标识。"""
        ...

    def new_message_id(self) -> MessageId:
        """生成新的内部原文标识。"""
        ...

    def new_fact_id(self) -> FactId:
        """生成新的事实标识。"""
        ...

    def new_digest_id(self) -> DigestId:
        """生成新的摘要标识。"""
        ...
