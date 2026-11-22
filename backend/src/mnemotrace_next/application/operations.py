"""定义持久写入任务和单次 worker 执行流程。"""

import logging
from asyncio import timeout
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Literal, Protocol

from ..domain.errors import InvalidMessage
from ..domain.ids import SessionId, SpaceId
from ..ports.inference import (
    CompletionRefused,
    CompletionTruncated,
    InferenceError,
    InvalidStructuredOutput,
)
from ..ports.storage import (
    CommitOutcomeUnknown,
    SessionUnavailable,
    SpaceSession,
    SpaceUnavailable,
)
from .remember import (
    MessageInput,
    Remember,
    RememberInvalidated,
    RememberOperation,
    RememberResult,
    RememberRetryRequired,
    RememberStage,
)

type OperationStatus = Literal["queued", "running", "succeeded", "failed"]
ATTEMPT_TIMEOUT = 180
LEASE_SECONDS = ATTEMPT_TIMEOUT + 30
MAX_ATTEMPTS = 3
_LOG = logging.getLogger(__name__)


def retry_delay_for(attempt: int, *, retry: bool) -> timedelta | None:
    """按共享的 worker 重试预算计算退避时间。"""
    return (
        timedelta(seconds=5 * 2 ** (attempt - 1))
        if retry and attempt < MAX_ATTEMPTS
        else None
    )


class IdempotencyConflict(ValueError):
    """幂等键已用于同一空间的另一批写入。"""


class QueueFull(RuntimeError):
    """空间的待处理任务达到上限。"""


class WriteModelsUnavailable(RuntimeError):
    """写入接收当前缺少配置完成的模型。"""


class OperationNotFound(LookupError):
    """指定的任务在绑定空间中不可用。"""


class LeaseLost(RuntimeError):
    """本次尝试已不能提交任务结果。"""


@dataclass(frozen=True)
class Operation:
    """对调用方公开的记忆写入状态。"""

    operation_id: str
    session_id: SessionId
    status: OperationStatus
    created_at: datetime
    updated_at: datetime
    next_retry_at: datetime | None
    message_ids: tuple[str, ...] | None
    fact_change_count: int | None
    error_code: str | None
    error_stage: RememberStage | None


@dataclass(frozen=True)
class ClaimedOperation:
    """worker 当前持有的任务和一次性租约。"""

    operation_id: str
    space_id: SpaceId
    owner_id: str
    session_id: SessionId
    messages: tuple[MessageInput, ...]
    received_at: float
    attempt: int
    lease_token: str


class OperationQueue(Protocol):
    """执行端使用的队列操作。"""

    async def claim(self) -> ClaimedOperation | None:
        """领取可执行的队首任务。

        Returns:
            领取到的任务。没有候选任务或只结算过期任务时返回 None。
        """
        ...

    async def complete(
        self, job: ClaimedOperation, writer: SpaceSession, result: RememberResult
    ) -> None: ...

    async def record_attempt_failure(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        retry_delay: timedelta | None,
    ) -> None:
        """在有效租约下安排重试或结束失败的任务。"""
        ...

    async def has_active_lease(self, job: ClaimedOperation) -> bool:
        """确认本次领取对应的租约仍然有效。"""
        ...


class OperationRunner:
    """领取一批任务，并将其发布或转入下一状态。"""

    def __init__(
        self,
        queue: OperationQueue,
        remember_for: Callable[[ClaimedOperation], Remember],
        transient: Callable[[Exception], bool],
    ) -> None:
        self._queue = queue
        self._remember_for = remember_for
        self._transient = transient

    async def run_once(self) -> bool:
        """处理一个可执行任务，并报告本次是否领到了工作。"""
        job = await self._queue.claim()
        if job is None:
            return False
        stage: RememberStage | None = None

        def enter_stage(value: RememberStage) -> None:
            nonlocal stage
            stage = value

        try:
            async with timeout(ATTEMPT_TIMEOUT):
                await self._remember_for(job).remember(
                    job.messages,
                    session_id=job.session_id,
                    operation=RememberOperation(
                        job.received_at,
                        enter_stage,
                        partial(self._queue.complete, job),
                    ),
                )
        except LeaseLost:
            _LOG.info("任务租约失效 operation_id=%s", job.operation_id)
        except CommitOutcomeUnknown:
            # 先从队列重新读取，状态有变化时交给已提交的结果或新的租约。
            if await self._queue.has_active_lease(job):
                await self._record_attempt_failure(
                    job, "commit_outcome_unknown", "publish", retry=True
                )
        except Exception as exc:  # noqa: BLE001
            # 单个任务的未知异常应在任务内终结，避免阻塞同会话后续批次。
            code, retry = self._classify_failure(exc)
            _LOG.warning(
                "任务执行失败 operation_id=%s code=%s stage=%s type=%s",
                job.operation_id,
                code,
                stage,
                type(exc).__name__,
            )
            await self._record_attempt_failure(job, code, stage, retry=retry)
        return True

    def _classify_failure(self, error: Exception) -> tuple[str, bool]:
        if isinstance(
            error, (SessionUnavailable, SpaceUnavailable, RememberInvalidated)
        ):
            return "write_invalidated", False
        if isinstance(error, InvalidMessage):
            return "invalid_message", False
        if isinstance(error, RememberRetryRequired):
            return "remember_conflict", True
        if isinstance(error, TimeoutError):
            return "operation_timeout", True
        if isinstance(
            error, (InvalidStructuredOutput, CompletionRefused, CompletionTruncated)
        ):
            return "invalid_model_output", False
        if isinstance(error, InferenceError):
            return "inference_failed", self._transient(error)
        if self._transient(error):
            return "database_unavailable", True
        return "internal_error", False

    async def _record_attempt_failure(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        *,
        retry: bool,
    ) -> None:
        await self._queue.record_attempt_failure(
            job, code, stage, retry_delay_for(job.attempt, retry=retry)
        )
