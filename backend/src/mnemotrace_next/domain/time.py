"""转换领域模型使用的 UTC epoch 时间。"""

from datetime import UTC, datetime, timedelta

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_datetime(seconds: float) -> datetime:
    """将有限的 epoch 秒数转换为 UTC 日历时刻，兼容 Windows 上的负时间戳。

    Args:
        seconds: 已校验的 UTC epoch 秒数。

    Returns:
        同一时刻的 UTC 日期时间。

    Raises:
        OverflowError: 时刻超出 datetime 的日历范围。
    """
    return _EPOCH + timedelta(seconds=seconds)
