"""通用连接器：OpenAI `messages` 数组、JSONL 日志、扁平记录列表和纯 transcript。

它们覆盖"我的历史长成 <某种形状>"的长尾场景，无需为每种形状单独写解析器。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .base import ImportMessage, ImportSession, extract_text, iter_jsonl, to_epoch

# 消息字典的角色可能放在下面任意一个键里；内容键同理
_ROLE_KEYS = ("role", "speaker", "author", "from", "sender")
_CONTENT_KEYS = ("content", "text", "message", "body", "value")
_TIME_KEYS = ("event_time", "timestamp", "time", "created_at", "create_time", "date", "ts")
_SESSION_KEYS = ("session_id", "session", "conversation_id", "thread_id", "chat_id")


def _first(d: dict, keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _msg_from_dict(d: dict, default_role: str = "user") -> Optional[ImportMessage]:
    role = _first(d, _ROLE_KEYS, default_role)
    if isinstance(role, dict):  # 例如 {"author": {"role": "user"}}
        role = role.get("role") or default_role
    text = extract_text(_first(d, _CONTENT_KEYS, ""))
    if not text or not str(text).strip():
        return None
    return ImportMessage(content=str(text).strip(), speaker=str(role),
                         event_time=to_epoch(_first(d, _TIME_KEYS)))


def parse_messages(obj: Any, session_id: str = "imported") -> list[ImportSession]:
    """OpenAI 风格的对话数组 —— {role, content} 列表（或 {messages:[...]} 包装）—— 解析为一个会话。
    也接受对话列表（list-of-lists / list-of-{messages}）。"""
    if isinstance(obj, dict) and "messages" in obj:
        obj = obj["messages"]
    if not isinstance(obj, list):
        raise ValueError("messages format expects a list of {role, content} objects.")

    # 是对话列表？ -> 递归处理，每个对话一个会话
    if obj and all(isinstance(x, (list, dict)) and (
            isinstance(x, list) or "messages" in x) for x in obj):
        out: list[ImportSession] = []
        for i, conv in enumerate(obj):
            out.extend(parse_messages(conv, session_id=f"{session_id}_{i}"))
        return [s for s in out if s.messages]

    messages = [m for m in (_msg_from_dict(x) for x in obj if isinstance(x, dict)) if m]
    sess = ImportSession(session_id=session_id, messages=messages, metadata={"source": "messages"})
    return [sess] if messages else []


def parse_records(obj: Any, session_id: str = "imported") -> list[ImportSession]:
    """扁平的记录字典列表，每条 {content, [session_id], [speaker], [timestamp]}。记录按其会话 id
    分组成会话（没有 id 的共用 `session_id`），保持原有顺序。"""
    if not isinstance(obj, list):
        raise ValueError("records format expects a list of objects.")
    grouped: dict[str, ImportSession] = {}
    order: list[str] = []
    for rec in obj:
        if not isinstance(rec, dict):
            continue
        msg = _msg_from_dict(rec)
        if msg is None:
            continue
        sid = str(_first(rec, _SESSION_KEYS, session_id))
        if sid not in grouped:
            grouped[sid] = ImportSession(session_id=sid, metadata={"source": "records"})
            order.append(sid)
        grouped[sid].messages.append(msg)
    return [grouped[s] for s in order if grouped[s].messages]


def parse_jsonl(text: str, session_id: str = "imported") -> list[ImportSession]:
    """JSON-Lines：每行一个 JSON 对象。每行被当作一条记录（可自带会话 id / 时间戳 / 角色），
    因此 JSONL 聊天日志会像 `records` 一样拆分成多个会话。"""
    records = list(iter_jsonl(text))
    return parse_records(records, session_id=session_id)


# 形如 "Alice: hello" 的 transcript 行 —— 捕获说话者与文本。名字保持较短，避免吞掉只是恰好含冒号的
# 真实句子文本。
_SPEAKER_RE = re.compile(r"^\s*([A-Za-z][\w .'-]{0,30}?)\s*[:：]\s+(.*\S.*)$")


def parse_transcript(text: str, session_id: str = "imported") -> list[ImportSession]:
    """纯文本/markdown transcript。若各行形如 'Speaker: text'，每行成为一轮（续行追加到当前轮）。
    否则整体作为一条 user 消息。"""
    messages: list[ImportMessage] = []
    current: Optional[ImportMessage] = None
    saw_speaker = False
    for raw in text.splitlines():
        m = _SPEAKER_RE.match(raw)
        if m:
            saw_speaker = True
            current = ImportMessage(content=m.group(2).strip(), speaker=m.group(1).strip())
            messages.append(current)
        elif current is not None and raw.strip():
            current.content += "\n" + raw.strip()
        elif raw.strip() and not saw_speaker:
            # 还没有说话者结构 —— 累积进同一条持续追加的消息
            if current is None:
                current = ImportMessage(content=raw.strip(), speaker="user")
                messages.append(current)
            else:
                current.content += "\n" + raw.strip()
    messages = [m for m in messages if m.content.strip()]
    sess = ImportSession(session_id=session_id, messages=messages, metadata={"source": "transcript"})
    return [sess] if messages else []
