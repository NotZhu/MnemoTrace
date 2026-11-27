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


class ProfileGroup(StrEnum):
    """画像条目和支持该组的事实所使用的固定分组。"""

    BASIC = "basic"
    COMMUNICATION_PREFERENCES = "communication_preferences"
    PREFERENCES = "preferences"
    CONSTRAINTS = "constraints"
