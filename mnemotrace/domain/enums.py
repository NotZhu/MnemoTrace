"""定义消息角色与事实来源。"""

from enum import StrEnum


class Role(StrEnum):
    """消息的发言角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class Source(StrEnum):
    """事实的创建来源。"""

    EXTRACTED = "extracted"
    MANUAL = "manual"
