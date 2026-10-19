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

from .base import ImportMessage, ImportSession, to_epoch

# 属于 agent 内部机制（而非其发言）的内容块。`thinking` 被刻意排除：那是进行中的推理，常与最终答案矛盾，
# 记住它会教给记忆一些 agent 最终否决的想法。
_SKIP_BLOCKS = {"thinking", "redacted_thinking", "tool_use", "tool_result", "image"}

# 完全不含对话内容的记录类型。
_SKIP_TYPES = {
    "queue-operation", "ai-title", "custom-title", "last-prompt", "attachment",
    "summary", "file-history-snapshot", "system",
}

# 以 user 轮次回显的工具结果是 harness 在自言自语，不是真人发言。
_TOOL_ECHO_PREFIXES = ("<tool_use_result", "<local-command", "Caveat: The messages below")

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
        if block.get("type") in _SKIP_BLOCKS:
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
                        min_chars: int = 12) -> list[ImportSession]:
    """把一份 Claude Code / Codex 会话日志解析成一个 ImportSession。

    `min_chars` 丢弃单词轮次（"ok"、"继续"、"yes"）—— 它们不含事实，否则会占满长会话的 episode 数量。
    """
    messages: list[ImportMessage] = []
    first_ts: Optional[float] = None
    for row in _rows(text):
        if row.get("type") in _SKIP_TYPES:
            continue
        if row.get("isSidechain") and not include_sidechains:
            continue
        # 整体都是工具机制的轮次会被压平成 ""，再被下面的 min_chars 丢弃；这是有意为之（真实会话的 162 轮
        # 中有 94 轮是 tool_use/tool_result/thinking）。Claude Code 把轮次放在 `message` 里；Codex 包装成
        # {"type": "response_item", "payload": {"type": "message", "role": ..., "content": [...]}}。
        message = row.get("message")
        if not isinstance(message, dict):
            payload = row.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "message":
                message = payload
            else:
                continue
        role = str(message.get("role") or row.get("type") or "").strip()
        if role not in ("user", "assistant"):
            continue  # Codex 还会发出 "developer"/"system" 轮次：harness 指令，不是记忆
        body = _text_of(message.get("content"))
        if len(body) < min_chars:
            continue
        if body.startswith(_TOOL_ECHO_PREFIXES):
            continue
        ts = to_epoch(row.get("timestamp"))
        if first_ts is None:
            first_ts = ts
        messages.append(ImportMessage(content=redact_secrets(body), speaker=role, event_time=ts))
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
                  min_bytes: int = 2048, since: Optional[float] = None) -> list[str]:
    """本机上的会话 transcript，最新优先。

    `min_bytes` 跳过短到不含任何持久内容的会话（只有一问的运行）。
    """
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
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_size < min_bytes:
                    continue
                if since is not None and st.st_mtime < since:
                    continue
                found.append((st.st_mtime, path))
    found.sort(reverse=True)
    paths = [p for _, p in found]
    return paths[:limit] if limit else paths


def load_sessions(paths: list[str], include_sidechains: bool = False) -> list[ImportSession]:
    """把多份 transcript 解析成会话，跳过没有对话内容的那些。"""
    out: list[ImportSession] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        out.extend(parse_agent_session(text, session_id=_session_label(path),
                                       include_sidechains=include_sidechains))
    return out
