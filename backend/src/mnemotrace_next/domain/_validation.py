"""提供领域构造共享的基础校验。"""

from math import isfinite

from .errors import DomainError


def require_type(
    value: object,
    expected: type[object] | tuple[type[object], ...],
    field: str,
    error: type[DomainError] = DomainError,
) -> None:
    """校验字段的运行时类型。

    Args:
        value: 待校验的字段值。
        expected: 允许的类型或类型元组。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Raises:
        DomainError: 值的类型不符，使用 error 指定的错误类。
    """
    if not isinstance(value, expected):
        raise error(f"{field} 的类型无效")


def require_nonblank(
    value: object, field: str, error: type[DomainError] = DomainError
) -> None:
    """校验文本去除首尾空白后仍有内容。

    Args:
        value: 待校验的字段值。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Raises:
        DomainError: 值不是字符串或只含空白，使用 error 指定的错误类。
    """
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} 必须是非空字符串")


def require_tuple(
    value: object,
    item_type: type[object],
    field: str,
    error: type[DomainError] = DomainError,
) -> None:
    """校验元组及其元素类型。

    Args:
        value: 待校验的字段值。
        item_type: 元组允许的元素类型。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Raises:
        DomainError: 值不是元组或有元素类型不符，使用 error 指定的错误类。
    """
    if not isinstance(value, tuple) or any(
        not isinstance(item, item_type) for item in value
    ):
        raise error(f"{field} 必须是 {item_type.__name__} 元组")


def blank_as_none(
    value: str | None, field: str, error: type[DomainError] = DomainError
) -> str | None:
    """将可选文本的空白值视为缺省。

    Args:
        value: 可选文本。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Returns:
        有内容时保留原文，空白或缺省时为 None。

    Raises:
        DomainError: 有值时其类型必须为字符串，失败时使用 error 指定的错误类。
    """
    if value is None:
        return None
    require_type(value, str, field, error)
    return value if value.strip() else None


def timestamp(value: object, field: str, error: type[DomainError]) -> float:
    """将数值时刻转换为有限的 UTC epoch 秒。

    Args:
        value: 待校验的时刻值。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Returns:
        表示同一时刻的有限浮点秒数。

    Raises:
        DomainError: 值不是有限数值时刻，使用 error 指定的错误类。
    """
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
    """校验可缺省的时刻。

    Args:
        value: 待校验或缺省的时刻值。
        field: 用于错误信息的字段名。
        error: 校验失败时使用的领域错误类。

    Returns:
        有值时返回有限的 UTC epoch 秒，缺省时为 None。

    Raises:
        DomainError: 已传入的值违反时刻约束，使用 error 指定的错误类。
    """
    return None if value is None else timestamp(value, field, error)
