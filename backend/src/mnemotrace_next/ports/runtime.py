"""定义时钟和领域标识生成端口。"""

from typing import Protocol

from ..domain.ids import DigestId, FactId, MessageId, SessionId, SpaceId


class Clock(Protocol):
    """提供当前时刻。"""

    def now(self) -> float:
        """读取当前时刻。

        Returns:
            UTC epoch 秒数。
        """
        ...


class IdGenerator(Protocol):
    """生成由调用方传入领域构造器的标识。"""

    def new_space_id(self) -> SpaceId:
        """为新空间分配身份。

        Returns:
            新空间的标识。
        """
        ...

    def new_session_id(self) -> SessionId:
        """为新对话分配身份。

        Returns:
            从未签发的对话标识。已擦除的标识也不可复用。
        """
        ...

    def new_message_id(self) -> MessageId:
        """为新原文分配内部身份。

        Returns:
            新原文的标识。
        """
        ...

    def new_fact_id(self) -> FactId:
        """为新事实分配身份。

        Returns:
            新事实的标识。
        """
        ...

    def new_digest_id(self) -> DigestId:
        """为新摘要分配身份。

        Returns:
            新摘要的标识。
        """
        ...
