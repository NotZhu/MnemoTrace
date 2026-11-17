"""提供领域构造共享的基础校验。"""

from math import isfinite

from .errors import DomainError


def require_type(
    value: object,
    expected: type[object] | tuple[type[object], ...],
    field: str,
    error: type[DomainError] = DomainError,
) -> None:
    """校验字段的实际类型。"""
    if not isinstance(value, expected):
        raise error(f"{field} 的类型无效")


def require_nonblank(
    value: object, field: str, error: type[DomainError] = DomainError
) -> None:
    """校验文本去除首尾空白后仍有内容。"""
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} 必须是非空字符串")


def require_tuple(
    value: object,
    item_type: type[object],
    field: str,
    error: type[DomainError] = DomainError,
) -> None:
    """校验元组及其元素类型。"""
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise error(f"{field} 必须是 {item_type.__name__} 元组")


def blank_as_none(
    value: str | None, field: str, error: type[DomainError] = DomainError
) -> str | None:
    """将可选文本的空白值视为缺省。"""
    if value is None:
        return None
    require_type(value, str, field, error)
    return value if value.strip() else None


def timestamp(value: object, field: str, error: type[DomainError]) -> float:
    """将数值时刻转换为有限的 UTC epoch 秒。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{field} 必须是有限的 UTC epoch 秒数")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise error(f"{field} 必须是有限的 UTC epoch 秒数") from exc
    if not isfinite(seconds):
        raise error(f"{field} 必须是有限的 UTC epoch 秒数")
    return seconds


def optional_timestamp(
    value: object, field: str, error: type[DomainError]
) -> float | None:
    """保留缺省时刻，校验其余时刻。"""
    return None if value is None else timestamp(value, field, error)
