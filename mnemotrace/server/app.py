"""HTTP API for key-authenticated PostgreSQL memory spaces."""
from __future__ import annotations

from threading import Lock
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import ConfigDict, StrictBool, BaseModel, ConfigDict, Field, StrictBool, model_validator
    from starlette.exceptions import HTTPException as StarletteHTTPException
except Exception as exc:  # noqa: BLE001
    raise SystemExit("the server needs FastAPI — `pip install \"mnemotrace-memory[server]\"`") from exc

from .. import __version__
from ..embed import EmbeddingUnavailable
from ..service import MemoryRevisionChanged, MemoryService, ProcessingFailure
from ..extraction import ExtractionError
from ..categories import Category
from ..store.contracts import FactState, SpaceContext
from ..store.postgres.errors import (
    AccountNotFound, AuthenticationError, CommitOutcomeUnknown, DatabaseConfigurationError,
    DatabaseError, DatabaseUnavailable, MessageConflict, SchemaMismatch, SpaceNotFound, VersionConflict,
)


DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _max_request_bytes() -> int:
    raw = os.environ.get("MNEMOTRACE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("MNEMOTRACE_MAX_REQUEST_BYTES must be a positive integer") from exc
    if value <= 0:
        raise ValueError("MNEMOTRACE_MAX_REQUEST_BYTES must be a positive integer")
    return value


def _bearer_token(authorization: str) -> str:
    """Reject malformed bearer credentials before accessing the database."""
    if any(not (" " <= char <= "~") for char in authorization):
        raise HTTPException(401, "bearer key must use printable ASCII characters")
    value = authorization.strip()
    if not value:
        return ""
    scheme, sep, token = value.partition(" ")
    if scheme.lower() != "bearer" or not sep or not token.strip():
        raise HTTPException(401, "expected Authorization: Bearer <token>")
    return token.strip()


_svc: MemoryService | None = None
_embedding_startup_error: str | None = None
_service_lock = Lock()


def svc() -> MemoryService:
    global _svc
    with _service_lock:
        if _embedding_startup_error is not None:
            raise EmbeddingUnavailable(_embedding_startup_error)
        if _svc is None:
            _svc = MemoryService.from_env()
        return _svc


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _svc, _embedding_startup_error
    service = None
    try:
        service = svc()
    except EmbeddingUnavailable as exc:
        _embedding_startup_error = str(exc)
    except DatabaseError as exc:
        # Uvicorn logs startup tracebacks, so suppress the driver's chained error.
        raise RuntimeError(str(exc)) from None
    try:
        yield
    finally:
        with _service_lock:
            if service is not None:
                service.close()
            _svc = None
            _embedding_startup_error = None


app = FastAPI(
    title="MnemoTrace Memory Service",
    version=__version__,
    description="Multi-tenant long-term memory — connect with a Bearer key and manage your own memory.",
    lifespan=_lifespan,
)


def _apply_security_headers(response, path: str):
    if path.startswith("/v1/"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith("/ui/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path == "/ui" or path.startswith("/ui/") or path == "/":
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    return response


@app.exception_handler(ExtractionError)
async def extraction_failure(_request: Request, error: ExtractionError):
    return JSONResponse(status_code=error.status_code, content={"detail": error.detail()})


@app.exception_handler(EmbeddingUnavailable)
async def embedding_failure(_request: Request, error: EmbeddingUnavailable):
    return JSONResponse(status_code=503, content={'detail': {
        'code': 'embedding_unavailable', 'message': str(error),
    }})


@app.exception_handler(DatabaseError)
async def database_failure(_request: Request, error: DatabaseError):
    status, message = 500, 'Memory storage operation failed'
    if isinstance(error, (AuthenticationError, AccountNotFound)):
        status, message = 401, 'Invalid or missing API key'
    elif isinstance(error, SpaceNotFound):
        status, message = 404, 'Memory space unavailable'
    elif isinstance(error, (VersionConflict, MessageConflict)):
        status, message = 409, 'Memory operation conflicts with existing data'
    elif isinstance(error, (DatabaseUnavailable, DatabaseConfigurationError, SchemaMismatch)):
        status, message = 503, 'Memory database unavailable'
    detail = dict(code=error.code, message=message)
    if isinstance(error, CommitOutcomeUnknown):
        detail['operation_id'] = error.operation_id
    return JSONResponse(status_code=status, content={'detail': detail})


@app.exception_handler(ProcessingFailure)
async def processing_failure(_request: Request, error: ProcessingFailure):
    detail = error.detail()
    status = {'processing_incomplete': 503, 'database_unavailable': 503, 'data_corruption': 500,
              'version_conflict': 409, 'extraction_conflict': 409}.get(detail['code'], 502)
    return JSONResponse(status_code=status, content={'detail': detail})


@app.exception_handler(ValueError)
async def invalid_parameter(_request: Request, _error: ValueError):
    return JSONResponse(status_code=422, content={'detail': {'code': 'invalid_parameter',
                                                           'message': 'Invalid memory operation parameters'}})


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, error: RequestValidationError):
    fields = [{'location': issue['loc'], 'code': issue['type']} for issue in error.errors()]
    return JSONResponse(status_code=422, content={'detail': {'code': 'invalid_parameter',
                                                           'message': 'Invalid request', 'fields': fields}})


@app.middleware("http")
async def request_limits_and_security_headers(request: Request, call_next):
    path = request.url.path
    try:
        max_bytes = _max_request_bytes()
    except ValueError:
        return _apply_security_headers(
            JSONResponse({"detail": "invalid server request-limit configuration"}, status_code=503),
            path,
        )
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            return _apply_security_headers(
                JSONResponse({"detail": "invalid Content-Length"}, status_code=400), path
            )
        if declared < 0:
            return _apply_security_headers(
                JSONResponse({"detail": "invalid Content-Length"}, status_code=400), path
            )
        if declared > max_bytes:
            return _apply_security_headers(
                JSONResponse({"detail": "request body too large"}, status_code=413), path
            )

    # 除了声明的大小，还要统计实际收到的字节数。缓存有界 body 既让 Starlette 可以把它重放给
    # FastAPI，又防止 chunked 或虚报长度的请求绕过限制、写入记忆。
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_bytes:
                return _apply_security_headers(
                    JSONResponse({"detail": "request body too large"}, status_code=413), path
                )
            chunks.append(chunk)
        request._body = b"".join(chunks)

    return _apply_security_headers(await call_next(request), path)


def auth(authorization: str = Header(default='')) -> str:
    token = _bearer_token(authorization)
    if not token:
        raise AuthenticationError('Invalid API key')
    return svc().spaces.authenticate(token).user_id


def authorized_space(space_id: str = Query(min_length=1, max_length=512),
                     owner: str = Depends(auth)) -> SpaceContext:
    return svc().spaces.authorize(owner, space_id)


@app.get("/metrics")
def metrics():
    """实时服务指标（AGENTS.md Bet D online）：每个操作的 p50/p95 延迟、请求计数器，以及
    检索上下文相对全量历史的 token 节省比。只输出聚合数字——不包含命名空间名和用户内容，
    因此可以保持和 /health 一样开放。"""
    return svc().metrics_snapshot()


class SpaceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    space_id: str = Field(min_length=1, max_length=512)


class SpaceName(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=128)


class RememberReq(SpaceRequest):
    content: str = Field(min_length=1, max_length=1_000_000)
    session_id: str = Field(default="default", min_length=1, max_length=512)
    extract: StrictBool = True
    processing: Literal['immediate', 'buffered'] = 'immediate'
    agent_id: str | None = None
    timeout: float = Field(default=30, ge=0, le=300)


class DialogueMessage(BaseModel):
    model_config = {'extra': 'forbid', 'strict': True, 'allow_inf_nan': False}
    message_id: str = Field(min_length=1, max_length=512)
    role: Literal['user', 'assistant']
    content: str = Field(min_length=1, max_length=1_000_000)
    event_time: Optional[float] = None


class MessagesReq(SpaceRequest):
    model_config = {'extra': 'forbid'}
    session_id: str = Field(min_length=1, max_length=512)
    messages: list[DialogueMessage] = Field(min_length=1, max_length=1000)
    processing: Literal['immediate', 'buffered'] = 'buffered'
    extract: StrictBool = True
    agent_id: str | None = None
    timeout: float = Field(default=30, ge=0, le=300)


class ProcessingFlushReq(SpaceRequest):
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    timeout: float = Field(default=30, ge=0, le=300)
    retry_failed: bool = False
    include_digests: bool = False


class RecallReq(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    space_id: str | None = Field(default=None, min_length=1, max_length=512)
    space_ids: list[str] | None = Field(default=None, min_length=1, max_length=16)
    query: str = Field(min_length=1, max_length=32_768)
    answer: bool = False
    subject: str | None = Field(default=None, min_length=1, max_length=512)
    predicate: str | None = Field(default=None, min_length=1, max_length=512)
    lean: bool = True
    n_digests: int = Field(default=20, ge=0, le=50)
    state: FactState = FactState.CURRENT
    valid_at: float | None = None
    as_of: Optional[float] = None  # epoch 秒：某时间点的记忆视图（as-of）
    redact_sensitive: bool = False  # 在返回的上下文/fact 列表中隐藏被标记为敏感的 fact


@app.get("/health")
def health():
    try:
        service = svc()
        service.ready()
    except EmbeddingUnavailable as exc:
        return dict(ok=True, ready=False, storage='postgres', auth_mode='api_keys',
                    detail=dict(code='embedding_unavailable', message=str(exc)))
    except DatabaseError:
        return dict(ok=True, ready=False, storage='postgres', auth_mode='api_keys')
    return dict(ok=True, ready=True, service='mnemotrace', version=__version__, storage='postgres',
                auth_mode='api_keys', anonymous_allowed=False, embedder=service.embedding_model or 'lexical',
                llm_configured=service.llm is not None, answerer_configured=service.answerer is not None)


@app.get("/ready")
def ready():
    payload = health()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


# 生产环境控制台（frontend/ 里的 React 应用）构建完成后由 /ui 提供服务；"/" 会重定向到那里。
# 未构建时只提供构建提示；API、离线 quickstart 和健康检查独立于控制台构建。
_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend", "dist")


def _spa_built() -> bool:
    return os.path.isfile(os.path.join(_DIST, "index.html"))


def _console_unavailable():
    return HTMLResponse(
        '<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>控制台尚未构建</title><h1>控制台尚未构建</h1>'
        '<p>在源码目录执行以下命令，然后重启服务：</p>'
        '<pre>cd frontend\npnpm install --frozen-lockfile\npnpm run build</pre>'
        '<p>HTTP API 与健康检查仍可独立使用。</p></html>',
        status_code=503,
    )


@app.get("/")
def root():
    if _spa_built():
        return RedirectResponse(url="/ui/")
    return _console_unavailable()


@app.get("/robots.txt")
def robots_txt():
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(
        "User-agent: *\n"
        "Disallow: /ui\n"
        "Disallow: /ui/\n"
        "Disallow: /v1/\n"
        "Disallow: /health\n"
    )


def _space_payload(space):
    return dict(space_id=space.space_id, name=space.name, status=space.status)


@app.get('/v1/spaces')
def list_spaces(after_id: str | None = None, limit: int = Query(100, ge=1, le=1000),
                owner: str = Depends(auth)):
    spaces = svc().spaces.list(owner, after_id=after_id, limit=limit)
    return dict(spaces=[_space_payload(space) for space in spaces],
                next_after_id=spaces[-1].space_id if len(spaces) == limit else None)


@app.post('/v1/spaces', status_code=201)
def create_space(req: SpaceName, owner: str = Depends(auth)):
    return _space_payload(svc().spaces.create(owner, req.name))


@app.patch('/v1/spaces/{space_id}')
def rename_space(space_id: str, req: SpaceName, owner: str = Depends(auth)):
    return _space_payload(svc().spaces.rename(owner, space_id, req.name))


@app.delete('/v1/spaces/{space_id}')
def delete_space(space_id: str, confirm: bool = False, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, space_id)
    if not confirm:
        raise HTTPException(400, 'Space deletion requires confirm=true')
    svc().memory(context).delete_space()
    return dict(ok=True, space_id=space_id)


@app.post('/v1/messages')
def remember_messages(req: MessagesReq, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    data = svc().remember_messages(context, [message.model_dump() for message in req.messages],
                                   req.session_id, req.processing, req.extract,
                                   agent_id=req.agent_id, timeout=req.timeout)
    return JSONResponse(data, status_code=202) if req.processing == 'buffered' else data


@app.post('/v1/remember')
def remember(req: RememberReq, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    data = svc().remember(context, req.content, req.session_id, req.extract, req.processing,
                          agent_id=req.agent_id, timeout=req.timeout)
    return JSONResponse(data, status_code=202) if req.processing == 'buffered' else data


@app.get('/v1/operations/{operation_id}')
def get_operation(operation_id: str, context: SpaceContext = Depends(authorized_space)):
    result = svc().operation(context, operation_id)
    if result is None:
        raise HTTPException(404, {'code': 'operation_not_found', 'message': 'Operation unavailable'})
    return result


@app.post('/v1/operations/{operation_id}/retry')
def retry_operation(operation_id: str, req: SpaceRequest, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    result = svc().operation(context, operation_id, retry=True)
    if result is None:
        raise HTTPException(404, {'code': 'operation_not_found', 'message': 'Operation unavailable'})
    return result


@app.get('/v1/processing')
def processing_status(session_id: str | None = None, after_id: str | None = None,
                      limit: int = Query(100, ge=1, le=1000), context: SpaceContext = Depends(authorized_space)):
    return svc().processing_status(context, session_id, after_id=after_id, limit=limit)


@app.post('/v1/processing/flush')
def processing_flush(req: ProcessingFlushReq, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    return svc().flush(context, req.session_id, req.timeout, req.retry_failed, req.include_digests)


@app.get('/v1/episodes/{episode_id}')
def get_episode(episode_id: str, context: SpaceContext = Depends(authorized_space)):
    result = svc().get_episode(context, episode_id)
    if result is None:
        raise HTTPException(404, 'Episode not found')
    return result


@app.post('/v1/recall')
def recall(req: RecallReq, owner: str = Depends(auth)):
    if (req.space_id is None) == (req.space_ids is None):
        raise ValueError('Provide space_id or space_ids')
    ids = req.space_ids if req.space_ids is not None else [req.space_id]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate space IDs')
    contexts = [svc().spaces.authorize(owner, space_id) for space_id in ids]
    options = dict(n_digests=req.n_digests, as_of=req.as_of,
                   redact_sensitive=req.redact_sensitive, state=req.state,
                   valid_at=req.valid_at)
    if len(contexts) > 1:
        if not req.lean or req.answer or req.subject is not None or req.predicate is not None:
            raise ValueError('Multi-space recall requires unfiltered lean context without an answer')
        return svc().recall_multi(contexts, req.query, **options)
    return svc().recall(contexts[0], req.query, lean=req.lean, answer=req.answer,
                        subject=req.subject, predicate=req.predicate, **options)


class StructuredRead(SpaceRequest):
    state: FactState = FactState.CURRENT
    valid_at: float | None = None
    redact_sensitive: bool = False


class AggregateReq(StructuredRead):
    predicate: str = Field(min_length=1, max_length=512)
    operation: Literal['count', 'count_distinct'] = 'count'
    subject: str | None = Field(default=None, max_length=512)
    start: float | None = None
    end: float | None = None
    query: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = Field(default=10, ge=1, le=1000)


class PathReq(StructuredRead):
    anchor: str = Field(min_length=1, max_length=512)
    predicates: list[str] = Field(min_length=2, max_length=4)
    limit: int = Field(default=20, ge=1, le=1000)


@app.post('/v1/aggregate')
def aggregate(req: AggregateReq, owner: str = Depends(auth)):
    service = svc()
    context = service.spaces.authorize(owner, req.space_id)
    return service.aggregate_facts(context, **req.model_dump(exclude={'space_id'}))


@app.post('/v1/follow-path')
def follow_path(req: PathReq, owner: str = Depends(auth)):
    service = svc()
    context = service.spaces.authorize(owner, req.space_id)
    return service.follow_path(context, **req.model_dump(exclude={'space_id'}))


@app.get("/v1/sessions")
def sessions(
    limit: Optional[int] = None,
    offset: int = 0,
    q: str = "",
    user: SpaceContext = Depends(authorized_space),
):
    """不含内容的 session 索引：session id、计数、时间戳和积压状态。

    在深入某个具体的 `/v1/sessions/report` 之前，可用它向用户展示哪些 Codex/Claude/app
    session 触碰过其记忆。
    """
    return svc().sessions(user, limit=limit, offset=offset, q=q)


@app.get("/v1/sessions/report")
def session_report(
    session_id: str,
    include_sensitive: bool = False,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: SpaceContext = Depends(authorized_space),
):
    """审计某个具体 session 向持久记忆贡献了什么。

    敏感 fact 文本默认脱敏。仅在用户明确拥有、且有意展示私有 fact 文本的审计场景下才传
    include_sensitive=true。
    """
    return svc().session_report(user, session_id, include_sensitive=include_sensitive, limit=limit, offset=offset)


@app.post("/v1/profile/refresh", status_code=202)
def refresh_profile(req: SpaceRequest, owner: str = Depends(auth)):
    return svc().refresh_profile(svc().spaces.authorize(owner, req.space_id))


@app.get("/v1/profile")
def profile(user: SpaceContext = Depends(authorized_space)):
    return svc().profile(user)


@app.get("/v1/memories")
def memories(
    facts_limit: Optional[int] = None,
    facts_offset: int = 0,
    episodes_limit: Optional[int] = None,
    episodes_offset: int = 0,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    q: str = "",
    include_sensitive: bool = False,
    categories: list[Category] | None = Query(default=None),
    outcome_kind: Optional[str] = None,
    expected_revision: Optional[str] = None,
    evaluated_at: Optional[float] = None,
    user: SpaceContext = Depends(authorized_space),
):
    """浏览已存储的记忆。

    默认是可分享的安全视图：非敏感 fact 加不含内容的计数，profile/原始 episode 均不返回。
    仅在明确的所有者检查视图中传 `include_sensitive=true`，此时会包含原始 episode、摘要、
    profile 和敏感 fact。
    `kind=outcomes` 只保留 session 结论（decision/finding/lesson/open_question），
    `kind=attributes` 只保留其余内容；其他值和 `status` 一样被忽略。
    `outcome_kind` 精确筛选四类结论；后续页成对携带 `expected_revision` 和 `evaluated_at`，
    分别固定已提交集合与状态评估时刻。提交版本不符时返回 409。
    categories 按任一标签匹配，在分页前执行，不过滤原始 episode。
    `fact_categories` 返回同一事实类型及敏感可见范围内的分类，不受搜索、状态及分页限制。
    """

    try:
        return svc().memories(
            user,
            facts_limit=facts_limit,
            facts_offset=facts_offset,
            episodes_limit=episodes_limit,
            episodes_offset=episodes_offset,
            status=status,
            kind=kind,
            q=q,
            include_sensitive=include_sensitive,
            categories=categories,
            outcome_kind=outcome_kind,
            expected_revision=expected_revision,
            evaluated_at=evaluated_at,
        )
    except MemoryRevisionChanged as exc:
        raise HTTPException(409, {"code": "memory_revision_changed", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/audit")
def audit(limit: int = 40, user: SpaceContext = Depends(authorized_space)):
    """记忆健康检查：列出值得修复的条目，每条都带被标记的原因和处理建议。

    个人记忆库赢得信任靠的是可检查，而不是体量大。在真实语料上做抽取必然产生垃圾，
    不逐条阅读 fact 根本发现不了。
    """
    return svc().audit(user, limit=max(1, min(limit, 200)))


@app.get("/v1/stats")
def stats(user: SpaceContext = Depends(authorized_space)):
    """不含内容的命名空间统计，供 dashboard 和就绪检查使用。与 /v1/memories 不同，它不返回
    profile 文本、fact 文本、episode 片段或存储路径。"""
    return svc().stats(user)


@app.get("/v1/agent/status")
def agent_status(session_id: Optional[str] = None, user: SpaceContext = Depends(authorized_space)):
    """给 agent 客户端在决定 recall/remember/close 之前查看的不含内容状态。

    返回命名空间/session 计数、focus 策略和推荐的工具调用，不暴露 fact 文本、profile 文字、
    原始 episode、摘要或存储路径。
    """
    return svc().agent_status(user, session_id=session_id)


class ForgetReq(SpaceRequest):
    confirm: bool = False


@app.post("/v1/forget")
def forget(req: ForgetReq, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    if not req.confirm:
        raise HTTPException(400, 'Space clearing requires confirm=true')
    return svc().forget(context)


# --- OpenAI 兼容 chat，透明叠加记忆（即插即用：把 OpenAI 客户端的 base_url 指到这里）--
class ChatMemoryRequest(SpaceRequest):
    recall: bool = True
    remember: bool = True
    session_id: str = Field(default='default', min_length=1, max_length=512)
    extract: StrictBool = True
    n_digests: int = Field(default=20, ge=0, le=50)
    as_of: float | None = None
    redact_sensitive: bool = False
    message_id: str | None = Field(default=None, min_length=1, max_length=512)
    agent_id: str | None = None


class ChatCompletionReq(BaseModel):
    model_config = ConfigDict(extra="allow")  # 接受（并忽略）temperature/top_p 等参数。
    model: str = "mnemotrace"
    messages: list[dict] = []
    stream: bool = False
    # 忆痕 扩展：recall/remember/session_id/extract/n_digests/as_of/redact_sensitive。
    memory: ChatMemoryRequest


@app.post('/v1/chat/completions')
def chat_completions(req: ChatCompletionReq, owner: str = Depends(auth)):
    from . import openai_compat as oc

    opts = req.memory
    context = svc().spaces.authorize(owner, opts.space_id)
    if not req.messages:
        raise HTTPException(400, 'messages must be a non-empty list')
    session_id = opts.session_id
    body = req.model_dump()
    try:
        response = oc.chat_completion(svc(), context, body, n_digests=opts.n_digests,
                                      do_recall=opts.recall, as_of=opts.as_of,
                                      redact_sensitive=opts.redact_sensitive,
                                      session_id=session_id)
    except oc.NoLLMConfigured as exc:
        raise HTTPException(503, str(exc)) from exc

    if opts.remember:
        content = oc.remembered_text(svc().captioner, req.messages)
        if content:
            agent_id = opts.agent_id
            result = svc().remember_messages(context, [
                dict(message_id=opts.message_id or response['id'] + ':user', role='user', content=content),
                dict(message_id=response['id'], role='assistant', event_time=response['created'],
                     content=response['choices'][0]['message']['content']),
            ], session_id, extract=opts.extract, agent_id=agent_id)
            response['mnemotrace'].update(remembered=True, remember_queued=bool(result['operation_ids']), remember_extract=opts.extract,
                                          operation_ids=result['operation_ids'])
    response['mnemotrace']['space_id'] = context.space_id
    if req.stream:
        return StreamingResponse(oc.iter_sse(response), media_type='text/event-stream')
    return response


class FactSensitivity(SpaceRequest):
    model_config = ConfigDict(extra='forbid')
    sensitive: StrictBool


@app.patch("/v1/facts/{fact_id}/sensitivity")
def set_fact_sensitive(fact_id: str, req: FactSensitivity, owner: str = Depends(auth)):
    context = svc().spaces.authorize(owner, req.space_id)
    result = svc().set_fact_sensitive(context, fact_id, req.sensitive)
    if result is None:
        raise HTTPException(404, "fact not found")
    return result


@app.delete("/v1/facts/{fact_id}")
def remove_fact(fact_id: str, user: SpaceContext = Depends(authorized_space)):
    """被遗忘权：永久删除单个 fact。"""
    return svc().delete_fact(user, fact_id)


class ClearSlotReq(SpaceRequest):
    subject: str
    predicate: str
    # 所有者批准时看到的计数。必填：不受保护的批量删除是这个端点绝不能提供的形态，
    # 因此不提供任何入口。
    expect_count: int


# 用 POST 而不是 DELETE /v1/facts/{slot}：slot 是 (subject, predicate) 对，若作为路径段路由
# 会与 /v1/facts/{fact_id} 冲突。
@app.post("/v1/facts/clear-slot")
def clear_slot(req: ClearSlotReq, owner: str = Depends(auth)):
    """按预期数量清理指定槽位的自动事实。"""
    user = svc().spaces.authorize(owner, req.space_id)
    if not req.subject.strip() or not req.predicate.strip():
        raise HTTPException(400, "subject and predicate are both required")
    return svc().clear_slot(user, req.subject, req.predicate, expect_count=req.expect_count)


# --- ③ focus 领域：自定义记忆强调（track）或抑制（mute）的内容 ---
class FocusReq(SpaceRequest):
    track: Optional[list[str]] = None  # None = 不变；[] = 清空
    mute: Optional[list[str]] = None


@app.get("/v1/focus")
def get_focus(user: SpaceContext = Depends(authorized_space)):
    return svc().get_focus(user)


@app.put("/v1/focus")
def put_focus(req: FocusReq, owner: str = Depends(auth)):
    """设置用户跟踪/静音的主题。被跟踪的主题显著度提升（排名更靠前、保持活跃）；
    被静音的主题在 recall 和 profile 中被隐藏。"""
    user = svc().spaces.authorize(owner, req.space_id)
    return svc().set_focus(user, track=req.track, mute=req.mute)


# --- 记忆策略：可编辑的 prompt + "记录什么"指令（记忆策略 页面）---
class PolicyReq(SpaceRequest):
    extract_instruction: Optional[str] = None  # None = 不变；"" = 重置为默认
    extract_system: Optional[str] = None
    digest_system: Optional[str] = None
    profile_system: Optional[str] = None


@app.get("/v1/policy")
def get_policy(user: SpaceContext = Depends(authorized_space)):
    """用户的 prompt 覆盖项以及内置默认值（控制台可以查看/编辑任意一方）。"""
    return svc().get_policy(user)


@app.put("/v1/policy")
def put_policy(req: PolicyReq, owner: str = Depends(auth)):
    """设置可编辑的抽取/摘要/persona prompt 和"记录什么"指令。在下一次 remember()/
    整合时生效。"""
    user = svc().spaces.authorize(owner, req.space_id)
    return svc().set_policy(user, **req.model_dump(exclude={'space_id'}))


# --- 需要人工判断的冲突 ---
class ResolveReq(SpaceRequest):
    keep: Literal["newer", "older", "both"] = "newer"


@app.get("/v1/conflicts")
def conflicts(user: SpaceContext = Depends(authorized_space)):
    """列出有效先后无法判断的待审冲突。"""
    return svc().conflicts(user)


@app.post("/v1/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, req: ResolveReq, owner: str = Depends(auth)):
    """采纳一方并撤回另一方，或保留双方。"""
    user = svc().spaces.authorize(owner, req.space_id)
    return svc().resolve_conflict(user, conflict_id, keep=req.keep)


# --- ② 语义图，供 关系图谱 可视化使用 ---
@app.get("/v1/graph")
def graph(
    as_of: float | None = None,
    include_sensitive: bool = False,
    q: str = "",
    live_only: bool = False,
    limit: Optional[int] = None,
    user: SpaceContext = Depends(authorized_space),
):
    """该用户语义图的节点（实体）+ 边（双时间轴关系）。
    默认是可分享的安全图，省略由敏感 fact 支撑的边。仅在明确的所有者图检查场景传
    `include_sensitive=true`。"""
    return svc().graph(
        user,
        as_of=as_of,
        include_sensitive=include_sensitive,
        q=q,
        live_only=live_only,
        limit=limit,
    )


# --- ④ 隐私：完整数据导出（GDPR 式可携带性）；擦除用 POST /v1/forget ---
@app.get('/v1/export')
def export(include_sensitive: bool = False, context: SpaceContext = Depends(authorized_space)):
    stream = svc().iter_export(context, include_sensitive)
    first = next(stream)

    def content():
        try:
            yield first
            yield from stream
        finally:
            stream.close()

    return StreamingResponse(content(), media_type='application/json',
                             headers={'Content-Disposition': 'attachment; filename="mnemotrace_export.json"'})


# --- 在 /ui 下以单页应用形式提供生产控制台（frontend/dist）---
# 所有文件与 SPA 回退共用 StaticFiles 的真实目录边界，避免独立的文件下载出口。


@app.head("/ui")
@app.head("/ui/{path:path}")
@app.get("/ui")
@app.get("/ui/{path:path}")
async def spa(request: Request, path: str = ""):
    if not _spa_built():
        return _console_unavailable()

    # ASGI 已解码 URL。跨平台拒绝 Windows 分隔符、盘符/流和父目录段；不要再次解码。
    if (path.startswith("/") or "\\" in path or ":" in path or "\x00" in path
            or ".." in path.split("/")):
        raise HTTPException(404)
    try:
        root = os.path.realpath(_DIST)
        target = os.path.realpath(os.path.join(root, path))
        inside = os.path.normcase(os.path.commonpath([root, target])) == os.path.normcase(root)
    except (OSError, ValueError):
        raise HTTPException(404) from None
    if not inside:
        # 外部符号链接/junction 必须拒绝，不能伪装成缺失的 SPA 页面而返回首页。
        raise HTTPException(404)

    files = StaticFiles(directory=_DIST, check_dir=False, follow_symlink=False)
    try:
        return await files.get_response(path or "index.html", request.scope)
    except StarletteHTTPException as exc:
        if exc.status_code != 404 or path == "assets" or path.startswith("assets/"):
            raise
    # 仅缺失的页面路由回退；首页自身也经过 StaticFiles 检查，不能越界读取链接目标。
    return await files.get_response("index.html", request.scope)
