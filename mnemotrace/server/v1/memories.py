"""在已授权空间召回事实并读取证据和原文。"""

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError

from ...application.recall import Recall, RecallQuery
from ...domain.ids import FactId, MessageId, SpaceId
from ...domain.values import Label
from ...ports.inference import Embedder
from ...ports.queries import FactFilter, FactQuery
from ...ports.reranker import Reranker
from ...ports.runtime import Clock
from ..auth import AuthorizedStorage
from ..errors import problem_responses
from ..recall_usage import RecallUsageQueue
from .audit_schemas import (
    ChangeListScope,
    ChangePageCursor,
    FactChangePage,
    FactChangesQuery,
)
from .fact_cursor import FactListScope, FactPageCursor
from .memory_schemas import (
    FactListQuery,
    FactPage,
    FactResponse,
    MessageResponse,
    RecallRequest,
    RecallResponse,
)
from .schemas import ResourceId, require_empty_body, require_json

router = APIRouter(
    prefix="/spaces/{space_id}",
    tags=["memory"],
    responses=problem_responses(401, 404, 422, 500, 503),
)


@router.post(
    "/recall",
    operation_id="recall",
    dependencies=[Depends(require_json)],
    responses=problem_responses(400, 413, 415, 502),
)
async def recall(
    space_id: ResourceId,
    body: RecallRequest,
    request: Request,
    storage: AuthorizedStorage,
) -> RecallResponse:
    """返回查询时点有效的可见事实证据。"""
    try:
        graph = body.graph.to_core() if body.graph is not None else None
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "graph")}]
        ) from exc
    space = SpaceId(space_id)
    embedder = cast(Embedder | None, request.app.state.embedder)
    reranker = cast(Reranker | None, request.app.state.reranker)
    use_rerank = reranker is not None if body.rerank is None else body.rerank
    if embedder is None or (use_rerank and reranker is None):
        async with storage.session(space):
            code = (
                "embedding_not_configured"
                if embedder is None
                else "reranker_not_configured"
            )
            raise HTTPException(503, detail=code)
    queue = cast(RecallUsageQueue | None, request.app.state.recall_usage)
    reader = Recall(
        space_id=space,
        storage=storage,
        embedder=embedder,
        clock=cast(Clock, request.app.state.recall_clock),
        exclude_sensitive=not body.include_sensitive,
        reranker=reranker,
    )
    query = RecallQuery(
        body.query,
        top_k=body.top_k,
        as_of=body.as_of.timestamp() if body.as_of is not None else None,
        graph=graph,
        prefer_recent=body.prefer_recent,
        rerank=use_rerank,
        filters=body.filters.to_core() if body.filters is not None else None,
        max_evidence_chars=body.max_evidence_chars,
    )
    result, usage = await reader.recall_with_usage(
        query, track_usage=body.track_usage and queue is not None
    )
    response = RecallResponse.from_result(result)
    if usage is not None and queue is not None:
        queue.submit(storage, usage)
    return response


@router.get("/facts", operation_id="listFacts")
async def list_facts(
    space_id: ResourceId,
    query: Annotated[FactListQuery, Query()],
    storage: AuthorizedStorage,
) -> FactPage:
    """按有效状态筛选并分页列出可见事实。"""
    scope = FactListScope(
        space_id=space_id,
        state=query.state,
        as_of=query.as_of,
        include_sensitive=query.include_sensitive,
        labels=tuple(sorted(set(query.labels))),
        source=query.source,
    )
    async with storage.session(SpaceId(space_id)) as reader:
        cursor = (
            FactPageCursor.decode_for_scope(query.cursor, scope)
            if query.cursor is not None
            else None
        )
        evaluated_at = (
            cursor.evaluated_at
            if cursor is not None
            else query.as_of or datetime.now(UTC)
        )
        spec = FactQuery(
            filters=FactFilter(
                state=query.state,
                valid_at=query.as_of.timestamp() if query.as_of is not None else None,
                exclude_sensitive=not query.include_sensitive,
                labels=tuple(map(Label, scope.labels)),
                source=scope.source,
            ),
            after=cursor.position() if cursor is not None else None,
            limit=query.limit + 1,
        )
        facts = await reader.query_facts(spec, reference_time=evaluated_at.timestamp())
    next_cursor = None
    if len(facts) > query.limit:
        last = facts[query.limit - 1]
        next_cursor = FactPageCursor(
            scope=scope,
            evaluated_at=evaluated_at,
            recorded_at=last.recorded_at,
            fact_id=last.id.value,
        ).encode()
    return FactPage(
        items=[FactResponse.from_fact(fact) for fact in facts[: query.limit]],
        next_cursor=next_cursor,
        evaluated_at=evaluated_at,
    )


@router.get("/facts/{fact_id}", operation_id="getFact")
async def get_fact(
    space_id: ResourceId,
    fact_id: ResourceId,
    storage: AuthorizedStorage,
    include_sensitive: bool = False,
) -> FactResponse:
    """读取可见事实的详情和两条时间线。"""
    async with storage.session(SpaceId(space_id)) as reader:
        fact = await reader.get_fact(
            FactId(fact_id), exclude_sensitive=not include_sensitive
        )
    if fact is None:
        raise HTTPException(404, detail="fact_not_found")
    return FactResponse.from_fact(fact)


@router.get("/facts/{fact_id}/changes", operation_id="listFactChanges")
async def list_fact_changes(
    space_id: ResourceId,
    fact_id: ResourceId,
    query: Annotated[FactChangesQuery, Query()],
    storage: AuthorizedStorage,
) -> FactChangePage:
    """分页读取事实的可见变更和截止依据。"""
    scope = ChangeListScope(
        space_id=space_id,
        resource="fact",
        resource_id=fact_id,
        include_sensitive=query.include_sensitive,
    )
    identifier = FactId(fact_id)
    exclude_sensitive = not query.include_sensitive
    async with storage.session(SpaceId(space_id)) as reader:
        if (
            await reader.get_fact(identifier, exclude_sensitive=exclude_sensitive)
            is None
        ):
            raise HTTPException(404, detail="fact_not_found")
        changes = await reader.list_fact_changes(
            identifier,
            after=ChangePageCursor.position_for(query.cursor, scope),
            limit=query.limit + 1,
            exclude_sensitive=exclude_sensitive,
        )
    return FactChangePage.from_changes(changes, scope, query.limit)


@router.get("/messages/{message_id}", operation_id="getMessage")
async def get_message(
    space_id: ResourceId, message_id: ResourceId, storage: AuthorizedStorage
) -> MessageResponse:
    """读取所属空间中的完整会话原文。"""
    identifier = MessageId(message_id)
    async with storage.session(SpaceId(space_id)) as reader:
        messages = await reader.get_messages((identifier,))
    message = messages.get(identifier)
    if message is None:
        raise HTTPException(404, detail="message_not_found")
    return MessageResponse.from_message(message)


@router.delete(
    "/facts/{fact_id}",
    operation_id="deleteFact",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_empty_body)],
    responses=problem_responses(400, 413),
)
async def delete_fact(
    space_id: ResourceId, fact_id: ResourceId, storage: AuthorizedStorage
) -> Response:
    """删除指定事实及其派生记录，保留会话原文。"""
    identifier = FactId(fact_id)
    async with storage.session(SpaceId(space_id), write=True) as writer:
        if await writer.get_fact(identifier) is None:
            raise HTTPException(404, detail="fact_not_found")
        await writer.delete_facts((identifier,))
    return Response(status_code=204)
