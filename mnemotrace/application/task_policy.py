"""定义后台任务共用的执行时限、租约和重试预算。"""

from datetime import timedelta

ATTEMPT_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 3
_LEASE_GRACE_SECONDS = 30
_INITIAL_RETRY_SECONDS = 5
LEASE_DURATION = timedelta(seconds=ATTEMPT_TIMEOUT_SECONDS + _LEASE_GRACE_SECONDS)


def retry_delay_for(attempt: int, *, retry: bool) -> timedelta | None:
    """返回下一次尝试前的退避，终态任务返回 None。"""
    if not retry or attempt >= MAX_ATTEMPTS:
        return None
    return timedelta(seconds=_INITIAL_RETRY_SECONDS * 2 ** (attempt - 1))
