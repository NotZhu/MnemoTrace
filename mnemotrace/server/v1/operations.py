"""接收会话记忆批次并公开持久写入的进度。"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field

from ...application.operations import (
    IdempotencyConflict,
    Operation,
    OperationNotFound,
    OperationStatus,
    QueueFull,
    WriteModelsUnavailable,
)
from ...application.remember import (
    MessageInput,
    RememberStage,
    UnrepresentableMessageTime,
)
from ...domain.enums import Role
from ...domain.ids import SessionId, SpaceId
from ...ports.storage import SessionUnavailable
from ..auth import OwnerDependency
from ..dependencies import OperationsDependency
from ..errors import problem_responses
from .audit_schemas import (
    ChangeListScope,
    ChangePageCursor,
    FactChangePage,
    FactChangesQuery,
    OperationFactChangePage,
)
from .page_cursor import SequencePageCursor
from .schemas import (
    CREATED_LOCATION,
    RFC3339,
    IdempotencyKey,
    NonblankText,
    PageQuery,
    ResourceId,
    require_json,
)

router = APIRouter(
    prefix="/spaces/{space_id}",
    tags=["operations"],
    responses=problem_responses(401, 404, 422, 503),
)


class RememberMessage(BaseModel):
    """一条待抽取的原文。"""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: NonblankText
    event_time: RFC3339 | None = None

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


class OperationSummary(BaseModel):
    """写入进度与失败诊断。"""

    operation_id: str
    session_id: str
    status: OperationStatus
    attempts: Annotated[int, Field(ge=0)]
    created_at: datetime
    updated_at: datetime
    next_retry_at: datetime | None = None
    error: OperationError | None = None

    @classmethod
    def from_operation(cls, item: Operation) -> Self:
        """投影列表和详情共用的任务字段。"""
        return cls(
            operation_id=item.operation_id,
            session_id=item.session_id.value,
            status=item.status,
            attempts=item.attempts,
            created_at=item.created_at.astimezone(UTC),
            updated_at=item.updated_at.astimezone(UTC),
            next_retry_at=item.next_retry_at.astimezone(UTC)
            if item.next_retry_at
            else None,
            error=OperationError(code=item.error_code, stage=item.error_stage)
            if item.error_code
            else None,
        )


class OperationResponse(OperationSummary):
    """写入进度与已发布的结果摘要。"""

    message_ids: list[str] | None = None
    fact_change_count: int | None = None

    @classmethod
    def from_operation(cls, item: Operation) -> Self:
        """在任务状态的基础上添加已发布的结果。"""
        return cls(
            **OperationSummary.from_operation(item).model_dump(),
            message_ids=list(item.message_ids)
            if item.message_ids is not None
            else None,
            fact_change_count=item.fact_change_count,
        )


class OperationListQuery(PageQuery):
    """查询同一空间内指定状态或会话的任务。"""

    status: OperationStatus | None = None
    session_id: ResourceId | None = None


class OperationListScope(BaseModel):
    """标识任务列表的空间和完整筛选范围。"""

    model_config = ConfigDict(extra="forbid")

    space_id: NonblankText
    status: OperationStatus | None
    session_id: NonblankText | None


OperationPageCursor = SequencePageCursor[OperationListScope]


class OperationPage(BaseModel):
    """一页按接收顺序排列的任务。"""

    items: list[OperationSummary]
    next_cursor: str | None


@router.post(
    "/sessions/{session_id}/remember",
    operation_id="remember",
    status_code=202,
    response_model_exclude_none=True,
    dependencies=[Depends(require_json)],
    responses={202: CREATED_LOCATION, **problem_responses(409, 415)},
)
async def remember(
    space_id: ResourceId,
    session_id: ResourceId,
    body: RememberRequest,
    request: Request,
    response: Response,
    operations: OperationsDependency,
    owner_id: OwnerDependency,
    key: IdempotencyKey,
) -> OperationResponse:
    """持久接收消息并返回可轮询的任务。"""
    try:
        item = await operations.submit(
            owner_id=owner_id,
            space_id=SpaceId(space_id),
            session_id=SessionId(session_id),
            key=key,
            messages=tuple(message.to_input() for message in body.messages),
            extraction_timezone=request.app.state.extraction_timezone,
            queue_limit=request.app.state.queue_limit,
            configured=request.app.state.write_configured,
        )
    except SessionUnavailable as exc:
        raise HTTPException(404, detail="session_not_found") from exc
    except UnrepresentableMessageTime as exc:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "messages", exc.message_index, "event_time"),
                    "msg": str(exc),
                }
            ]
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail="idempotency_conflict") from exc
    except QueueFull as exc:
        raise HTTPException(503, detail="remember_queue_full") from exc
    except WriteModelsUnavailable as exc:
        raise HTTPException(503, detail="write_models_unavailable") from exc
    accepted = OperationResponse.from_operation(item)
    response.headers["Location"] = str(
        request.app.url_path_for(
            "get_operation", space_id=space_id, operation_id=accepted.operation_id
        )
    )
    return accepted


@router.get("/operations", operation_id="listOperations")
async def list_operations(
    space_id: ResourceId,
    query: Annotated[OperationListQuery, Query()],
    operations: OperationsDependency,
    owner_id: OwnerDependency,
) -> OperationPage:
    """按接收顺序倒序列出当前空间的任务。"""
    scope = OperationListScope(
        space_id=space_id, status=query.status, session_id=query.session_id
    )
    items = await operations.list(
        owner_id,
        SpaceId(space_id),
        status=query.status,
        session_id=SessionId(query.session_id)
        if query.session_id is not None
        else None,
        after=OperationPageCursor.position_for(query.cursor, scope),
        limit=query.limit + 1,
    )
    return OperationPage(
        items=[
            OperationSummary.from_operation(item) for _, item in items[: query.limit]
        ],
        next_cursor=(
            OperationPageCursor(
                scope=scope, position=items[query.limit - 1][0]
            ).encode()
            if len(items) > query.limit
            else None
        ),
    )


@router.get(
    "/operations/{operation_id}",
    operation_id="getOperation",
    response_model_exclude_none=True,
)
async def get_operation(
    space_id: ResourceId,
    operation_id: ResourceId,
    operations: OperationsDependency,
    owner_id: OwnerDependency,
) -> OperationResponse:
    """查询当前空间内的写入任务状态。"""
    try:
        item = await operations.get(owner_id, SpaceId(space_id), operation_id)
    except OperationNotFound as exc:
        raise HTTPException(404, detail="operation_not_found") from exc
    return OperationResponse.from_operation(item)


@router.get(
    "/operations/{operation_id}/fact-changes", operation_id="listOperationFactChanges"
)
async def list_operation_fact_changes(
    space_id: ResourceId,
    operation_id: ResourceId,
    query: Annotated[FactChangesQuery, Query()],
    operations: OperationsDependency,
    owner_id: OwnerDependency,
) -> OperationFactChangePage:
    """读取任务状态和在同一发布事务中产生的可见事实变更。"""
    scope = ChangeListScope(
        space_id=space_id,
        resource="operation",
        resource_id=operation_id,
        include_sensitive=query.include_sensitive,
    )
    try:
        operation, changes = await operations.list_fact_changes(
            owner_id,
            SpaceId(space_id),
            operation_id,
            after=ChangePageCursor.position_for(query.cursor, scope),
            limit=query.limit + 1,
            exclude_sensitive=not query.include_sensitive,
        )
    except OperationNotFound as exc:
        raise HTTPException(404, detail="operation_not_found") from exc
    page = FactChangePage.from_changes(changes, scope, query.limit)
    return OperationFactChangePage(
        items=page.items,
        next_cursor=page.next_cursor,
        status=operation.status,
    )
