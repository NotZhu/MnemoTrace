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

    async def claim(self) -> ClaimedOperation | None: ...

    async def complete(
        self, job: ClaimedOperation, writer: SpaceSession, result: RememberResult
    ) -> None: ...

    async def fail(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        retry_delay: timedelta | None,
    ) -> None: ...

    async def is_running(self, job: ClaimedOperation) -> bool: ...


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
            if await self._queue.is_running(job):
                await self._fail(job, "commit_outcome_unknown", "publish", retry=True)
        except Exception as exc:  # noqa: BLE001
            # 单个任务的未知异常应在任务内终结，避免阻塞同会话后续批次。
            if isinstance(
                exc, (SessionUnavailable, SpaceUnavailable, RememberInvalidated)
            ):
                code, retry = "write_invalidated", False
            elif isinstance(exc, InvalidMessage):
                code, retry = "invalid_message", False
            elif isinstance(exc, RememberRetryRequired):
                code, retry = "remember_conflict", True
            elif isinstance(exc, TimeoutError):
                code, retry = "operation_timeout", True
            elif isinstance(
                exc, (InvalidStructuredOutput, CompletionRefused, CompletionTruncated)
            ):
                code, retry = "invalid_model_output", False
            elif isinstance(exc, InferenceError):
                code, retry = "inference_failed", self._transient(exc)
            elif self._transient(exc):
                code, retry = "database_unavailable", True
            else:
                code, retry = "internal_error", False
            _LOG.warning(
                "任务执行失败 operation_id=%s code=%s stage=%s type=%s",
                job.operation_id,
                code,
                stage,
                type(exc).__name__,
            )
            await self._fail(job, code, stage, retry=retry)
        return True

    async def _fail(
        self,
        job: ClaimedOperation,
        code: str,
        stage: RememberStage | None,
        *,
        retry: bool,
    ) -> None:
        delay = (
            timedelta(seconds=5 * 2 ** (job.attempt - 1))
            if retry and job.attempt < MAX_ATTEMPTS
            else None
        )
        await self._queue.fail(job, code, stage, delay)
