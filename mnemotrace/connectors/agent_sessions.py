"""Agent 会话 transcript（Claude Code、Codex）-> 忆痕 会话。

这个模块存在的原因：对 agent 用户来说，值得记忆的素材就是他们的 agent 会话，而这些会话本来就躺在
磁盘上 —— ~/.claude/projects/<project>/<session>.jsonl 和 ~/.codex/sessions/。在此之前，要把它们导入
只能一条一条手写进 `mnemotrace_remember`，实际上等于什么都没导入。

这些日志不是聊天导出。它们把对话与 agent 的内部机制交织在一起：thinking 块、工具调用及其结果、
队列操作、标题生成、权限询问。全部导入会让那几条持久的事实淹没在 transcript 噪音里 —— 记忆会被
"跑了 grep，命中 3 条"填满。所以这里只保留人真正说过的话和 agent 真正的回复，其余全部丢弃。

    from mnemotrace.connectors.agent_sessions import parse_agent_session, find_sessions
    sessions = parse_agent_session(open(path).read(), session_id="claude-code:my-project")

子 agent 的 transcript（`isSidechain: true`）默认丢弃：那是 agent 之间讨论怎么干活的记录，
不是用户自己的历史。
"""
from __future__ import annotations

import json
import re
import os
from typing import Any, Iterator, Optional

from .base import ImportMessage, ImportSession, record_time

# Only documented dialogue text blocks enter memory.
_TEXT_BLOCKS = {"text", "input_text", "output_text"}

# Agent 会话里经常出现仍有效的凭据：用户粘贴一个 key，或某条命令回显了 .env。长期记忆是它们最不该去的
# 地方 —— 它持久、可被每个 agent 检索、还可导出。脱敏发生在导入时、任何内容入库之前，因为秘密一旦进入
# 存储，就已经泄漏进嵌入、摘要和提取出的 fact 里了。
_SECRET_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,})"),                       # OpenAI/DeepSeek/Anthropic 风格
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),                    # GitHub token
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"),                # Slack
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),                           # AWS access key id
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"),  # JWT
    # KEY=value / TOKEN: value 形式，即粘贴的 .env 或 export 行的样子。
    re.compile(r"((?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)\s*[=:]\s*)([^\s\"']{8,})",
               re.IGNORECASE),
]


def redact_secrets(text: str) -> str:
    """把看起来像有效凭据的内容替换成占位标记，同时保持句子可读。"""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text


def _text_of(content: Any) -> str:
    """把消息体压平成人能认出"这就是原话"的文本。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in _TEXT_BLOCKS:
            continue
        # Codex 把文本包装成 {"type": "input_text"|"output_text", "text": ...}；Claude Code 用
        # {"type": "text", "text": ...}。两种都会走到这里。
        text = block.get("text") or block.get("content")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def _rows(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # 活跃会话的最后一行被截断是正常现象；跳过而不是报错
        if isinstance(row, dict):
            yield row


def parse_agent_session(text: str, session_id: str = "agent-session",
                        include_sidechains: bool = False,
                        min_chars: int = 1) -> list[ImportSession]:
    """把一份 Claude Code / Codex 会话日志解析成一个 ImportSession。

    默认保留所有非空对话，包括可能确认先前建议的简短回复。
    """
    messages: list[ImportMessage] = []
    first_ts: Optional[float] = None
    for row in _rows(text):
        if row.get("isSidechain") and not include_sidechains:
            continue
        kind = row.get("type")
        if kind in ("user", "assistant"):
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role", kind) != kind:
                continue
            role = kind
        elif kind == "response_item":
            message = row.get("payload")
            if not isinstance(message, dict) or message.get("type") != "message":
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
        else:
            continue
        body = _text_of(message.get("content"))
        if len(body) < min_chars:
            continue
        ts = record_time(row, "timestamp")
        if first_ts is None:
            first_ts = ts
        message_id = message.get('id') or row.get('uuid')
        messages.append(ImportMessage(content=redact_secrets(body), role=role, event_time=ts,
                                      message_id=str(message_id) if message_id is not None else None))
    if not messages:
        return []
    return [ImportSession(session_id=session_id, messages=messages, event_time=first_ts,
                          metadata={"source": "agent_session"})]


def _session_label(path: str) -> str:
    """一个可读的会话 id：项目目录名加上 transcript 自身的 id。

    Claude Code 把项目路径编码进目录名（如 "-Users-someone-Documents-Foo"），所以尾段才是人认得出的部分。
    """
    name = os.path.basename(path)
    if name.startswith("rollout-"):  # Codex: rollout-<iso-date>-<uuid>.jsonl
        return f"codex:{name[8:18]}:{os.path.splitext(name)[0][-8:]}"
    project = os.path.basename(os.path.dirname(path)).strip("-").split("-")[-1] or "project"
    return f"claude-code:{project}:{os.path.splitext(name)[0][:8]}"


def find_sessions(root: Optional[str] = None, limit: Optional[int] = None,
                  since: Optional[float] = None) -> list[str]:
    """List candidate transcripts by modification time, newest first."""
    roots = [root] if root else [
        os.path.expanduser("~/.claude/projects"),
        os.path.expanduser("~/.codex/sessions"),
    ]
    found: list[tuple[float, str]] = []
    for base in roots:
        if not base or not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for name in filenames:
                if not name.endswith(".jsonl"):
                    continue
                # agent-*.jsonl 是子 agent 的 transcript（一个 agent 指挥另一个），不是用户自己的会话；
                # 否则任务内部的 chatter 会灌满记忆。
                if name.startswith("agent-"):
                    continue
                path = os.path.join(dirpath, name)
                if os.path.islink(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if since is not None and st.st_mtime < since:
                    continue
                found.append((st.st_mtime, path))
    found.sort(reverse=True)
    paths = [p for _, p in found]
    return paths[:limit] if limit else paths


def load_sessions(paths: list[str], include_sidechains: bool = False, *,
                  skip_unreadable: bool = True) -> list[ImportSession]:
    """把多份 transcript 解析成会话，跳过没有对话内容的那些。"""
    out: list[ImportSession] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            if not skip_unreadable:
                raise
            continue
        out.extend(parse_agent_session(text, session_id=_session_label(path),
                                       include_sidechains=include_sidechains))
    return out
