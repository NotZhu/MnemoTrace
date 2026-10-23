"""忆痕 Memory Service — 多租户 HTTP API，任何人都可以接入并管理自己的记忆
（AGENTS.md 「代码导航」，服务化/采用层）。每个 API key 对应一个隔离的记忆命名空间；记忆持久化到磁盘，
跨请求和重启均可用。

启动方式：
    pip install "mnemotrace-memory[server]"
    export MNEMOTRACE_API_KEYS="alice:sk-alice-123,bob:sk-bob-456"   # user:key 键值对（开发环境可用 MNEMOTRACE_OPEN=1）
    export MNEMOTRACE_EMBEDDER=hashing          # 零下载默认值；想要更好的本地嵌入可用 bge-small
    export MNEMOTRACE_LLM=deepseek              # 可选：更好的 fact 抽取（需要对应 provider 的 key）
    uvicorn mnemotrace.server.app:app --host 0.0.0.0 --port 8000

然后客户端（curl / SDK / MCP bridge）用 `Authorization: Bearer <key>` 调用：
    POST /v1/remember  {"content": "...", "session_id": "..."}   -> 存储 + 整合
    POST /v1/recall    {"query": "...", "lean": true}            -> 返回一小段检索到的上下文用于回答
    GET  /v1/profile                                             -> 用户的合成画像
    POST /v1/forget {"confirm": true}                            -> 清空该用户的记忆

这是托管产品（类似 Hy-Memory）的基础。默认单节点 + 文件存储——把内存 store 换成
pgvector/Qdrant + Kuzu（已在 store 接口后可插拔），再放到你的鉴权/网关后面，即可扩展为公开服务。
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from typing import Optional

try:
    from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, ConfigDict, Field
    from starlette.exceptions import HTTPException as StarletteHTTPException
except Exception as exc:  # noqa: BLE001
    raise SystemExit("the server needs FastAPI — `pip install \"mnemotrace-memory[server]\"`") from exc

from .. import __version__
from ..memory import InvalidFactEditError
from ..service import MemoryService
from ..spaces import AuthConfigError, Principal, is_valid_api_key, load_spaces, principal_for_token


DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _load_keys() -> dict[str, str]:
    """从 MNEMOTRACE_API_KEYS（'alice:sk-a,bob:sk-b'）解析 API key -> user_id 映射。"""
    out: dict[str, str] = {}
    raw = os.environ.get("MNEMOTRACE_API_KEYS", "").strip()
    if not raw:
        return out
    for index, pair in enumerate(raw.split(","), start=1):
        pair = pair.strip()
        if ":" not in pair:
            raise AuthConfigError(f"MNEMOTRACE_API_KEYS entry {index} must use tenant:key")
        user, key = (part.strip() for part in pair.split(":", 1))
        if not user or not key:
            raise AuthConfigError(f"MNEMOTRACE_API_KEYS entry {index} has an empty tenant or key")
        if not is_valid_api_key(key):
            raise AuthConfigError(f"MNEMOTRACE_API_KEYS entry {index} key must be non-empty printable ASCII")
        previous = out.get(key)
        if previous is not None and previous != user:
            raise AuthConfigError("one API key cannot map to multiple tenants")
        out[key] = user
    return out


def _max_request_bytes() -> int:
    raw = os.environ.get("MNEMOTRACE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuthConfigError("MNEMOTRACE_MAX_REQUEST_BYTES must be a positive integer") from exc
    if value <= 0:
        raise AuthConfigError("MNEMOTRACE_MAX_REQUEST_BYTES must be a positive integer")
    return value


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _bearer_token(authorization: str) -> str:
    """严格解析 Bearer 头。把"缺少鉴权"和"格式错误的鉴权"区分开，既保留本地匿名模式的可能，
    又不会把意外或伪造的 scheme 当作命名空间接受。"""
    # 在 strip 和任何模式分支之前拒绝非法字符，避免隐藏控制字符或进入不支持 Unicode 的比较。
    if any(not (" " <= char <= "~") for char in authorization):
        raise HTTPException(401, "bearer key must use printable ASCII characters")
    value = authorization.strip()
    if not value:
        return ""
    scheme, sep, token = value.partition(" ")
    if scheme.lower() != "bearer" or not sep or not token.strip():
        raise HTTPException(401, "expected Authorization: Bearer <token>")
    return token.strip()


_svc: Optional[MemoryService] = None


def svc() -> MemoryService:
    """共享的多租户核心（每进程一个）。MCP server 和 OpenAI 兼容代理用的是同一个类，
    因此三个入口共享一套实现。"""
    global _svc
    if _svc is None:
        _svc = MemoryService()
    return _svc


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    svc()  # 启动时加载一次 embedder（和 LLM），避免首个请求产生竞态
    yield
    # 先排空排队中的异步 System-2 整合，再停止 worker，保证优雅停机不会丢掉待处理的整合。
    # 默认同步模式下为空操作。
    if _svc is not None:
        _svc.flush()
        _svc.close()


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


@app.middleware("http")
async def request_limits_and_security_headers(request: Request, call_next):
    path = request.url.path
    try:
        max_bytes = _max_request_bytes()
    except AuthConfigError:
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


def auth(authorization: str = Header(default="")) -> str:
    """从 Bearer key 解析调用者的 user_id。开发开放模式直接用 key 文本本身作为命名空间，
    这样无需预先配置 key 即可试用，同时除非显式启用，仍避免共享匿名记忆。"""
    try:
        keys = _load_keys()
    except AuthConfigError as exc:
        raise HTTPException(503, "invalid API key configuration") from exc
    token = _bearer_token(authorization)
    if keys:
        matched_user = None
        for key, user in keys.items():
            if hmac.compare_digest(token, key):
                matched_user = user
        if matched_user is None:
            raise HTTPException(401, "invalid or missing API key")
        return matched_user
    if _env_flag("MNEMOTRACE_OPEN"):
        if token:
            return token
        if _env_flag("MNEMOTRACE_ALLOW_ANONYMOUS"):
            return "anonymous"
        raise HTTPException(
            401,
            "missing bearer namespace: in MNEMOTRACE_OPEN mode send Authorization: Bearer <namespace>, "
            "or set MNEMOTRACE_ALLOW_ANONYMOUS=1 to allow shared anonymous memory",
        )
    raise HTTPException(401, "set MNEMOTRACE_API_KEYS (user:key,...) or MNEMOTRACE_OPEN=1")


def auth_principal(authorization: str = Header(default="")) -> Principal:
    """把调用者解析为 Principal —— 其 home space 加上可读/可写的 space 列表。

    MNEMOTRACE_SPACES 的 key（多 space、常量时间匹配）优先；其余 token 走既有的单 space `auth`，
    因此 MNEMOTRACE_API_KEYS / 开放模式的行为——包括全部加固措施——完全不变，多 space 是纯增量。
    只有感知 space 的路由（remember/recall）依赖它；其余一切仍用普通的 `auth` home 字符串。"""
    token = _bearer_token(authorization)
    try:
        principal = principal_for_token(token)
    except AuthConfigError as exc:
        raise HTTPException(503, "invalid API key configuration") from exc
    if principal is not None:
        return principal
    return Principal.single(auth(authorization))


@app.get("/metrics")
def metrics():
    """实时服务指标（AGENTS.md Bet D online）：每个操作的 p50/p95 延迟、请求计数器，以及
    检索上下文相对全量历史的 token 节省比。只输出聚合数字——不包含命名空间名和用户内容，
    因此可以保持和 /health 一样开放。"""
    return svc().metrics_snapshot()


class RememberReq(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    session_id: str = Field(default="default", min_length=1, max_length=512)
    scope: str = "auto"  # auto（按易逝性路由）| long（强制长期）| working（强制临时）
    space: Optional[str] = Field(default=None, max_length=512)  # 目标命名空间（默认：调用者的 home）


class RecallReq(BaseModel):
    query: str = Field(min_length=1, max_length=32_768)
    lean: bool = True
    n_chunks: int = Field(default=6, ge=0, le=50)
    session_id: Optional[str] = Field(default=None, max_length=512)
    as_of: Optional[float] = None  # epoch 秒：某时间点的记忆视图（as-of）
    redact_sensitive: bool = False  # 在返回的上下文/fact 列表中隐藏被标记为敏感的 fact
    spaces: Optional[list[str]] = Field(default=None, max_length=16)  # 要读取并融合的命名空间（默认：[home]）


class CloseSessionReq(BaseModel):
    session_id: str = Field(default="default", min_length=1, max_length=512)
    summarize: bool = True
    clear_working: bool = True
    # 将 session 蒸馏为 decision/finding/lesson fact。None = 跟随服务端的 session_outcomes
    # 配置（现默认开启）；True/False 允许客户端仅针对这一次关闭选择退出或重新加入蒸馏，而不改变
    # 服务端默认值。这不是绕过运营方成本上限的后门：无论传什么，MNEMOTRACE_SESSION_OUTCOMES=0
    # 都会强制关闭。
    outcomes: Optional[bool] = None


@app.get("/health")
def health():
    service = svc()
    config_error = False
    try:
        keys = _load_keys()
        load_spaces()
        _max_request_bytes()
    except AuthConfigError:
        keys = {}
        config_error = True
    auth_mode = (
        "invalid"
        if config_error
        else ("api_keys" if keys else ("open" if _env_flag("MNEMOTRACE_OPEN") else "disabled"))
    )
    data_ready = os.path.isdir(service.data_dir) and os.access(service.data_dir, os.W_OK)
    ready = auth_mode in {"api_keys", "open"} and data_ready
    return {
        "ok": True,
        "ready": ready,
        "service": "mnemotrace",
        "version": __version__,
        "auth_mode": auth_mode,
        "anonymous_allowed": auth_mode == "open" and _env_flag("MNEMOTRACE_ALLOW_ANONYMOUS"),
        "embedder": service.embedder.__class__.__name__,
        "llm_configured": service.llm is not None,
        "answerer_configured": service.answerer is not None,
        "storage": service.config.storage,
        "users_hot": service.hot_count,
        "max_hot_users": service.max_hot_users,
        "max_hot_facts": service.config.max_hot_facts,
    }


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


@app.post("/v1/remember")
def remember(req: RememberReq, principal: Principal = Depends(auth_principal)):
    # 写入指定的 space（默认：调用者的 home）；ACL 决定一个 key 可以写哪些 space。
    target = req.space or principal.home
    if not principal.can_write(target):
        raise HTTPException(403, f"not allowed to write to space '{target}'")
    # 在 service 内部按易逝性路由：无论走哪条路，带日期的 episode 都会被存储（历史仍可回答）；
    # 临时状态还会进入 working memory，且不会被提升为持久 fact。
    return svc().remember(target, req.content, session_id=req.session_id, scope=req.scope)


@app.post("/v1/recall")
def recall(req: RecallReq, principal: Principal = Depends(auth_principal)):
    spaces = req.spaces or [principal.home]
    for s in spaces:
        if not principal.can_read(s):
            raise HTTPException(403, f"not allowed to read space '{s}'")
    if len(spaces) > 1:
        # 跨 space 读取：融合各 space 的 lean 上下文（agent + 团队 + 用户记忆），并打上来源标签。
        return svc().recall_multi(spaces, req.query, n_chunks=req.n_chunks, session_id=req.session_id,
                                  as_of=req.as_of, redact_sensitive=req.redact_sensitive)
    # answer=True：在 lean 上下文上生成真实答案，并报告 full-context 基线的 token 数，
    # 这样控制台的 问答 视图能同时展示答案和 token 节省。
    return svc().recall(spaces[0], req.query, lean=req.lean, n_chunks=req.n_chunks,
                        session_id=req.session_id, as_of=req.as_of,
                        redact_sensitive=req.redact_sensitive, answer=True)


@app.post("/v1/sessions/close")
def close_session(req: CloseSessionReq, user: str = Depends(auth)):
    """结束一个客户端 session：排空待处理的整合、为摘要建立索引、清空临时 working 状态。

    原始 episode 仍保留以供溯源和未来的 as-of 读取；这是面向 agent 客户端的生命周期/整理钩子，
    不是删除对话记录的端点。
    """
    return svc().close_session(
        user,
        req.session_id,
        summarize=req.summarize,
        clear_working=req.clear_working,
        outcomes=req.outcomes,
    )


@app.get("/v1/sessions")
def sessions(
    limit: Optional[int] = None,
    offset: int = 0,
    q: str = "",
    user: str = Depends(auth),
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
    user: str = Depends(auth),
):
    """审计某个具体 session 向持久记忆贡献了什么。

    敏感 fact 文本默认脱敏。仅在用户明确拥有、且有意展示私有 fact 文本的审计场景下才传
    include_sensitive=true。
    """
    return svc().session_report(user, session_id, include_sensitive=include_sensitive)


@app.get("/v1/profile")
def profile(user: str = Depends(auth)):
    return svc().profile(user)


@app.get("/v1/profile/structured")
def structured_profile(user: str = Depends(auth)):
    """L2 结构化画像：基本信息 + 偏好（按类别）+ 习惯，分为 confirmed 与 待确认 (tentative) 两层。
    仅用于展示的分层——不影响 recall/search 能看到的内容。"""
    return svc().structured_profile(user)


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
    category: Optional[str] = None,
    outcome_kind: Optional[str] = None,
    expected_revision: Optional[str] = None,
    evaluated_at: Optional[float] = None,
    user: str = Depends(auth),
):
    """浏览已存储的记忆。

    默认是可分享的安全视图：非敏感 fact 加不含内容的计数，profile/原始 episode 均不返回。
    仅在明确的所有者检查视图中传 `include_sensitive=true`，此时会包含原始 episode、摘要、
    profile 和敏感 fact。
    `kind=outcomes` 只保留 session 结论（decision/finding/lesson/open_question），
    `kind=attributes` 只保留其余内容；其他值和 `status` 一样被忽略。
    `outcome_kind` 精确筛选四类结论；后续页成对携带 `expected_revision` 和 `evaluated_at`，
    分别固定已提交集合与状态评估时刻。提交版本不符时返回 409。
    `category` 精确筛选事实分类（空分类归入“其他”），在分页前生效；
    `fact_categories` 返回同一事实类型及敏感可见范围内的分类，不受搜索、状态及分页限制。
    """
    from ..service import MemoryRevisionChanged

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
            category=category,
            outcome_kind=outcome_kind,
            expected_revision=expected_revision,
            evaluated_at=evaluated_at,
        )
    except MemoryRevisionChanged as exc:
        raise HTTPException(409, {"code": "memory_revision_changed", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/v1/audit")
def audit(limit: int = 40, user: str = Depends(auth)):
    """记忆健康检查：列出值得修复的条目，每条都带被标记的原因和处理建议。

    个人记忆库赢得信任靠的是可检查，而不是体量大。在真实语料上做抽取必然产生垃圾，
    不逐条阅读 fact 根本发现不了。
    """
    return svc().audit(user, limit=max(1, min(limit, 200)))


@app.get("/v1/stats")
def stats(user: str = Depends(auth)):
    """不含内容的命名空间统计，供 dashboard 和就绪检查使用。与 /v1/memories 不同，它不返回
    profile 文本、fact 文本、episode 片段或存储路径。"""
    return svc().stats(user)


@app.get("/v1/agent/status")
def agent_status(session_id: Optional[str] = None, user: str = Depends(auth)):
    """给 agent 客户端在决定 recall/remember/close 之前查看的不含内容状态。

    返回命名空间/session 计数、focus 策略和推荐的工具调用，不暴露 fact 文本、profile 文字、
    原始 episode、摘要或存储路径。
    """
    return svc().agent_status(user, session_id=session_id)


class ForgetReq(BaseModel):
    confirm: bool = False


@app.post("/v1/forget")
def forget(req: Optional[ForgetReq] = None, confirm: bool = False, user: str = Depends(auth)):
    if user in {"anonymous", "mnemotrace-test"}:
        raise HTTPException(403, "the public demo/anonymous namespace cannot be wiped")
    if not confirm and not (req and req.confirm):
        raise HTTPException(400, "forget requires explicit confirm=true because it permanently erases this namespace")
    return svc().forget(user)


# --- 批量导入：自带历史（ChatGPT 导出 / OpenAI messages / JSONL / 对话记录）---
class ImportReq(BaseModel):
    # 传入已解析的 `sessions`（来自 mnemotrace.connectors.parse / 导入 CLI），或原始 `data` + 一个
    # `format` 由服务端解析。两者同时给出时以 `sessions` 为准。
    sessions: Optional[list] = None
    data: Optional[object] = None
    format: str = "auto"
    # None = 对除 agent-session 对话记录之外的所有来源逐轮抽取 fact（对话记录会先存储，留待
    # close 时蒸馏；对它们做逐轮抽取需要显式 true 且有 LLM）。
    consolidate: Optional[bool] = None
    summarize: bool = True
    dedupe: bool = True  # 幂等的重复导入：跳过已入库的 episode（按内容指纹）


@app.post("/v1/import")
def import_history(req: ImportReq, user: str = Depends(auth)):
    """单次批量摄入外部历史。解析常见导出并 POST 到这里的 CLI 见 `python -m mnemotrace.connectors`。"""
    if req.sessions is None and req.data is None:
        raise HTTPException(400, "provide either 'sessions' (pre-parsed) or 'data' (+ 'format') to import")
    try:
        return svc().import_(user, sessions=req.sessions, data=req.data, format=req.format,
                             consolidate=req.consolidate, summarize=req.summarize, dedupe=req.dedupe)
    except ValueError as exc:
        # 未知/不匹配的 format 是调用者的 payload 问题，不是服务端故障 -> 400，绝不能 500
        raise HTTPException(400, f"could not parse import data: {exc}")


class DocumentReq(BaseModel):
    """上传单个文档/图片作为记忆。可在 `data_base64` 中发送 base64 编码的原始字节
    （PDF/DOCX/图片），或传已抽取的 `text`。图片由视觉模型（MNEMOTRACE_VISION_LLM）生成
    可检索的文字描述。"""
    filename: Optional[str] = Field(default=None, max_length=512)
    content_type: Optional[str] = Field(default=None, max_length=256)
    data_base64: Optional[str] = None  # 原始文件字节，base64 编码（受请求大小上限约束）
    text: Optional[str] = Field(default=None, max_length=1_000_000)  # 或已抽取的纯文本
    session_id: str = Field(default="document", min_length=1, max_length=512)


@app.post("/v1/documents")
def import_document(req: DocumentReq, user: str = Depends(auth)):
    """把 PDF/DOCX/text 文档或图片作为长期记忆摄入（多模态，AGENTS.md 「代码导航」）。"""
    if req.text is not None:
        data: object = req.text
    elif req.data_base64:
        import base64
        try:
            data = base64.b64decode(req.data_base64)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"data_base64 is not valid base64: {exc}")
    else:
        raise HTTPException(400, "provide 'data_base64' (raw file bytes) or 'text'")
    return svc().import_document(user, data, filename=req.filename, content_type=req.content_type,
                                 session_id=req.session_id)


# --- OpenAI 兼容 chat，透明叠加记忆（即插即用：把 OpenAI 客户端的 base_url 指到这里）--
class ChatCompletionReq(BaseModel):
    model_config = ConfigDict(extra="allow")  # 接受（并忽略）temperature/top_p 等参数。
    model: str = "mnemotrace"
    messages: list[dict] = []
    stream: bool = False
    # 忆痕 扩展：recall/remember/session_id/scope/n_chunks/as_of/redact_sensitive。
    memory: Optional[dict] = None


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionReq, background: BackgroundTasks, user: str = Depends(auth)):
    """OpenAI 兼容的 chat completions，叠加长期记忆：为最新一条 user 消息检索相关片段、注入
    上下文、用配置的 LLM 生成，然后在关键路径之外记住这一轮。通过 body.memory={"recall":false}、
    {"remember":false}、{"session_id":"..."}、{"scope":"working"}、{"as_of":...} 或
    {"redact_sensitive":true} 控制记忆层。"""
    from . import openai_compat as oc

    if not req.messages:
        raise HTTPException(400, "messages must be a non-empty list")
    opts = req.memory or {}
    session_id = str(opts.get("session_id") or "default")
    scope = str(opts.get("scope") or "auto")
    body = req.model_dump()
    try:
        resp = oc.chat_completion(
            svc(),
            user,
            body,
            n_chunks=int(opts.get("n_chunks", 6)),
            do_recall=opts.get("recall", True),
            as_of=opts.get("as_of"),
            redact_sensitive=bool(opts.get("redact_sensitive", False)),
            session_id=session_id,
        )
    except oc.NoLLMConfigured as exc:
        raise HTTPException(503, str(exc))

    # 记住本轮文本 + 任意图片部分的描述（多模态）；没有图片/描述器时与纯文本完全一致，
    # 纯文本的即插即用路径不受影响。
    user_text = oc.remembered_text(svc().captioner, body.get("messages", []))
    if opts.get("remember", True) and user_text:
        background.add_task(
            svc().remember,
            user,
            user_text,
            session_id=session_id,
            scope=scope,
        )  # 在响应路径之外写入（System-2）
        resp["mnemotrace"]["remembered"] = True
        resp["mnemotrace"]["remember_scope"] = scope

    if req.stream:
        from fastapi.responses import StreamingResponse
        return StreamingResponse(oc.iter_sse(resp), media_type="text/event-stream")
    return resp


# --- 用户自写的记忆管理（可编辑层；用户断言具有最高优先级）---
class FactReq(BaseModel):
    subject: str = "user"
    predicate: str
    object: str
    sensitive: Optional[bool] = None
    category: Optional[str] = None


class FactEdit(BaseModel):
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    sensitive: Optional[bool] = None  # 用户对自动敏感标记的覆盖（⑤）
    category: Optional[str] = None
    statement: Optional[str] = Field(default=None, min_length=1)


@app.post("/v1/facts")
def add_fact(req: FactReq, user: str = Depends(auth)):
    """手动添加一条你断言的 fact——它是权威的，不会被自动覆盖。"""
    return svc().add_fact(
        user,
        req.subject,
        req.predicate,
        req.object,
        sensitive=req.sensitive,
        category=req.category,
    )


@app.patch("/v1/facts/{fact_id}")
def edit_fact(fact_id: str, req: FactEdit, user: str = Depends(auth)):
    """修改 fact 内容后变为用户撰写；仅修改敏感标记或分类时保留原来源。"""
    if req.statement is not None and (not req.statement.strip() or
            any(value is not None for value in (req.subject, req.predicate, req.object))):
        raise HTTPException(422, "statement must not be blank or combined with subject, predicate or object")
    try:
        result = svc().update_fact(user, fact_id, subject=req.subject, predicate=req.predicate,
                                   object=req.object, sensitive=req.sensitive, category=req.category,
                                   statement=req.statement)
    except InvalidFactEditError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "fact not found")
    return result


@app.delete("/v1/facts/{fact_id}")
def remove_fact(fact_id: str, user: str = Depends(auth)):
    """被遗忘权：永久删除单个 fact。"""
    return svc().delete_fact(user, fact_id)


class ClearSlotReq(BaseModel):
    subject: str
    predicate: str
    # 所有者批准时看到的计数。必填：不受保护的批量删除是这个端点绝不能提供的形态，
    # 因此不提供任何入口。
    expect_count: int


# 用 POST 而不是 DELETE /v1/facts/{slot}：slot 是 (subject, predicate) 对，若作为路径段路由
# 会与 /v1/facts/{fact_id} 冲突。
@app.post("/v1/facts/clear-slot")
def clear_slot(req: ClearSlotReq, user: str = Depends(auth)):
    """处理 audit 中的 slot_overflow 发现：清空被塞进单个单值 slot 的所有 fact。"""
    if not req.subject.strip() or not req.predicate.strip():
        raise HTTPException(400, "subject and predicate are both required")
    return svc().clear_slot(user, req.subject, req.predicate, expect_count=req.expect_count)


# --- ③ focus 领域：自定义记忆强调（track）或抑制（mute）的内容 ---
class FocusReq(BaseModel):
    track: Optional[list[str]] = None  # None = 不变；[] = 清空
    mute: Optional[list[str]] = None


@app.get("/v1/focus")
def get_focus(user: str = Depends(auth)):
    return svc().get_focus(user)


@app.put("/v1/focus")
def put_focus(req: FocusReq, user: str = Depends(auth)):
    """设置用户跟踪/静音的主题。被跟踪的主题显著度提升（排名更靠前、保持活跃）；
    被静音的主题在 recall 和 profile 中被隐藏。"""
    return svc().set_focus(user, track=req.track, mute=req.mute)


# --- 记忆策略：可编辑的 prompt + "记录什么"指令（记忆策略 页面）---
class PolicyReq(BaseModel):
    extract_instruction: Optional[str] = None  # None = 不变；"" = 重置为默认
    extract_system: Optional[str] = None
    summary_system: Optional[str] = None
    persona_system: Optional[str] = None


@app.get("/v1/policy")
def get_policy(user: str = Depends(auth)):
    """用户的 prompt 覆盖项以及内置默认值（控制台可以查看/编辑任意一方）。"""
    return svc().get_policy(user)


@app.put("/v1/policy")
def put_policy(req: PolicyReq, user: str = Depends(auth)):
    """设置可编辑的抽取/摘要/persona prompt 和"记录什么"指令。在下一次 remember()/
    整合时生效。"""
    return svc().set_policy(user, **req.model_dump())


# --- ① working memory：临时的、按 session/TTL 作用域的状态，不进入长期记忆 ---
class WorkingReq(BaseModel):
    content: str
    session_id: str = "default"
    kind: str = "state"  # state | intent | schedule | note | ...
    ttl_seconds: Optional[float] = None  # 硬性过期；None => 存活到 session 被清空


@app.post("/v1/working")
def add_working(req: WorkingReq, user: str = Depends(auth)):
    """存储一个临时条目（不会被整合进长期记忆或 profile）。"""
    return svc().add_working(user, req.content, session_id=req.session_id, kind=req.kind,
                             ttl_seconds=req.ttl_seconds)


@app.get("/v1/working")
def list_working(session_id: Optional[str] = None, user: str = Depends(auth)):
    """存活的 working-memory 条目（可选按 session 过滤）。已过期/已消费的不包含。"""
    return svc().working_memory(user, session_id=session_id)


@app.delete("/v1/working")
def clear_working(session_id: str, user: str = Depends(auth)):
    """session 结束/重启时的清空：删除该 session 的 working memory。"""
    return svc().clear_working(user, session_id)


# --- 疑似冲突（System-2 中由 LLM 检出，需用户确认；绝不自动解决）---
class ResolveReq(BaseModel):
    keep: str = "newer"  # newer | older | both(=撤销)


@app.get("/v1/conflicts")
def conflicts(user: str = Depends(auth)):
    """等待用户裁决的疑似冲突（绝不自动解决）。需要启用 System-2 LLM 检测器
    （MNEMOTRACE_CONFLICT_DETECTION=1）。"""
    return svc().conflicts(user)


@app.post("/v1/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str, req: ResolveReq, user: str = Depends(auth)):
    """执行用户的裁决：保留较新/较旧（失效化另一方）或 both（撤销）。"""
    return svc().resolve_conflict(user, conflict_id, keep=req.keep)


# --- ② 语义图，供 关系图谱 可视化使用 ---
@app.get("/v1/graph")
def graph(
    as_of: float | None = None,
    include_sensitive: bool = False,
    q: str = "",
    live_only: bool = False,
    limit: Optional[int] = None,
    user: str = Depends(auth),
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
@app.get("/v1/export")
def export(include_sensitive: bool = False, user: str = Depends(auth)):
    """以 JSON 下载该用户的记忆（数据可携带）。

    默认是可分享的安全结构化导出：非敏感 fact 及其图，省略自由文本的 profile/episode/摘要。
    仅在明确的完整私有导出场景传 `include_sensitive=true`，此时包含每个 fact 的双时间轴
    时间戳 + 溯源、原始 episode、摘要、profile 和 focus。
    """
    from fastapi.responses import JSONResponse

    data = svc().export(user, include_sensitive=include_sensitive)
    fname = f"mnemotrace_{''.join(c for c in user if c.isalnum()) or 'me'}_export.json"
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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
