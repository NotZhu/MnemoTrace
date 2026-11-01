"""mnemotrace_mcp — 忆痕 长期记忆的 Model Context Protocol 服务器（AGENTS.md 「代码导航」，
2026 年 agent 技术栈中杠杆率最高的采用入口）。

给任何 MCP 客户端（Claude Desktop、Claude Code、Cursor 等）一个持久的双时间轴记忆：它可以在
session 之间 `mnemotrace_remember` fact，并 `mnemotrace_recall` 一小段相关内容来锚定下一个回答——
而不是重新塞入整个历史。

运行（stdio，供桌面客户端使用）：
    pip install "mnemotrace-memory[mcp]"
    python -m mnemotrace.mcp                       # 本地记忆在 ~/.mnemotrace/data，命名空间 'me'
    MNEMOTRACE_API_URL=http://localhost:8000 MNEMOTRACE_API_KEY=sk-… python -m mnemotrace.mcp   # 代理服务器

这些工具是对共享 MemoryService / HTTP 契约的薄格式化层（见 mnemotrace/mcp/backends.py），
因此本地与托管部署行为完全一致。
"""
# NOTE: 这里不要加 `from __future__ import annotations` —— FastMCP 会在运行时内省这些工具签名
# （对每个参数的注解做 issubclass 检查），字符串化注解会破坏它。

import json
from enum import Enum
from typing import Annotated, Literal, Optional

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from pydantic import Field
except Exception as exc:  # noqa: BLE001
    raise SystemExit('the MCP server needs the MCP SDK — `pip install "mnemotrace-memory[mcp]"`') from exc

from .backends import Backend, make_backend
from ..topics import Topic

INSTRUCTIONS = (
    "MnemoTrace is the user's long-term memory. Use `mnemotrace_recall` BEFORE answering anything that may "
    "depend on what you've learned about the user in past sessions (their preferences, projects, "
    "people, decisions) — it returns a small, dated, relevant context to ground your answer. Use "
    "`mnemotrace_remember` whenever the user states a durable fact, preference, or decision worth keeping "
    "(use scope='long' or the default scope='auto'); use scope='working' for temporary current-session "
    "state that should not become a long-term profile fact. "
    "Use `mnemotrace_close_session` when a thread/task ends to finish consolidation and clear ephemeral "
    "working state for that session; use `mnemotrace_session_report` when the user asks what that session "
    "saved; use `mnemotrace_list_sessions` when the user asks which agent/app sessions touched memory. "
    "`mnemotrace_search` gives a single direct answer; `mnemotrace_agent_status` checks the current namespace, "
    "session, focus, and next memory actions without exposing memory contents; `mnemotrace_stats` checks "
    "content-free memory health; "
    "`mnemotrace_list_facts` / `mnemotrace_profile` browse what's stored; `mnemotrace_add_fact` records an "
    "authoritative fact; `mnemotrace_update_fact` corrects a specific fact; `mnemotrace_delete_fact` deletes "
    "one specific fact; `mnemotrace_get_focus` / `mnemotrace_set_focus` inspect and update the user's tracked "
    "or muted memory topics; `mnemotrace_import` bulk-loads an exported history; `mnemotrace_export` gives the "
    "user a portable memory export; `mnemotrace_forget` erases everything (irreversible)."
)

mcp = FastMCP("mnemotrace_mcp", instructions=INSTRUCTIONS)

# --- backend 单例（测试/嵌入场景可覆盖） ------------------
_backend: Optional[Backend] = None


def backend() -> Backend:
    global _backend
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


class RememberScope(str, Enum):
    AUTO = "auto"
    LONG = "long"
    WORKING = "working"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _err(e: Exception) -> str:
    """可操作、不泄露内部信息的错误文案（最佳实践：引导 agent 修复）。"""
    from ..extraction import ExtractionError
    if isinstance(e, ExtractionError):
        return "Error: " + _json(e.detail())
    try:
        import httpx

        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code in {502, 503}:
                try:
                    detail = e.response.json().get("detail", {})
                    if detail.get("code") in {"llm_not_configured", "buffered_processing_disabled", "processing_incomplete", "extraction_failed", "extraction_invalid_response", "extraction_truncated"}:
                        return "Error: " + _json(detail)
                except (ValueError, AttributeError):
                    pass
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
    return f"Error: {type(e).__name__}: {e}"


