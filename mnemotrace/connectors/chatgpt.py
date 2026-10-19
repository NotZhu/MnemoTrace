"""ChatGPT 数据导出连接器。

ChatGPT 的 `conversations.json` 是一个对话列表，每个对话是 `mapping` 字典里的一棵消息节点*树*
（以便表达被编辑/分叉的轮次）。我们从 `current_node` 沿 `parent` 指针向上回溯，重建**用户实际看到的
活跃线性线程**；若树结构损坏，则回退为按时间排序遍历全部节点。
"""
from __future__ import annotations

from typing import Any

from .base import ImportMessage, ImportSession, extract_text, to_epoch


def _node_text(message: dict) -> str:
    """从 ChatGPT 消息节点里提取展示文本，覆盖各内容类型（text / multimodal / code）。"""
    content = message.get("content")
    if isinstance(content, dict) and content.get("content_type") == "code":
        return extract_text(content.get("text", ""))
    return extract_text(content)


def _is_hidden(message: dict) -> bool:
    meta = message.get("metadata") or {}
    return bool(meta.get("is_visually_hidden_from_conversation"))


def _linear_thread(mapping: dict, current_node: Any) -> list[dict]:
    """活跃路径：从 current_node 沿 parent 链向上到根，再反转为时间顺序。"""
    path: list[dict] = []
    seen: set[str] = set()
    node_id = current_node
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        msg = node.get("message")
        if msg:
            path.append(msg)
        node_id = node.get("parent")
    path.reverse()
    return path


def _all_messages_sorted(mapping: dict) -> list[dict]:
    """没有可用 current_node 时的回退：全部消息节点，按 create_time 排序。"""
    msgs = [n["message"] for n in mapping.values() if isinstance(n, dict) and n.get("message")]
    return sorted(msgs, key=lambda m: m.get("create_time") or 0.0)


def parse_conversation(conv: dict, index: int = 0) -> ImportSession:
    """一个 ChatGPT 对话 -> 一个 ImportSession。"""
    mapping = conv.get("mapping") or {}
    nodes = _linear_thread(mapping, conv.get("current_node")) or _all_messages_sorted(mapping)

    messages: list[ImportMessage] = []
    for msg in nodes:
        if not isinstance(msg, dict) or _is_hidden(msg):
            continue
        role = ((msg.get("author") or {}).get("role")) or "user"
        if role == "system":
            continue  # 隐藏的 system 前导 —— 不是用户记忆
        text = _node_text(msg).strip()
        if not text:
            continue
        messages.append(ImportMessage(
            content=text, speaker=role, event_time=to_epoch(msg.get("create_time"))
        ))

    title = conv.get("title") or f"conversation {index + 1}"
    sid = conv.get("conversation_id") or conv.get("id") or f"chatgpt_{index}"
    start = to_epoch(conv.get("create_time"))
    return ImportSession(session_id=str(sid), messages=messages, event_time=start, title=str(title),
                         metadata={"source": "chatgpt"})


def parse_chatgpt(obj: Any) -> list[ImportSession]:
    """解析 ChatGPT 导出。接受顶层列表、`{conversations: [...]}` 包装或单个对话对象。
    空对话（没有存活轮次的）被丢弃。"""
    if isinstance(obj, dict):
        if "conversations" in obj:
            obj = obj["conversations"]
        elif "mapping" in obj:
            obj = [obj]
    if not isinstance(obj, list):
        raise ValueError("ChatGPT export must be a list of conversations (or a {conversations:[...]}).")
    sessions = [parse_conversation(c, i) for i, c in enumerate(obj) if isinstance(c, dict)]
    return [s for s in sessions if s.messages]
