"""MnemoTrace MCP tools for authorized memory spaces."""
# NOTE: 这里不要加 `from __future__ import annotations` —— FastMCP 会在运行时内省这些工具签名
# （对每个参数的注解做 issubclass 检查），字符串化注解会破坏它。

import json
from contextvars import ContextVar
from enum import Enum
from typing import Annotated, Literal, Optional

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from pydantic import Field, StrictBool
except Exception as exc:  # noqa: BLE001
    raise SystemExit('the MCP server needs the MCP SDK — `pip install "mnemotrace-memory[mcp]"`') from exc

from .backends import Backend, make_backend
from ..categories import Category, ObjectKind
from ..types import Cardinality, Retention
from ..store.contracts import FactState

INSTRUCTIONS = (
    "MnemoTrace is the user's long-term memory. Use `mnemotrace_recall` BEFORE answering anything that may "
    "depend on what you've learned about the user in past sessions (their preferences, projects, "
    "people, decisions) — it returns a small, dated, relevant context to ground your answer. Use "
    "`mnemotrace_remember` whenever the user states a durable fact, preference, or decision worth keeping "
    "with extraction enabled by default. "
    "Keep temporary execution state in the agent's task tools or files. "
    "Use `mnemotrace_flush_processing` with the session ID to finish core processing; "
    "use `mnemotrace_session_report` when the user asks what that session "
    "saved; use `mnemotrace_list_sessions` when the user asks which agent/app sessions touched memory. "
    "Use `mnemotrace_aggregate` for counts and `mnemotrace_follow_path` for ordered relations. "
    "`mnemotrace_search` gives a single direct answer; `mnemotrace_agent_status` checks the current namespace, "
    "session, focus, and next memory actions without exposing memory contents; `mnemotrace_stats` checks "
    "content-free memory health; "
    "`mnemotrace_list_facts` / `mnemotrace_profile` browse what's stored; `mnemotrace_add_fact` records an "
    "authoritative fact; `mnemotrace_update_fact` corrects a specific fact; `mnemotrace_delete_fact` deletes "
    "one specific fact; `mnemotrace_get_focus` / `mnemotrace_set_focus` inspect and update the user's tracked "
    "or muted memory topics; `mnemotrace_export` gives the "
    "user a portable memory export; `mnemotrace_forget` erases everything (irreversible)."
)

class MemoryMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict):
        if name in {"mnemotrace_remember", "mnemotrace_remember_messages"}:
            # FastMCP drops unknown arguments and can parse JSON strings before validation.
            if "scope" in arguments:
                raise ToolError("Use the boolean extract argument")
            if "mode" in arguments:
                raise ToolError("mode is no longer supported")
            if "extract" in arguments and type(arguments["extract"]) is not bool:
                raise ToolError("extract must be a boolean")
        return await super().call_tool(name, arguments)


mcp = MemoryMCP("mnemotrace_mcp", instructions=INSTRUCTIONS)

# --- backend 单例（测试/嵌入场景可覆盖） ------------------
_backend: Optional[Backend] = None
request_backend: ContextVar[Backend | None] = ContextVar("request_backend", default=None)
require_request_backend = False


def backend() -> Backend:
    global _backend
    if current := request_backend.get():
        return current
    if require_request_backend:
        raise RuntimeError('An authorized request scope is required')
    if _backend is None:
        _backend = make_backend()
    return _backend


def set_backend(b: Optional[Backend]) -> None:
    """注入一个 backend（测试，或把服务器嵌入其他进程）。"""
    global _backend
    _backend = b


# --- 共享格式化 + 错误辅助函数 ----------------------------------------------
class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _err(e: Exception) -> str:
    """可操作、不泄露内部信息的错误文案（最佳实践：引导 agent 修复）。"""
    from ..extraction import ExtractionError
    from ..service import ProcessingFailure
    from ..store.postgres.errors import DatabaseError

    if isinstance(e, ProcessingFailure):
        return "Error: " + _json(e.detail())
    if isinstance(e, DatabaseError):
        return "Error: " + _json(dict(code=e.code, message='Memory database operation failed'))
    if isinstance(e, ExtractionError):
        return "Error: " + _json(e.detail())
    try:
        import httpx

        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            try:
                detail = e.response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            # 服务端对抽取/冲突/校验错误一律返回结构化 detail（含 code、stored_raw、episode_ids
            # 或字段原因）。原实现只对 502/503 的固定 code 白名单保留 detail，导致 409/422/500
            # 被降级为裸状态码，远程 agent 无法区分 message_conflict 与 extraction_conflict。
            # 只要 detail 是带 code 的对象就如实透传，其余状态码的文案保持原有引导。
            if isinstance(detail, dict) and isinstance(detail.get("code"), str):
                return "Error: " + _json(detail)
            if code == 401:
                return ("Error: the MnemoTrace server rejected the credentials (401). Check MNEMOTRACE_API_KEY "
                        "matches a configured key on the server.")
            if code == 404:
                return "Error: the MnemoTrace server has no such resource (404)."
            if code == 400:
                return f"Error: the MnemoTrace server rejected the request (400): {e.response.text[:200]}"
            return f"Error: the MnemoTrace server returned HTTP {code}."
        if isinstance(e, httpx.ConnectError):
            return ("Error: could not reach the MnemoTrace server at MNEMOTRACE_API_URL. Is it running "
                    "(uvicorn mnemotrace.server.app:app)?")
    except Exception:  # noqa: BLE001 —— 纯本地模式下可能没有 httpx
        pass
    return "Error: Memory tool request failed."


