"""批量导入连接器（CLAUDE.md §6 采纳层）：把外部的历史导出转成 忆痕 会话，再由
`Memory.import_messages()` 用少量批量调用完成导入。

    from mnemotrace import Memory
    from mnemotrace.connectors import parse

    sessions = parse(open("conversations.json").read(), format="chatgpt")
    Memory().import_messages(sessions, user_id="me")

`format="auto"`（默认）会探测数据形状。支持的格式：
    chatgpt     ChatGPT 数据导出（conversations.json）
    messages    OpenAI 对话数组 — [{role, content}, ...]（或 {messages:[...]}）
    records     扁平的 {content, [session_id], [speaker], [timestamp]} 列表
    jsonl       JSON-Lines，每行一条记录
    transcript  纯文本 / markdown（"Speaker: text" 行，或自由文本）
"""
from __future__ import annotations

from typing import Any

from .base import ImportMessage, ImportSession, extract_text, load_json, to_epoch
from .chatgpt import parse_chatgpt
from .agent_sessions import find_sessions, load_sessions, parse_agent_session
from .generic import parse_jsonl, parse_messages, parse_records, parse_transcript

__all__ = [
    "ImportMessage", "ImportSession", "parse", "sniff",
    "parse_chatgpt", "parse_messages", "parse_records", "parse_jsonl", "parse_transcript",
    "parse_agent_session", "find_sessions", "load_sessions",
    "extract_text", "to_epoch",
]

FORMATS = ("chatgpt", "messages", "records", "jsonl", "transcript", "agent_session")


def sniff(data: Any) -> str:
    """根据数据形状猜测格式。自由文本回退为 'transcript'。"""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", errors="replace")
    if isinstance(data, str):
        s = data.lstrip()
        if not s:
            return "transcript"
        if s[0] not in "[{":
            return "transcript"  # 纯文本 / markdown
        # 每行都以 '{' 开头的多行 => JSON-Lines
        lines = [ln for ln in data.splitlines() if ln.strip()]
        if len(lines) > 1 and all(ln.lstrip().startswith("{") for ln in lines):
            # Agent 会话日志也是 JSONL，但通用读取器会保留其中的工具调用、丢掉对话内容，
            # 所以先用其独特的信封特征把它们识别出来。
            head = "\n".join(lines[:60])
            if '"isSidechain"' in head or '"type": "response_item"' in head or '"type":"response_item"' in head:
                return "agent_session"
            return "jsonl"
        try:
            data = load_json(data)
        except Exception:  # noqa: BLE001
            return "transcript"
    return _sniff_obj(data)


def _sniff_obj(obj: Any) -> str:
    if isinstance(obj, dict):
        if "mnemotrace_export_version" in obj:
            return "mnemotrace"  # 我们自己的 /v1/export 快照 —— 用于恢复，不能当作消息解析
        if "mapping" in obj or "conversations" in obj:
            return "chatgpt"
        if "messages" in obj:
            return "messages"
        return "records"
    if isinstance(obj, list):
        sample = next((x for x in obj if isinstance(x, dict)), None)
        if sample is None:
            return "records"
        if "mapping" in sample:
            return "chatgpt"
        if "messages" in sample:
            return "messages"
        if any(k in sample for k in ("role", "speaker", "author", "from")) and \
                any(k in sample for k in ("content", "text", "message", "body")):
            return "messages" if not any(
                k in sample for k in ("session_id", "conversation_id", "thread_id")) else "records"
        return "records"
    return "transcript"


def parse(data: Any, format: str = "auto", session_id: str = "imported") -> list[ImportSession]:
    """把原始导出（对象、JSON 字符串或 bytes）解析成 ImportSession 列表。

    Args:
        data: 导出内容 —— Python 对象、JSON/JSONL 字符串或 bytes。
        format: FORMATS 之一，或 'auto'（默认）自动探测。
        session_id: 对不带自身 id 的格式使用的基准会话 id。

    遇到未知格式或与指定格式不符的形状时抛出 ValueError。
    """
    fmt = format.lower().strip()
    if fmt == "auto":
        fmt = sniff(data)

    if fmt == "transcript" or fmt in ("text", "markdown", "md"):
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        return parse_transcript(text, session_id=session_id)
    if fmt in ("agent_session", "claude_code", "codex"):
        # Claude Code / Codex 会话日志：大部分是工具机制，需要专用读取器而不是通用 JSONL 读取器
        # （后者会保留工具调用、丢掉对话）。
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        return parse_agent_session(text, session_id=session_id)
    if fmt == "jsonl":
        text = data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        return parse_jsonl(text, session_id=session_id)

    obj = load_json(data)
    if fmt == "mnemotrace":
        raise ValueError(
            "this is an MnemoTrace export snapshot, not a message history -- restore it with "
            "Memory.import_snapshot() (or POST it to /v1/import, which routes it automatically).")
    if fmt == "chatgpt":
        return parse_chatgpt(obj)
    if fmt in ("messages", "openai"):
        return parse_messages(obj, session_id=session_id)
    if fmt in ("records", "generic"):
        return parse_records(obj, session_id=session_id)
    raise ValueError(f"unknown import format {format!r}; expected one of {FORMATS} or 'auto'.")
