"""Compose OpenAI-shaped responses with context from an authorized memory space.

The HTTP route validates `memory.space_id` and passes its authorized context here.
This module assembles the prompt and response without depending on FastAPI.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..util import extract_text
from ..util import gen_id, now


class NoLLMConfigured(RuntimeError):
    """请求生成但服务器没有配置 LLM 后端时抛出（需设置 MNEMOTRACE_LLM）。"""


# 为模型框定注入记忆的指令。
_MEMORY_PREAMBLE = (
    "You have access to long-term memory about this user, retrieved below. Treat it as known context "
    "and use it when relevant; prefer the most recent dated fact when values conflict. If the memory "
    "doesn't cover the question, answer normally.\n\nRELEVANT MEMORY:\n"
)


def _content(msg: dict) -> str:
    return extract_text(msg.get("content"))


def latest_user_text(messages: list) -> str:
    """查找最近一条带文本的用户消息。

    Args:
        messages: 聊天消息列表。

    Returns:
        最近的非空用户文本。无匹配时返回空字符串。
    """
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            text = _content(m).strip()
            if text:
                return text
    return ""


def _latest_user_message(messages: list) -> Optional[dict]:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            return m
    return None


def remembered_text(captioner: Any, messages: list) -> str:
    """提取最近一条用户消息中可保存的文本和图片描述。

    Args:
        captioner: 可选的图片描述器。
        messages: 聊天消息列表。

    Returns:
        合并后的文本。没有用户消息时返回空字符串。
    """
    from ..llm.vision import caption_content

    m = _latest_user_message(messages)
    if m is None:
        return ""
    text = _content(m).strip()
    caps = caption_content(captioner, m.get("content"))  # 无图片部分时为 []
    lines = ([text] if text else []) + caps
    return "\n".join(lines).strip()


def build_prompt(messages: list, memory_context: str) -> tuple[Optional[str], str]:
    """将记忆和聊天消息渲染为模型提示。

    Args:
        messages: 聊天消息列表。
        memory_context: 要注入系统提示的记忆上下文。

    Returns:
        系统提示与对话提示。无系统内容时首项为 None。
    """
    system_parts: list[str] = []
    if memory_context.strip():
        system_parts.append(_MEMORY_PREAMBLE + memory_context.strip())
    system_parts += [c for c in (_content(m) for m in messages
                                 if isinstance(m, dict) and m.get("role") == "system") if c.strip()]
    system = "\n\n".join(system_parts) or None

    convo = [m for m in messages if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not convo:
        prompt = ""
    elif len(convo) == 1:
        prompt = _content(convo[0])
    else:
        rendered = "\n".join(f"{m['role'].capitalize()}: {_content(m)}" for m in convo)
        prompt = rendered + "\nAssistant:"
    return system, prompt


def _est_tokens(text: str) -> int:
    return len((text or "").split())


def chat_completion(
    svc: Any,
    user: str,
    body: dict,
    n_digests: int = 20,
    do_recall: bool = True,
    as_of: Optional[float] = None,
    redact_sensitive: bool = False,
    session_id: Optional[str] = None,
) -> dict:
    """按需检索并注入记忆，然后生成聊天回复。

    Args:
        svc: 记忆与模型服务。
        user: 调用者已授权的空间上下文。
        body: 聊天请求数据。
        n_digests: 来源摘要数量上限。
        do_recall: 是否在生成前检索记忆。
        as_of: 查询事实有效性的时间戳。
        redact_sensitive: 是否排除敏感事实。
        session_id: 写入响应元数据的会话 ID。

    Returns:
        带记忆使用信息的 OpenAI 兼容聊天回复。

    Raises:
        NoLLMConfigured: 未配置聊天生成模型。
    """
    messages = body.get("messages") or []
    model = body.get("model") or "mnemotrace"
    query = latest_user_text(messages) or (_content(messages[-1]) if messages else "")

    memory_context = ""
    if do_recall and query:
        memory_context = (
            svc.recall(
                user,
                query,
                lean=True,
                n_digests=n_digests,

                as_of=as_of,
                redact_sensitive=redact_sensitive,
            ).get("context") or ""
        )

    if svc.llm is None:
        raise NoLLMConfigured(
            "no LLM configured for generation — set MNEMOTRACE_LLM (e.g. 'deepseek'), or use /v1/recall "
            "for retrieval-only.")

    system, prompt = build_prompt(messages, memory_context)
    content = svc.llm.complete(prompt, system=system)

    p_tokens = _est_tokens((system or "") + " " + prompt)
    c_tokens = _est_tokens(content)
    return {
        "id": gen_id("chatcmpl"),
        "object": "chat.completion",
        "created": int(now()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": p_tokens, "completion_tokens": c_tokens,
                  "total_tokens": p_tokens + c_tokens},
        # 忆痕 扩展——对记忆层的透明披露（标准 OpenAI 客户端会忽略）。
        "mnemotrace": {
            "recalled": bool(memory_context.strip()),
            "memory_tokens_est": _est_tokens(memory_context),
            "session_id": session_id,
            "as_of": as_of,
            "redacted_sensitive": redact_sensitive,
            "remembered": False,  # 路由调度写入后将其翻为 True
        },
    }


def iter_sse(response: dict, slice_size: int = 48):
    """为 `stream=true` 的客户端产出极简但合法的 OpenAI SSE 流。生成并非真正增量（LLM 接口
    返回完整字符串），因此把已完成的内容切片输出——重组后的流与非流式内容逐字节一致。"""
    base = {"id": response["id"], "object": "chat.completion.chunk",
            "created": response["created"], "model": response["model"]}

    def chunk(delta: dict, finish: Optional[str]):
        payload = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if finish is not None and "mnemotrace" in response:
            payload["mnemotrace"] = response["mnemotrace"]
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    content = response["choices"][0]["message"]["content"] or ""
    yield chunk({"role": "assistant"}, None)
    for i in range(0, len(content), slice_size):
        yield chunk({"content": content[i:i + slice_size]}, None)
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"