def _format_feed(feed: dict) -> str:
    """记忆上次由本机 agent session 投喂的时间——这一行能区分空记忆和健康记忆。"""
    if not feed.get("last_fed_at_h"):
        return "- Last fed: never — run mnemotrace-watch --install"
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
    max_chunks: Annotated[int, Field(description="How many event evidence records to include "
                                     "(0 = facts/summaries only). Default 6.", ge=0, le=20)] = 6,
    session_id: Annotated[Optional[str], Field(description="Optional conversation/thread id; when set, "
                                           "session-scoped working memory is included.")] = None,
    as_of: Annotated[Optional[float], Field(description="Optional epoch seconds for a point-in-time memory "
                                           "view, e.g. what was believed before a later update.")] = None,
    redact_sensitive: Annotated[bool, Field(description="When true, omit facts tagged sensitive from the "
                                            "returned context.")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
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
            n_chunks=max_chunks,
            lean=True,
            session_id=session_id,
            as_of=as_of,
            redact_sensitive=redact_sensitive,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    ctx = (data.get("context") or "").strip()
    if not ctx:
        return "No relevant memory found for that query."
    return f"Relevant memory (~{data.get('tokens_est', '?')} tokens):\n\n{ctx}"


@mcp.tool(name="mnemotrace_get_episode", annotations=ToolAnnotations(title="Read original event", **_READ))
async def mnemotrace_get_episode(
    episode_id: Annotated[str, Field(description="Event ID returned by recall; reads its full original.",
                                   min_length=1, max_length=512)],
    space: Annotated[Optional[str], Field(description="Source space shown beside the event ID.",
                                         max_length=512)] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Explicitly read a source event. Recall does not expand summarized events automatically."""
    try:
        data = await backend().get_episode(episode_id, space)
    except Exception as exc:  # noqa: BLE001 -- shared sanitized adapter errors
        return _err(exc)
    if data is None:
        return "Error: episode not found."
    if response_format == ResponseFormat.JSON:
        return _json(data)
    return (f"Event {data['id']} (space: {data['space']}; session: {data['session_id']}; "
            f"event_time: {data['event_time']}; ingested_at: {data['ingested_at']})\n\n{data['content']}")


@mcp.tool(name="mnemotrace_search", annotations=ToolAnnotations(title="Answer from memory", **_READ))
async def mnemotrace_search(
    query: Annotated[str, Field(description="A direct factual question, e.g. 'Where does the user work?'",
                                min_length=1)],
    as_of: Annotated[Optional[float], Field(description="Optional epoch seconds for a point-in-time memory "
                                           "view, e.g. answer as of a past date.")] = None,
    redact_sensitive: Annotated[bool, Field(description="When true, omit facts tagged sensitive and abstain "
                                            "instead of returning sensitive answers.")] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """直接从记忆回答单个事实性问题（附支撑的 fact）。

    当你想要一个具体答案而非上下文块时使用（更宽的锚定用 `mnemotrace_recall`）。当该属性未存储时，
    它会拒答（"I don't have that in memory."）而不是猜测。

    返回（markdown）：答案及其依据的 fact。
    返回（json）：{"answer": str, "facts": [str, ...], "as_of": float|null, "redacted_sensitive": bool}。
    """
    try:
        data = await backend().recall(query, lean=False, as_of=as_of, redact_sensitive=redact_sensitive)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    lines = [f"**Answer:** {data.get('answer') or '(no answer)'}"]
    facts = data.get("facts") or []
    if facts:
        lines += ["", "Supporting facts:"] + [f"- {t}" for t in facts]
    return "\n".join(lines)


@mcp.tool(name='mnemotrace_remember_messages', annotations=ToolAnnotations(title='Remember dialogue messages', **_WRITE))
async def mnemotrace_remember_messages(messages: list[dict], session_id: str,
        mode: Literal['live', 'historical'] = 'live',
        processing: Literal['immediate', 'buffered'] = 'buffered',
        scope: Literal['auto', 'long'] = 'auto') -> str:
    """Store complete user/assistant messages with required stable message_id, role and content.

    event_time may be unknown. Reuse IDs on retransmission; historical never activates old tasks.
    Acceptance persists raw sources and tasks; inspect processing status or flush for completion.
    """
    try:
        return _json(await backend().remember_messages(messages, session_id, mode, processing, scope))
    except Exception as exc:
        return _err(exc)


@mcp.tool(name="mnemotrace_remember", annotations=ToolAnnotations(title="Remember a memory", **_WRITE))
async def mnemotrace_remember(
    content: Annotated[str, Field(description="The message / fact / preference to store, in natural "
                                  "language. e.g. 'I just moved to Berlin and started a new job at Acme.'",
                                  min_length=1)],
    session_id: Annotated[str, Field(description="Optional conversation id to group related turns.")] = "default",
    scope: Annotated[RememberScope, Field(description="auto = route by ephemerality; long = force durable "
                                           "fact extraction; working = current-session state that should not "
                                           "be promoted to long-term profile facts.")] = RememberScope.AUTO,
    processing: Literal['immediate', 'buffered'] = 'immediate',
) -> str:
    """把内容存入用户长期记忆并整合（抽取原子双时间轴 fact、与已有认知解决矛盾、更新知识图）。

    当用户说出值得跨 session 记住的持久 fact、偏好、计划或决定时调用。临时性的当前 session
    状态用 `scope=working`。存储与旧值矛盾的值不会删除历史——旧 fact 被失效化并保留
    （as-of 查询依然可用）。auto/long 需要配置 LLM；抽取失败会返回可识别错误并保留原文供重试。
    """
    try:
        data = await backend().remember(content, session_id=session_id, scope=scope.value, processing=processing)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    summary = data['summarization']
    summary_status = f" Summarization: {summary['status']}"
    if summary['error_code']:
        summary_status += f" ({summary['error_code']})"
    summary_status += "."
    if data.get("queued"):
        return (f"Queued raw episode {data['episode_id']} for model extraction; extraction is not complete."
                + summary_status)
    if data.get("scope") == "working":
        return (
            "Stored as working memory for this session. It remains in dated history, but will not be "
            "promoted to durable profile facts." + summary_status
        )
    if data.get("scope") == "none":
        return "Saved raw history. No durable facts or working memory were extracted (scope=none)." + summary_status
    return (f"Remembered. Extracted {data.get('extracted', 0)} new fact(s); "
            f"added {data.get('working_added', 0)} working item(s) (scope={data.get('scope')}); "
            f"{data.get('total_facts', '?')} live facts total." + summary_status)


@mcp.tool(name='mnemotrace_processing_status', annotations=ToolAnnotations(title='Memory processing status', **_READ))
async def mnemotrace_processing_status(session_id: Optional[str] = None) -> str:
    """Inspect buffered processing without returning raw content or model credentials."""
    try:
        return _json(await backend().processing_status(session_id))
    except Exception as exc:
        return _err(exc)


@mcp.tool(name='mnemotrace_flush_processing', annotations=ToolAnnotations(title='Flush memory processing', **_WRITE))
async def mnemotrace_flush_processing(session_id: Optional[str] = None, timeout: float = 30,
                                     retry_failed: bool = False, include_summaries: bool = False) -> str:
    """Process the currently accepted boundary; retry_failed explicitly retries failed/cancelled work."""
    try:
        return _json(await backend().flush_processing(session_id, timeout, retry_failed, include_summaries))
    except Exception as exc:
        return _err(exc)


@mcp.tool(
    name="mnemotrace_close_session",
    annotations=ToolAnnotations(title="Close a memory session", **_WRITE),
)
async def mnemotrace_close_session(
    session_id: Annotated[str, Field(description="Conversation/thread id to close.")] = "default",
    summarize: Annotated[
        bool,
        Field(description="Create missing long-event summaries within the captured session boundary."),
    ] = True,
    clear_working: Annotated[
        bool,
        Field(description="Clear ephemeral working memory for this session."),
    ] = True,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """结束一个客户端线程的记忆生命周期，但不删除原始对话记录。

    在 Claude Code / Codex / Cursor 完成任务或切换 session 时调用。它会排空该 session 待处理的
    整合、补齐缺失的摘要、把知识更新反映到摘要中、清空短时效的 working memory，并持久化命名空间。

    返回（json）：{"ok","session_id","episodes","pending_consolidated","facts_added","outcomes",
    "summaries","working_cleared",...}。
    """
    try:
        data = await backend().close_session(
            session_id=session_id,
            summarize=summarize,
            clear_working=clear_working,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    # session 结论是关闭动作中最值得让人看到的部分；只报数字会让蒸馏在 agent 唯一汇报的
    # 地方变得不可见。
    distilled = data.get("outcomes") or 0
    return (
        f"Closed session `{data.get('session_id', session_id)}`. "
        f"Processed {data.get('episodes', 0)} episode(s), "
        f"consolidated {data.get('pending_consolidated', 0)} pending episode(s), "
        f"added {data.get('facts_added', 0)} fact(s), "
        f"created {data.get('summaries', 0)} summary(ies), "
        f"cleared {data.get('working_cleared', 0)} working item(s)"
        + (f", distilled {distilled} conclusion(s)." if distilled else ".")
    )


@mcp.tool(name="mnemotrace_session_report", annotations=ToolAnnotations(title="Audit a memory session", **_READ))
async def mnemotrace_session_report(
    session_id: Annotated[str, Field(description="Conversation/thread id to audit.", min_length=1)],
    include_sensitive: Annotated[
        bool,
        Field(description="When false (default), redact sensitive fact text from the report."),
    ] = False,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
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
        f"- Working memory still live: {data.get('working_live', 0)} item(s)",
        f"- Durable facts linked to this session: {data.get('facts_added', 0)}",
        f"- Sensitive facts redacted: {data.get('facts_redacted', 0)}",
    ]
    if not facts:
        lines += ["", "No durable facts are linked to this session yet."]
        return "\n".join(lines)
    lines += ["", "Facts:"]
    for f in facts:
        tag = " _(superseded)_" if f.get("status") != "live" else ""
        redacted = " 🔒 redacted" if f.get("redacted") else ""
        lines.append(f"- `{f.get('id', '?')}` [{f.get('valid_at', '?')}] {f.get('text', '')}{tag}{redacted}")
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
            f"{row.get('working_live', 0)} working",
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
    topic: Annotated[Optional[Topic], Field(description="Exact stored main topic; unknown matches both "
                                          "manual unknown and unassigned facts. Omit for all topics.")] = None,
) -> str:
    """浏览 忆痕 存储的关于用户的原子 fact（分页）。

    每个 fact 带日期（生效时间）并标记为 live 或 superseded；用户断言的 fact 带 🔒。
    可用于审计已知内容，或在修正前找到 fact id。

    返回（json）：{"total","count","offset","has_more","next_offset","facts":[...]}。
    """
    try:
        dump = await backend().memories(topic=topic)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    facts = dump.get("facts", [])
    if not include_superseded:
        facts = [f for f in facts if f.get("status") == "live"]
    total = len(facts)
    page = facts[offset:offset + limit]
    nxt = offset + len(page) if offset + len(page) < total else None
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
                     f" · topic: {f.get('topic', 'unknown')} ({f.get('topic_source', 'unset')})")
    if nxt is not None:
        lines += ["", f"…{total - nxt} more — call again with offset={nxt}."]
    return "\n".join(lines)


@mcp.tool(name="mnemotrace_profile", annotations=ToolAnnotations(title="Get the user profile", **_READ))
async def mnemotrace_profile(response_format: ResponseFormat = ResponseFormat.MARKDOWN) -> str:
    """获取用户的合成画像（对其持久偏好、习惯和关键 fact 的紧凑叙述）——session 开始时适合
    先调用它来建立上下文。

    返回（json）：{"profile": str, "facts": [str, ...]}。
    """
    try:
        data = await backend().profile()
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if response_format == ResponseFormat.JSON:
        return _json(data)
    profile = (data.get("profile") or "").strip()
    out = ["# User profile", "", profile or "(no profile synthesized yet — memory may be empty)"]
    facts = data.get("facts") or []
    if facts:
        out += ["", "Key facts:"] + [f"- {t}" for t in facts[:20]]
    return "\n".join(out)


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
        f"- Namespace: `{data.get('user', '?')}`",
        f"- Session: `{s.get('id') or '(none supplied)'}`",
        f"- Session episodes: {s.get('episodes', 0)} "
        f"({s.get('episodes_pending', 0)} pending consolidation)",
        f"- Session extraction failures: {s.get('extraction_failures', 0)} episode(s)",
        f"- Overall extraction failures: {c.get('extraction_failures', 0)} episode(s)",
        f"- Session working memory: {s.get('working_live', 0)} live item(s)",
        f"- Long-term memory: {c.get('facts_live', 0)} live fact(s), "
        f"{c.get('summaries', 0)} summary(ies), {c.get('pending_conflicts', 0)} pending conflict(s)",
        f"- Overall working memory: {c.get('working_live', 0)} live item(s)",
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
    """找出值得修复的已存记忆，每条都带被标记的原因和处理办法。

    个人记忆库赢得信任靠的是可检查，而不是体量大：在真实对话上做抽取必然留下垃圾（把原始
    token 当值用、object 只是重复 predicate、从未压缩成 fact 的整句陈述），不逐条阅读根本
    发现不了。在信任一个 profile 之前、或 recall 返回奇怪结果时使用。只读——只提出修复建议，
    不执行（执行见 mnemotrace_update_fact/mnemotrace_delete_fact）。

    返回（json）：{"checked", "total_findings", "by_kind", "findings":[{kind, why, action, ...}]}。
    """
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
        f"**{data.get('total_findings', 0)} worth a look**",
        "",
    ]
    for kind, n in (data.get("by_kind") or {}).items():
        lines.append(f"- `{kind}`: {n}")
    findings = data.get("findings") or []
    if findings:
        lines += ["", "## Details", ""]
        for f in findings:
            what = f.get("text") or f.get("entity") or ""
            if not what and f.get("predicate"):
                # 组级发现（slot_overflow）描述的是整个 (subject, predicate) slot，而不是某一行，
                # 所以它没有 `text`。没有这段兜底，该行会渲染成空的加粗文本，agent 会被要求
                # "clear this slot" 却不知道是哪个 slot。
                what = f"{f.get('subject', '')} {f.get('predicate')} ×{f.get('count', 0)}".strip()
            lines.append(f"- **{what}** — {f.get('why', '')} → _{f.get('action', '')}_")
            for sample in (f.get("samples") or [])[:3]:
                lines.append(f"  - {sample.get('text', '')}")
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
        f"({c.get('episodes_consolidated', 0)} consolidated, {c.get('episodes_pending', 0)} pending, "
        f"{c.get('episodes_ephemeral', 0)} with working content)",
        f"- Extraction failures: {c.get('extraction_failures', 0)} episode(s)",
        f"- Facts: {c.get('facts_live', 0)} live, {c.get('facts_superseded', 0)} superseded, "
        f"{c.get('facts_sensitive', 0)} sensitive",
        f"- Heat tiering: {c.get('facts_hot', 0)} hot, {c.get('facts_cold', 0)} cold, "
        f"{c.get('cold_pages_out', 0)} paged out, {c.get('cold_pages_in', 0)} paged back",
        f"- Working memory: {c.get('working_live', 0)} live",
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
    subject: Annotated[str, Field(description="Who/what the fact is about. Default 'user'.")] = "user",
    sensitive: Annotated[Optional[bool], Field(description="Optional user override for sensitivity.")] = None,
    category: Annotated[Optional[str], Field(description="Optional user-visible category label.")] = None,
    topic: Annotated[Optional[Topic], Field(description="Optional manually selected main topic.")] = None,
) -> str:
    """断言单条权威的 (subject, predicate, object) fact。

    与 `mnemotrace_remember`（从自由文本抽取 fact）不同，这里记录的是用户明确断言的一条精确 fact。
    它会被标记为用户撰写且是权威的：自动抽取绝不会悄悄覆盖它，并且它会失效化同一 slot 上
    任何冲突的抽取值。
    """
    try:
        data = await backend().add_fact(subject, predicate, object, sensitive=sensitive, category=category,
                                        topic=topic)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    return (f"Stored authoritative fact: {data.get('text', f'{subject} {predicate} {object}')}\n"
            f"Topic: {data.get('topic', 'unknown')} ({data.get('topic_source', 'unset')})")


@mcp.tool(name="mnemotrace_update_fact", annotations=ToolAnnotations(title="Correct a stored fact", **_WRITE))
async def mnemotrace_update_fact(
    fact_id: Annotated[str, Field(description="Fact id from mnemotrace_list_facts.", min_length=1)],
    subject: Annotated[Optional[str], Field(description="Optional replacement subject.")] = None,
    predicate: Annotated[Optional[str], Field(description="Optional replacement predicate.")] = None,
    object: Annotated[Optional[str], Field(description="Optional replacement object/value.")] = None,
    sensitive: Annotated[Optional[bool], Field(description="Optional sensitivity override.")] = None,
    category: Annotated[Optional[str], Field(description="Optional category override.")] = None,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
    topic: Annotated[Optional[Topic], Field(description="Optional manual main topic; unknown is explicit.")] = None,
    reset_topic: Annotated[bool, Field(description="Re-evaluate topic with offline rules, without a model call; "
                                                  "cannot be combined with topic.")] = False,
) -> str:
    """按 id 修正一条已存的 fact。

    当用户指出某条被记住的 fact 有误时使用。实际修改内容后变为用户撰写且权威，之后的
    自动抽取不会悄悄覆盖它；仅修改敏感标记或分类时保留原来源。

    返回（json）：{"ok": true, "id": str, "text": str}，找不到时返回错误字符串。
    """
    if all(v is None for v in (subject, predicate, object, sensitive, category, topic)) and not reset_topic:
        return "Error: provide at least one field to update."
    try:
        data = await backend().update_fact(
            fact_id,
            subject=subject,
            predicate=predicate,
            object=object,
            sensitive=sensitive,
            category=category,
            topic=topic,
            reset_topic=reset_topic,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)
    if data is None:
        return f"Error: fact `{fact_id}` was not found."
    if response_format == ResponseFormat.JSON:
        return _json(data)
    return (f"Updated fact `{data.get('id', fact_id)}`: {data.get('text', '')}\n"
            f"Topic: {data.get('topic', 'unknown')} ({data.get('topic_source', 'unset')})")


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


@mcp.tool(name="mnemotrace_import", annotations=ToolAnnotations(title="Bulk-import a history", **_WRITE))
async def mnemotrace_import(
    content: Annotated[str | list | dict,
                       Field(description="The raw export to import: a ChatGPT conversations.json, an "
                             "OpenAI messages array, JSON-Lines, or a plain 'Speaker: text' transcript. "
                             "Paste the file contents (JSON text, an array, or transcript text).")],
    format: Annotated[str, Field(description="chatgpt | messages | records | jsonl | transcript | auto "
                                 "(default, sniffs the shape).")] = "auto",
) -> str:
    """单次批量把导出的聊天历史导入记忆（抽取 fact + 摘要）。

    用于从既有历史（如用户的 ChatGPT 导出）播种记忆，而不是逐条消息重放。`format='auto'`
    会自动识别格式。
    """
    try:
        data = await backend().import_(content, format=format)
    except Exception as e:  # noqa: BLE001
        return _err(e)
    text = (f"Imported {data.get('sessions', 0)} session(s) / {data.get('episodes', 0)} episode(s); "
            f"extracted {data.get('facts_added', 0)} fact(s), {data.get('summaries', 0)} summary(ies).")
    if data.get('queued'):
        text += '\nProcessing queued; use processing_status or flush_processing before treating extraction as complete.'
    if data.get('summarization'):
        text += "\nSummarization by event: " + _json(data['summarization'])
    deferred = int(data.get("facts_deferred") or 0)
    if deferred:
        text += f"; {deferred} message(s) stored without automatic extraction"
    return text


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
        f"- Namespace: `{data.get('user', '?')}`",
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
