"""定义领域构造与状态变更的错误。"""


class DomainError(ValueError):
    """表示输入违反领域约束。"""


class InvalidSpace(DomainError):
    """表示空间字段无效。"""


class InvalidMessage(DomainError):
    """表示消息字段无效。"""


class InvalidEntity(DomainError):
    """表示实体字段无效。"""


class InvalidFact(DomainError):
    """表示事实字段或截止操作无效。"""


class InvalidProfile(DomainError):
    """表示画像字段或更新时间无效。"""
