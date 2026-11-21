"""接收会话记忆批次并公开持久写入的进度。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from ...adapters.postgres.operations import PostgresOperations
from ...application.extraction import message_reference_time
from ...application.operations import (
    IdempotencyConflict,
    Operation,
    OperationNotFound,
    OperationStatus,
    QueueFull,
    WriteModelsUnavailable,
)
from ...application.remember import MessageInput, RememberStage
from ...domain.enums import Role
from ...domain.ids import SessionId, SpaceId
from ...ports.storage import SessionUnavailable
from ..auth import OwnerDependency, PoolDependency
from ..errors import problem_responses
from .resource_ids import encode_resource_id
from .schemas import (
    CREATED_LOCATION,
    RFC3339,
    IdempotencyKey,
    NonblankText,
    ResourceId,
    require_json,
)

router = APIRouter(
    prefix="/spaces/{space_id}",
    tags=["operations"],
    responses=problem_responses(401, 404, 422, 500, 503),
)


def _event_time(value: datetime) -> datetime:
    if message_reference_time(value.timestamp(), timezone=UTC) is None:
        raise ValueError("消息发言时刻超出支持的日历范围")
    return value


class RememberMessage(BaseModel):
    """一条待抽取的原文。"""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: NonblankText
    event_time: Annotated[RFC3339, AfterValidator(_event_time)] | None = None

    def to_input(self) -> MessageInput:
        """转换为核心写入接受的消息。"""
        role: Literal["user", "assistant"] = (
            "user" if self.role == Role.USER else "assistant"
        )
        return MessageInput(
            role,
            self.content,
            self.event_time.timestamp() if self.event_time is not None else None,
        )


class RememberRequest(BaseModel):
    """按会话内的发言顺序接收非空批次。"""

    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[RememberMessage], Field(min_length=1, max_length=64)]


class OperationError(BaseModel):
    """后台失败的公开分类。"""

    code: str
    stage: RememberStage | None = None


class OperationResponse(BaseModel):
    """写入进度与对应终态的简要结果。"""

    operation_id: str
    session_id: str
    status: OperationStatus
    created_at: datetime
    updated_at: datetime
    next_retry_at: datetime | None = None
    message_ids: list[str] | None = None
    fact_change_count: int | None = None
    error: OperationError | None = None

    @classmethod
    def from_operation(cls, item: Operation) -> Self:
        """公开状态与结果，不返回输入副本和内部租约。"""
        return cls(
            operation_id=encode_resource_id(item.operation_id),
            session_id=encode_resource_id(item.session_id.value),
            status=item.status,
            created_at=item.created_at.astimezone(UTC),
            updated_at=item.updated_at.astimezone(UTC),
            next_retry_at=item.next_retry_at.astimezone(UTC)
            if item.next_retry_at
            else None,
            message_ids=[encode_resource_id(value) for value in item.message_ids]
            if item.message_ids is not None
            else None,
            fact_change_count=item.fact_change_count,
            error=OperationError(code=item.error_code, stage=item.error_stage)
            if item.error_code
            else None,
        )


@router.post(
    "/sessions/{session_id}/remember",
    operation_id="remember",
    status_code=202,
    response_model_exclude_none=True,
    dependencies=[Depends(require_json)],
    responses={202: CREATED_LOCATION, **problem_responses(400, 409, 413, 415)},
)
async def remember(
    space_id: ResourceId,
    session_id: ResourceId,
    body: RememberRequest,
    request: Request,
    response: Response,
    pool: PoolDependency,
    owner_id: OwnerDependency,
    key: IdempotencyKey,
) -> OperationResponse:
    """持久接收消息并返回可轮询的任务。"""
    try:
        item = await PostgresOperations(pool).submit(
            owner_id=owner_id,
            space_id=SpaceId(space_id),
            session_id=SessionId(session_id),
            key=key,
            messages=tuple(message.to_input() for message in body.messages),
            queue_limit=request.app.state.queue_limit,
            configured=request.app.state.write_configured,
        )
    except SessionUnavailable as exc:
        raise HTTPException(404, detail="session_not_found") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail="idempotency_conflict") from exc
    except QueueFull as exc:
        raise HTTPException(503, detail="remember_queue_full") from exc
    except WriteModelsUnavailable as exc:
        raise HTTPException(503, detail="write_models_unavailable") from exc
    accepted = OperationResponse.from_operation(item)
    response.headers["Location"] = (
        f"/v1/spaces/{encode_resource_id(space_id)}/operations/{accepted.operation_id}"
    )
    return accepted


@router.get(
    "/operations/{operation_id}",
    operation_id="getOperation",
    response_model_exclude_none=True,
)
async def get_operation(
    space_id: ResourceId,
    operation_id: ResourceId,
    pool: PoolDependency,
    owner_id: OwnerDependency,
) -> OperationResponse:
    """查询当前空间内的写入任务状态。"""
    try:
        item = await PostgresOperations(pool).get(
            owner_id, SpaceId(space_id), operation_id
        )
    except OperationNotFound as exc:
        raise HTTPException(404, detail="operation_not_found") from exc
    return OperationResponse.from_operation(item)