def _format_feed(feed: dict) -> str:
    """记忆上次由本机 agent session 投喂的时间——这一行能区分空记忆和健康记忆。"""
    if not feed.get("last_fed_at_h"):
        return "- Last fed: no agent-session feed recorded."
    return (f"- Last fed: {feed.get('last_fed_at_h')} · {feed.get('sessions', 0)} session(s) · "
            f"{feed.get('conclusions', 0)} conclusion(s)")


def _format_focus(focus: dict) -> str:
    track = focus.get("track") or []
    mute = focus.get("mute") or []
    track_s = ", ".join(track) if track else "(none)"
    mute_s = ", ".join(mute) if mute else "(none)"
    return f"- Track: {track_s}\n- Mute: {mute_s}"


# 注解预设
_READ = dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_WRITE = dict(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_DESTRUCTIVE = dict(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
@mcp.tool(name="mnemotrace_recall", annotations=ToolAnnotations(title="Recall relevant memory", **_READ))
async def mnemotrace_recall(
    query: Annotated[str, Field(description="What you want to remember about the user / past sessions, "
                                "e.g. 'the user's dietary restrictions' or 'what we decided about the API'.",
                                min_length=1)],
    max_digests: Annotated[int, Field(description="How many source digests to include "
                                      "(0 = facts only). Default 20.", ge=0, le=50)] = 20,
    as_of: Annotated[Optional[float], Field(description="Optional epoch seconds for a point-in-time memory "
                                           "view, e.g. what was believed before a later update.")] = None,
    redact_sensitive: Annotated[bool, Field(description="When true, omit facts tagged sensitive from the "
                                            "returned context.")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    state: FactState = FactState.CURRENT,
    valid_at: float | None = None,
    known_at: float | None = None,
    answer: bool = False,
) -> str:
    """检索一小段相关、带日期的用户长期记忆，用于锚定你的回答。

    这是主要的读取工具——在回答任何可能依赖既往 session 的问题前先调用它。返回用户的
    profile + 最相关的带日期 fact + session 摘要 + 少量完整对话，已做 token 预算控制
    （不是整个历史）。读取返回的上下文并据此回答；如果为空，说明记忆中没有该主题的内容。

    返回（markdown）：可读取的上下文块，附近似 token 数。
    返回（json）：{"context": str, "tokens_est": int, "as_of": float|null, "redacted_sensitive": bool}。
    """
    try:
        data = await backend().recall(
            query,
            n_digests=max_digests,
            lean=True,
            answer=answer,
            as_of=as_of,
            redact_sensitive=redact_sensitive, state=state, valid_at=valid_at, known_at=known_at,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    outcome = ""
    if answer:
        outcome = f"**Answer status:** {data['answer_status']}\n\n"
        if data.get("answer"):
            outcome += f"**Answer:** {data['answer']}\n\n"
    ctx = (data.get("context") or "").strip()
    if not ctx:
        return outcome + "No relevant memory found for that query."
    return outcome + f"Relevant memory (~{data.get('tokens_est', '?')} tokens):\n\n{ctx}"


@mcp.tool(name="mnemotrace_aggregate", annotations=ToolAnnotations(title="Count structured facts", **_READ))
async def mnemotrace_aggregate(
    predicate: Annotated[str, Field(min_length=1, max_length=512)],
    operation: Literal['count', 'count_distinct'] = 'count',
    subject: str | None = None,
    start: float | None = None,
    end: float | None = None,
    query: str | None = None,
    state: FactState = FactState.CURRENT,
    valid_at: float | None = None,
    known_at: float | None = None,
    redact_sensitive: bool = False,
    limit: Annotated[int, Field(ge=1, le=1000)] = 10,
) -> str:
    """Count facts with an explicit predicate. Use count_distinct for distinct object values.
    Provide optional valid-time bounds [start, end) in epoch seconds. If only original
    text supports the question, pass a query and treat source candidates as incomplete evidence.
    """
    try:
        return _json(await backend().aggregate_facts(
            predicate, operation=operation, subject=subject, start=start, end=end, query=query,
            state=state, valid_at=valid_at, known_at=known_at, redact_sensitive=redact_sensitive, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(name="mnemotrace_follow_path", annotations=ToolAnnotations(title="Follow explicit fact relations", **_READ))
async def mnemotrace_follow_path(
    anchor: Annotated[str, Field(min_length=1, max_length=512)],
    predicates: Annotated[list[str], Field(min_length=2, max_length=4)],
    state: FactState = FactState.CURRENT,
    valid_at: float | None = None,
    known_at: float | None = None,
    redact_sensitive: bool = False,
    limit: Annotated[int, Field(ge=1, le=1000)] = 20,
) -> str:
    """Follow two to four outbound predicates in order, for example sister then works_at.
    Use an exact entity name. Each result includes supporting facts.
    """
    try:
        return _json(await backend().follow_path(anchor, predicates, state=state, valid_at=valid_at,
                                                known_at=known_at, redact_sensitive=redact_sensitive, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(name="mnemotrace_get_episode", annotations=ToolAnnotations(title="Read original event", **_READ))
async def mnemotrace_get_episode(
    episode_id: Annotated[str, Field(description="Event ID returned by recall; reads its full original.",
                                   min_length=1, max_length=512)],
    space_id: Annotated[Optional[str], Field(description="Source space shown beside the event ID.",
                                         max_length=512)] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Explicitly read a source event. Recall does not expand summarized events automatically."""
    try:
        data = await backend().get_episode(episode_id, space_id)
    except Exception as exc:  # noqa: BLE001 -- shared sanitized adapter errors
        return _err(exc)
    if data is None:
        return "Error: episode not found."
    if response_format == ResponseFormat.JSON:
        return _json(data)
    return (f"Event {data['id']} (space: {data['space_id']}; session: {data['session_id']}; "
            f"event_time: {data['event_time']}; ingested_at: {data['ingested_at']})\n\n{data['content']}")


@mcp.tool(name="mnemotrace_search", annotations=ToolAnnotations(title="Search memory evidence", **_READ))
async def mnemotrace_search(
    query: Annotated[str, Field(description="A direct factual question, e.g. 'Where does the user work?'",
                                min_length=1)],
    as_of: Annotated[Optional[float], Field(description="Optional epoch seconds for a point-in-time memory "
                                           "view, e.g. answer as of a past date.")] = None,
    redact_sensitive: Annotated[bool, Field(description="When true, omit facts tagged sensitive and abstain "
                                            "instead of returning sensitive answers.")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    state: FactState = FactState.CURRENT,
    valid_at: float | None = None,
    known_at: float | None = None,
    subject: str | None = None,
    predicate: str | None = None,
) -> str:
    """Retrieve candidate facts and sources. An explicit subject and predicate
    can return a unique value from the selected time view.
    """
    try:
        data = await backend().recall(query, lean=False, as_of=as_of, redact_sensitive=redact_sensitive,
                                      state=state, valid_at=valid_at, known_at=known_at,
                                      subject=subject, predicate=predicate)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    lines = [f"**Status:** {data['status']}"]
    if data.get("answer"):
        lines.append(f"**Answer:** {data['answer']}")
    facts = data.get("facts") or []
    if facts:
        lines += ["", "Candidate facts:"] + [f"- {t}" for t in facts]
    lines.extend(f"- [source {ep['id']}] {ep['text']}" for ep in data.get("sources", ()))
    lines.extend(f"- [source {source_id}]" for source_id in data.get("source_ids", ()))
    return "\n".join(lines)


@mcp.tool(name='mnemotrace_remember_messages', annotations=ToolAnnotations(title='Remember dialogue messages', **_WRITE))
async def mnemotrace_remember_messages(messages: list[dict], session_id: str,
        processing: Literal['immediate', 'buffered'] = 'buffered',
        extract: StrictBool = True, agent_id: str | None = None) -> str:
    """Store complete user/assistant messages with required stable message_id, role and content.

    event_time may be unknown. Reuse IDs on retransmission; replays reuse their existing processing operations.
    Acceptance persists raw sources and tasks; inspect processing status or flush for completion.
    """
    try:
        return _json(await backend().remember_messages(messages, session_id, processing, extract, agent_id=agent_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool(name="mnemotrace_remember", annotations=ToolAnnotations(title="Remember a memory", **_WRITE))
async def mnemotrace_remember(
    content: Annotated[str, Field(description="The message / fact / preference to store, in natural "
                                  "language. e.g. 'I just moved to Berlin and started a new job at Acme.'",
                                  min_length=1)],
    session_id: Annotated[str, Field(description="Optional conversation id to group related turns.")] = "default",
    extract: Annotated[StrictBool, Field(description="Extract reusable facts from the saved source.")] = True,
    processing: Literal['immediate', 'buffered'] = 'immediate',
    agent_id: str | None = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """把内容存入用户长期记忆并整合（抽取原子双时间轴 fact、与已有认知解决矛盾、更新知识图）。

    当用户说出值得跨 session 记住的持久 fact、偏好、计划或决定时调用。临时性的当前 session
    状态由 agent 的任务工具管理。存储与旧值矛盾的值不会删除历史——旧 fact 被失效化并保留
    （as-of 查询依然可用）。extract=True 需要配置 LLM；抽取失败会返回可识别错误并保留原文供重试。
    """
    try:
        data = await backend().remember(content, session_id=session_id, extract=extract, processing=processing, agent_id=agent_id)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    if not extract:
        return 'Saved raw history.'
    operation_ids = data.get('operation_ids', ())
    progress = 'Accepted for processing.' if data.get('status') == 'accepted' else 'Remembered.'
    return progress + (f" Operations: {', '.join(operation_ids)}" if operation_ids else '')


@mcp.tool(name='mnemotrace_processing_status', annotations=ToolAnnotations(title='Memory processing status', **_READ))
async def mnemotrace_processing_status(session_id: Optional[str] = None) -> str:
    """Inspect buffered processing without returning raw content or model credentials."""
    try:
        return _json(await backend().processing_status(session_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool(name='mnemotrace_flush_processing', annotations=ToolAnnotations(title='Flush memory processing', **_WRITE))
async def mnemotrace_flush_processing(session_id: Optional[str] = None, timeout: float = 30,
                                     retry_failed: bool = False, include_digests: bool = False) -> str:
    """Process the currently accepted boundary; retry_failed explicitly retries failed/cancelled work."""
    try:
        return _json(await backend().flush_processing(session_id, timeout, retry_failed, include_digests))
    except Exception as exc:
        return _err(exc)


@mcp.tool(name='mnemotrace_get_operation', annotations=ToolAnnotations(title='Read memory operation', **_READ))
async def mnemotrace_get_operation(operation_id: str) -> str:
    try:
        result = await backend().operation(operation_id)
        return _json(result) if result is not None else 'Error: operation not found.'
    except Exception as exc:
        return _err(exc)


@mcp.tool(name='mnemotrace_retry_operation', annotations=ToolAnnotations(title='Retry memory operation', **_WRITE))
async def mnemotrace_retry_operation(operation_id: str) -> str:
    try:
        result = await backend().operation(operation_id, retry=True)
        return _json(result) if result is not None else 'Error: operation not found.'
    except Exception as exc:
        return _err(exc)


@mcp.tool(name="mnemotrace_session_report", annotations=ToolAnnotations(title="Audit a memory session", **_READ))
async def mnemotrace_session_report(
    session_id: Annotated[str, Field(description="Conversation/thread id to audit.", min_length=1)],
    include_sensitive: Annotated[
        bool,
        Field(description="When false (default), redact sensitive fact text from the report."),
    ] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """展示某个具体 session 向记忆贡献了哪些持久 fact。

    当用户问"你从这个 session 存了什么？"或刚关闭 session 想做可见审计时使用。敏感 fact 默认
    脱敏；仅当用户明确要求完整私有审计时才设 include_sensitive=true。

    返回（json）：{"ok","session_id","episodes","facts_added","facts_redacted","facts":[...]}。
    """
    try:
        data = await backend().session_report(
            session_id=session_id,
            include_sensitive=include_sensitive,
            limit=limit,
            offset=offset,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    facts = data.get("facts") or []
    lines = [
        "# MnemoTrace session report",
        "",
        f"- Session: `{data.get('session_id', session_id)}`",
        f"- Episodes: {data.get('episodes', 0)} "
        f"({data.get('episodes_pending', 0)} pending consolidation)",
        f"- Extraction failures: {data.get('extraction_failures', 0)} episode(s)",
        f"- Durable facts linked to this session: {data.get('facts_added', 0)}",
        f"- Sensitive facts redacted: {data.get('facts_redacted', 0)}",
    ]
    if not facts and not data.get("facts_added"):
        lines += ["", "No durable facts are linked to this session yet."]
        return "\n".join(lines)
    lines += ["", "Facts:"]
    for f in facts:
        tag = " _(superseded)_" if f.get("status") != "live" else ""
        redacted = " 🔒 redacted" if f.get("redacted") else ""
        lines.append(f"- `{f.get('id', '?')}` [{f.get('valid_at', '?')}] {f.get('text', '')}{tag}{redacted}")
    if (next_offset := (data.get("facts_page") or {}).get("next_offset")) is not None:
        lines.append(f"\nFor more facts, call this tool with offset={next_offset}.")
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_list_sessions", annotations=ToolAnnotations(title="List memory sessions", **_READ))
async def mnemotrace_list_sessions(
    limit: Annotated[int, Field(description="Max sessions to return. Default 20.", ge=1, le=100)] = 20,
    offset: Annotated[int, Field(description="Sessions to skip (pagination).", ge=0)] = 0,
    q: Annotated[str, Field(description="Optional substring filter over session ids, e.g. 'codex'.")] = "",
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """列出不含内容的跨 agent/app session 索引。

    当用户询问哪些 Codex / Claude Code / app session 触碰过其记忆时使用。只返回 session id、
    时间戳和计数——不含原始 episode 文本、摘要、profile 文字或 fact 文本。审计某个 session 的
    持久 fact 用 `mnemotrace_session_report(session_id)`。

    返回（json）：{"ok","sessions":[...],"page":...}。
    """
    try:
        data = await backend().sessions(limit=limit, offset=offset, q=q)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    sessions = data.get("sessions") or []
    page = data.get("page") or {}
    total = page.get("total", len(sessions))
    if not sessions:
        return "No memory sessions found." if not q else f"No memory sessions found matching `{q}`."
    start = offset + 1
    lines = [f"# Memory sessions ({start}–{offset + len(sessions)} of {total})", ""]
    for row in sessions:
        sid = row.get("id", "?")
        parts = [
            f"{row.get('episodes', 0)} episode(s)",
            f"{row.get('facts_added', 0)} fact(s)",
        ]
        if row.get("episodes_pending", 0):
            parts.append(f"{row.get('episodes_pending')} pending")
        if row.get("facts_sensitive", 0):
            parts.append(f"{row.get('facts_sensitive')} sensitive")
        last = row.get("last_event_at_h") or "no timestamp"
        lines.append(f"- `{sid}` — {', '.join(parts)}; last: {last}")
    nxt = page.get("next_offset")
    if nxt is not None:
        lines += ["", f"…more sessions — call again with offset={nxt}."]
    lines += ["", "Use `mnemotrace_session_report(session_id=...)` to audit one session."]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_list_facts", annotations=ToolAnnotations(title="List stored facts", **_READ))
async def mnemotrace_list_facts(
    limit: Annotated[int, Field(description="Max facts to return. Default 20.", ge=1, le=100)] = 20,
    offset: Annotated[int, Field(description="Facts to skip (pagination).", ge=0)] = 0,
    include_superseded: Annotated[bool, Field(description="Also show old facts that were superseded by a "
                                              "newer value (the bi-temporal history).")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    categories: Annotated[list[Category] | None, Field(description="Match any of these fact labels.")] = None,
) -> str:
    """浏览 忆痕 存储的关于用户的原子 fact（分页）。

    每个 fact 带日期（生效时间）并标记为 live 或 superseded；用户断言的 fact 带 🔒。
    可用于审计已知内容，或在修正前找到 fact id。

    返回（json）：{"total","count","offset","has_more","next_offset","facts":[...]}。
    """
    try:
        dump = await backend().memories(categories=categories, limit=limit, offset=offset,
                                        include_superseded=include_superseded)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    result = dump['facts_page']
    page, total, nxt = result['items'], result['total'], result['next_offset']
    if response_format == ResponseFormat.JSON:
        return _json({"total": total, "count": len(page), "offset": offset,
                      "has_more": nxt is not None, "next_offset": nxt, "facts": page})
    if not page:
        return "No facts stored yet." if total == 0 else "No facts on this page."
    lines = [f"# Stored facts ({offset + 1}–{offset + len(page)} of {total})", ""]
    for f in page:
        tag = "" if f.get("status") == "live" else " _(superseded)_"
        lock = " 🔒" if f.get("source") == "user" else ""
        lines.append(f"- `{f.get('id', '?')}` [{f.get('valid_at', '?')}] {f.get('text', '')}{tag}{lock}"
                     f" · categories: {', '.join(f.get('categories', [])) or '—'}")
    if nxt is not None:
        lines += ["", f"…{total - nxt} more — call again with offset={nxt}."]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_profile", annotations=ToolAnnotations(title="Read cached user profile", **_READ))
async def mnemotrace_profile(response_format: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """Read the cached fact-backed profile without calling a model."""
    try:
        data = await backend().profile()
        if response_format == ResponseFormat.JSON:
            return _json(data)
        from ..profile import render
        status = (f"Cache version: {data['cache_version']}\n"
                  f"Updated at: {data['updated_at']}\n"
                  f"Pending fact changes: {data['pending_fact_count']}\n"
                  f"Needs refresh: {data['needs_refresh']}")
        if data['operation_id']:
            status += f"\nRefresh operation: {data['operation_id']} ({data['refresh_state']})"
        if data['refresh_error']:
            status += f"\nRefresh error: {data['refresh_error']}"
        return "# User profile\n\n" + status + "\n\n" + (render(data) or "(Profile has no visible entries)")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(name="mnemotrace_refresh_profile", annotations=ToolAnnotations(title="Refresh user profile", **_WRITE))
async def mnemotrace_refresh_profile() -> str:
    """Explicitly request a model-generated profile refresh; inspect its operation ID for progress."""
    try:
        return _json(await backend().refresh_profile())
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(name="mnemotrace_get_focus", annotations=ToolAnnotations(title="Get memory focus", **_READ))
async def mnemotrace_get_focus(response_format: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """查看用户的 focus 策略：被跟踪的主题在检索/显著度中被加权，被静音的主题在
    recall/profile 上下文中被抑制。

    返回（json）：{"track": [str, ...], "mute": [str, ...]}。
    """
    try:
        focus = await backend().get_focus()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(focus)
    return "# Memory focus\n\n" + _format_focus(focus)


@mcp.tool(name="mnemotrace_set_focus", annotations=ToolAnnotations(title="Set memory focus", **_WRITE))
async def mnemotrace_set_focus(
    track: Annotated[Optional[list[str]], Field(description="Topics MnemoTrace should emphasize. Omit to leave "
                                                 "unchanged; pass [] to clear tracked topics.")] = None,
    mute: Annotated[Optional[list[str]], Field(description="Topics MnemoTrace should suppress from recall/profile. "
                                                "Omit to leave unchanged; pass [] to clear muted topics.")] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """更新用户的记忆 focus 策略。

    当用户说"多关注我的项目决定"或"除非我问起，否则别提健康细节"时使用。`track` 的主题获得
    显著度加成；`mute` 的主题在组装的 recall/profile 上下文中隐藏。传 None 表示该列表不变；
    传 [] 清空。

    返回（json）：{"ok": true, "focus": {"track": [...], "mute": [...]}}。
    """
    if track is None and mute is None:
        return "Error: provide `track` or `mute`. Pass [] to clear a list."
    try:
        data = await backend().set_focus(track=track, mute=mute)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    focus = data.get("focus") or {}
    return "# Updated memory focus\n\n" + _format_focus(focus)


@mcp.tool(name="mnemotrace_agent_status", annotations=ToolAnnotations(title="Get agent memory status", **_READ))
async def mnemotrace_agent_status(
    session_id: Annotated[Optional[str], Field(description="Optional current conversation/thread id. "
                                           "When set, session-specific counts are included.")] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """查看本 agent session 的不含内容记忆契约。

    启动时或排查 忆痕 是否正确接入时使用。它不返回 profile 文本、fact 文本、摘要或原始
    episode；只返回命名空间/session 计数、focus 策略和推荐的下一步工具调用。

    返回（json）：{"ok","user","session_id","focus","session","counts","recommended_next_actions",...}。
    """
    try:
        data = await backend().agent_status(session_id=session_id)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    c = data.get("counts") or {}
    s = data.get("session") or {}
    focus = data.get("focus") or {}
    lines = [
        "# MnemoTrace agent status",
        "",
        f"- Space: `{data.get('space_id', '?')}`",
        f"- Session: `{s.get('id') or '(none supplied)'}`",
        f"- Session episodes: {s.get('episodes', 0)} "
        f"({s.get('episodes_pending', 0)} pending consolidation)",
        f"- Session extraction failures: {s.get('extraction_failures', 0)} episode(s)",
        f"- Overall extraction failures: {c.get('extraction_failures', 0)} episode(s)",
        f"- Long-term memory: {c.get('facts_live', 0)} live fact(s), "
        f"{c.get('digests', 0)} digest(ies), {c.get('pending_conflicts', 0)} pending conflict(s)",
        f"- Consolidation backlog: {'yes' if data.get('consolidation_backlog') else 'no'}",
        _format_feed(data.get("feed") or {}),
        "- Focus:",
        _format_focus(focus),
        "",
        "Recommended next actions:",
    ]
    lines += [f"- {item}" for item in data.get("recommended_next_actions", [])]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_audit", annotations=ToolAnnotations(title="Check memory health", **_READ))
async def mnemotrace_audit(limit: int = 40, response_format: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """Read-only audit of missing edges, projection mismatches and unreferenced graph nodes."""
    try:
        data = await backend().audit(limit)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    checked = data.get("checked") or {}
    lines = [
        "# Memory health",
        "",
        f"Checked {checked.get('facts', 0)} facts / {checked.get('entities', 0)} entities — "
        f"**{data.get('total_findings', 0)} projection findings**",
        "",
    ]
    for kind, n in (data.get("by_kind") or {}).items():
        lines.append(f"- `{kind}`: {n}")
    findings = data.get("findings") or []
    if findings:
        lines += ["", "## Details", ""]
        for f in findings:
            refs = ", ".join(f"`{f[key]}`" for key in ("fact_id", "edge_id", "entity_id") if key in f)
            lines.append(f"- `{f['kind']}` {refs} — {f['why']} → _{f['action']}_")
    if data.get("truncated"):
        lines += ["", f"_(showing {len(findings)}; raise `limit` for more)_"]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_stats", annotations=ToolAnnotations(title="Get memory stats", **_READ))
async def mnemotrace_stats(response_format: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """获取不含内容的命名空间可观测性：episode 计数、整合积压、fact 计数、图计数、待处理
    冲突和后端就绪状态。用于健康检查和进度审计，不暴露 profile 文本、fact 或原始 episode。

    返回（json）：`/v1/stats` 的 MemoryStats。
    """
    try:
        data = await backend().stats()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    c = data.get("counts") or {}
    tr = data.get("time_range") or {}
    lines = [
        "# MnemoTrace stats",
        "",
        f"- Episodes: {c.get('episodes', 0)} "
        f"({c.get('episodes_consolidated', 0)} consolidated, {c.get('episodes_pending', 0)} pending)",
        f"- Extraction failures: {c.get('extraction_failures', 0)} episode(s)",
        f"- Facts: {c.get('facts_live', 0)} live, {c.get('facts_superseded', 0)} superseded, "
        f"{c.get('facts_sensitive', 0)} sensitive",
        f"- Graph: {c.get('entities', 0)} entities, {c.get('relations', 0)} relations",
        f"- Graph hygiene: {c.get('graph_orphan_entities', 0)} orphan entities, "
        f"{c.get('graph_stale_relations', 0)} stale relations",
        f"- Pending conflicts: {c.get('pending_conflicts', 0)}",
        f"- Consolidation backlog: {'yes' if data.get('consolidation_backlog') else 'no'}",
        f"- Backend: storage={data.get('storage', '?')}, embedder={data.get('embedder', '?')}, "
        f"llm_configured={bool(data.get('llm_configured'))}",
    ]
    if tr.get("first_event_at_h") or tr.get("last_event_at_h"):
        lines.append(f"- Event range: {tr.get('first_event_at_h') or '?'} to {tr.get('last_event_at_h') or '?'}")
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_add_fact", annotations=ToolAnnotations(title="Assert an authoritative fact", **_WRITE))
async def mnemotrace_add_fact(
    predicate: Annotated[str, Field(description="The relation, snake_case, e.g. 'works_at', 'lives_in', "
                                    "'prefers', 'allergic_to'.", min_length=1)],
    object: Annotated[str, Field(description="The value, e.g. 'Acme Corp', 'Berlin', 'dark mode'.",
                                 min_length=1)],
    text: Annotated[str, Field(description="Complete, self-contained fact sentence in the source language.", min_length=1)],
    subject: Annotated[str, Field(description="Who/what the fact is about. Default 'user'.")] = "user",
    sensitive: Annotated[Optional[bool], Field(description="Optional user override for sensitivity.")] = None,
    categories: Annotated[list[Category] | None, Field(description="Set fact labels; use [] to clear.")] = None,
    object_kind: ObjectKind = "unknown",
    cardinality: Cardinality = "unknown",
    retention: Retention = "unknown",
) -> str:
    """Assert a fact. Set cardinality to single to update the same slot over time.
    Use multi when values can coexist and durable to exempt it from decay.
    """
    try:
        data = await backend().add_fact(subject, predicate, object, text=text, sensitive=sensitive, categories=categories,
                                        object_kind=object_kind, cardinality=cardinality, retention=retention)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return (f"Stored authoritative fact: {data['text']}\n"
            f"Categories: {', '.join(data.get('categories', [])) or '—'} ({data.get('categories_source', 'unset')})")


@mcp.tool(name="mnemotrace_update_fact", annotations=ToolAnnotations(title="Correct a stored fact", **_WRITE))
async def mnemotrace_update_fact(
    fact_id: Annotated[str, Field(description="Fact id from mnemotrace_list_facts.", min_length=1)],
    subject: Annotated[Optional[str], Field(description="Optional replacement subject.")] = None,
    predicate: Annotated[Optional[str], Field(description="Optional replacement predicate.")] = None,
    object: Annotated[Optional[str], Field(description="Replacement value; content edits require subject, predicate, object and text together.")] = None,
    text: Annotated[Optional[str], Field(description="Complete replacement fact sentence; required with all three structured fields for content edits.")] = None,
    sensitive: Annotated[Optional[bool], Field(description="Optional sensitivity override.")] = None,
    categories: Annotated[list[Category] | None, Field(description="Set fact labels; use [] to clear.")] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    object_kind: ObjectKind | None = None,
    cardinality: Cardinality | None = None,
    retention: Retention | None = None,
) -> str:
    """按 id 修正一条已存的 fact。

    当用户指出某条被记住的 fact 有误时使用。实际修改内容后变为用户撰写且权威，之后的
    自动抽取不会悄悄覆盖它；仅修改敏感标记或分类时保留原来源。

    返回（json）：{"ok": true, "id": str, "text": str}，找不到时返回错误字符串。
    """
    if all(v is None for v in (subject, predicate, object, text, sensitive, categories, object_kind, cardinality, retention)):
        return "Error: provide at least one field to update."
    try:
        data = await backend().update_fact(
            fact_id,
            subject=subject,
            predicate=predicate,
            object=object,
            text=text,
            sensitive=sensitive,
            categories=categories,
            object_kind=object_kind,
            cardinality=cardinality,
            retention=retention,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if data is None:
        return f"Error: fact `{fact_id}` was not found."
    if response_format == ResponseFormat.JSON:
        return _json(data)
    return (f"Updated fact `{data.get('id', fact_id)}`: {data.get('text', '')}\n"
            f"Categories: {', '.join(data.get('categories', [])) or '—'} ({data.get('categories_source', 'unset')})")


@mcp.tool(name="mnemotrace_delete_fact", annotations=ToolAnnotations(title="Delete one stored fact", **_DESTRUCTIVE))
async def mnemotrace_delete_fact(
    fact_id: Annotated[str, Field(description="Fact id from mnemotrace_list_facts.", min_length=1)],
    confirm: Annotated[bool, Field(description="Must be true to permanently delete this fact.")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """按 id 永久删除一条已存的 fact。

    用于精确修正/被遗忘权请求。它不会擦除整个命名空间；仅当用户明确要求清空一切时才用
    `mnemotrace_forget(confirm=true)`。

    返回（json）：{"ok": bool}。
    """
    if not confirm:
        return (
            f"This permanently deletes fact `{fact_id}` and cannot be undone. Re-call with confirm=true "
            "only if the user explicitly asked to delete that fact."
        )
    try:
        data = await backend().delete_fact(fact_id)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    return f"Deleted fact `{fact_id}`." if data.get("ok") else f"Fact `{fact_id}` was not found."


@mcp.tool(name="mnemotrace_export", annotations=ToolAnnotations(title="Export memory data", **_READ))
async def mnemotrace_export(
    include_sensitive: Annotated[
        bool,
        Field(description="When false (default), return a share-safe structured export: non-sensitive "
              "facts + graph only. Set true only when the user explicitly wants a full private export."),
    ] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """导出用户记忆，用于可携带或审计。

    默认是可分享的安全导出：敏感 fact 和自由文本层（profile、原始 episode、摘要）均省略。
    仅在用户明确要求的完整私有导出时设 include_sensitive=true。

    返回（json）：`/v1/export` 的 payload。
    """
    try:
        data = await backend().export(include_sensitive=include_sensitive)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    facts = data.get("facts") or []
    graph = data.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    lines = [
        "# MnemoTrace export",
        "",
        f"- Space: `{data.get('space_id', '?')}`",
        f"- Include sensitive: {bool(data.get('include_sensitive'))}",
        f"- Redacted sensitive: {bool(data.get('redacted_sensitive'))}",
        f"- Facts: {len(facts)}",
        f"- Graph: {len(nodes)} node(s), {len(edges)} edge(s)",
    ]
    if data.get("redaction_note"):
        lines += ["", data["redaction_note"]]
    lines += [
        "",
        "Call again with `response_format=\"json\"` to get the portable JSON payload.",
    ]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_forget", annotations=ToolAnnotations(title="Erase all memory", **_DESTRUCTIVE))
async def mnemotrace_forget(
    confirm: Annotated[bool, Field(description="Must be true to proceed — this is irreversible.")] = False,
) -> str:
    """永久擦除该命名空间下的全部记忆（被遗忘权）。不可逆。

    需要 confirm=true。不传时是无操作，只解释会发生什么——这样一次误调用不会抹掉用户的记忆。
    """
    if not confirm:
        return ("This permanently erases ALL stored memory for this namespace and cannot be undone. "
                "Re-call with confirm=true only if the user explicitly asked to wipe their memory.")
    try:
        data = await backend().forget()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return data.get("message", "All memory erased.")
