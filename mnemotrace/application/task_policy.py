"""定义后台任务共用的领取结果、租约和失败处理策略。"""

from collections.abc import Callable
from datetime import timedelta
from enum import Enum, auto

from ..ports.inference import (
    CompletionRefused,
    CompletionTruncated,
    InferenceError,
    InvalidStructuredOutput,
)

ATTEMPT_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 3
_LEASE_GRACE_SECONDS = 30
_INITIAL_RETRY_SECONDS = 5
LEASE_DURATION = timedelta(seconds=ATTEMPT_TIMEOUT_SECONDS + _LEASE_GRACE_SECONDS)


class ClaimOutcome(Enum):
    """区分已结算一项过期任务与当前没有可领取任务。"""

    SETTLED = auto()
    EMPTY = auto()


class LeaseLost(RuntimeError):
    """本次尝试已不能提交任务结果。"""


def classify_worker_failure(
    error: Exception, transient: Callable[[Exception], bool]
) -> tuple[str, bool]:
    """为通用任务异常返回稳定错误码及是否允许重试。"""
    if isinstance(error, TimeoutError):
        return "operation_timeout", True
    if isinstance(
        error, (InvalidStructuredOutput, CompletionRefused, CompletionTruncated)
    ):
        return "invalid_model_output", False
    if isinstance(error, InferenceError):
        return "inference_failed", transient(error)
    if transient(error):
        return "database_unavailable", True
    return "internal_error", False


def retry_delay_for(attempt: int, *, retry: bool) -> timedelta | None:
    """返回下一次尝试前的退避，终态任务返回 None。"""
    if not retry or attempt >= MAX_ATTEMPTS:
        return None
    return timedelta(seconds=_INITIAL_RETRY_SECONDS * 2 ** (attempt - 1))
