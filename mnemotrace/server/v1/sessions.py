"""在已授权空间中登记和查看会话。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from ...adapters.runtime import UuidGenerator
from ...application.sessions import create_session_registration
from ...domain.ids import SessionId, SpaceId
from ...ports.storage import SessionRegistration
from ..auth import AuthorizedStorage
from ..errors import problem_responses
from .memory_schemas import MessagePage, MessageResponse
from .page_cursor import SequencePageCursor, SessionPageCursor
from .schemas import (
    CREATED_LOCATION,
    NonblankText,
    PageQuery,
    ResourceId,
    SessionPage,
    SessionResponse,
    require_empty_body,
)

router = APIRouter(
    prefix="/spaces/{space_id}/sessions",
    tags=["sessions"],
    responses=problem_responses(401, 404, 422, 500, 503),
)
_ids = UuidGenerator()


class MessageListScope(BaseModel):
    """标识原文页所属的空间和会话。"""

    model_config = ConfigDict(extra="forbid")

    space_id: NonblankText
    session_id: NonblankText


MessagePageCursor = SequencePageCursor[MessageListScope]


@router.post(
    "",
    operation_id="createSession",
    status_code=201,
    responses={201: CREATED_LOCATION, **problem_responses(413)},
)
async def create_session(
    space_id: ResourceId,
    request: Request,
    response: Response,
    storage: AuthorizedStorage,
) -> SessionResponse:
    """登记一个供后续写入使用的会话。"""
    # 登记本身不使用正文，仍需让中间件在写入前检查流式请求的实际字节数。
    async for _ in request.stream():
        pass
    session = await create_session_registration(storage, SpaceId(space_id), _ids)
    created = SessionResponse.from_registration(session)
    response.headers["Location"] = (
        f"/v1/spaces/{space_id}/sessions/{created.session_id}"
    )
    return created


@router.get("", operation_id="listSessions")
async def list_sessions(
    space_id: ResourceId,
    storage: AuthorizedStorage,
    query: Annotated[PageQuery, Query()],
) -> SessionPage:
    """分页列出所属空间的会话，包括尚未写入消息的会话。"""
    async with storage.session(SpaceId(space_id)) as reader:
        after = None
        if query.cursor is not None:
            cursor = SessionPageCursor.decode(query.cursor)
            if cursor.space_id != space_id:
                raise HTTPException(422, detail="invalid_cursor")
            after = SessionRegistration(SessionId(cursor.session_id), cursor.created_at)
        limit = query.limit
        sessions = await reader.list_sessions(after=after, limit=limit + 1)
    next_cursor = None
    if len(sessions) > limit:
        last = sessions[limit - 1]
        next_cursor = SessionPageCursor(
            space_id=space_id,
            session_id=last.session_id.value,
            created_at=last.created_at,
        ).encode()
    return SessionPage(
        items=[SessionResponse.from_registration(item) for item in sessions[:limit]],
        next_cursor=next_cursor,
    )


@router.get("/{session_id}", operation_id="getSession")
async def get_session(
    space_id: ResourceId, session_id: ResourceId, storage: AuthorizedStorage
) -> SessionResponse:
    """读取空间内指定的会话登记。"""
    async with storage.session(SpaceId(space_id)) as reader:
        session = await reader.get_session(SessionId(session_id))
    if session is None:
        raise HTTPException(404, detail="session_not_found")
    return SessionResponse.from_registration(session)


@router.delete(
    "/{session_id}",
    operation_id="deleteSession",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_empty_body)],
    responses=problem_responses(400, 413),
)
async def delete_session(
    space_id: ResourceId, session_id: ResourceId, storage: AuthorizedStorage
) -> Response:
    """擦除会话原文、任务及失去独立证据的抽取事实。"""
    identifier = SessionId(session_id)
    async with storage.maintenance_session(SpaceId(space_id)) as writer:
        if not await writer.has_session(identifier):
            raise HTTPException(404, detail="session_not_found")
        await writer.delete_session_data(identifier)
    return Response(status_code=204)


@router.get("/{session_id}/messages", operation_id="listSessionMessages")
async def list_session_messages(
    space_id: ResourceId,
    session_id: ResourceId,
    query: Annotated[PageQuery, Query()],
    storage: AuthorizedStorage,
) -> MessagePage:
    """按追加位置分页读取已登记会话的原文。"""
    scope = MessageListScope(space_id=space_id, session_id=session_id)
    identifier = SessionId(session_id)
    async with storage.session(SpaceId(space_id)) as reader:
        if not await reader.has_session(identifier):
            raise HTTPException(404, detail="session_not_found")
        after = MessagePageCursor.position_for(query.cursor, scope)
        messages = await reader.list_session_messages(
            identifier, after=after, limit=query.limit + 1
        )
    return MessagePage(
        items=[
            MessageResponse.from_message(message)
            for _, message in messages[: query.limit]
        ],
        next_cursor=(
            MessagePageCursor(
                scope=scope, position=messages[query.limit - 1][0]
            ).encode()
            if len(messages) > query.limit
            else None
        ),
    )
