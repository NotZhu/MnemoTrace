"""OpenAI 兼容的 chat 端点，透明叠加长期记忆（AGENTS.md 「代码导航」 采用层）。

把任何 OpenAI 客户端的 `base_url` 指向 忆痕 服务器即可免费获得记忆：模型回答之前，忆痕
检索一小段相关的用户历史并注入；回答之后，在关键路径之外记住这一轮。这是最大的即插即用采用
杠杆——只改一个 URL，保留现有 OpenAI SDK 代码。

    from openai import OpenAI
    client = OpenAI(base_url="http://localhost:8000/v1", api_key="sk-alice-123")
    client.chat.completions.create(model="mnemotrace", messages=[{"role":"user","content":"where do I live?"}])

本模块刻意不依赖 FastAPI（纯粹基于 MemoryService + LLM 接口的编排），因此组装逻辑无需 web 栈
即可单元测试。路由接线在 mnemotrace/server/app.py。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..connectors.base import extract_text  # 复用：展平 str | 多模态 parts | dict 内容
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
    """最近一条 user 消息——我们对其做检索，并（可选）将其记住。"""
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
    """为最新一条 user 消息持久化的文本：其文本加上任意图片部分的描述，让通过代理发送的图片
    留下可检索的痕迹（AGENTS.md 「代码导航」 多模态）。没有图片或没有视觉描述器时精确退化为
    `latest_user_text`——纯文本路径不变。"""
    from ..llm.vision import caption_content

    m = _latest_user_message(messages)
    if m is None:
        return ""
    text = _content(m).strip()
    caps = caption_content(captioner, m.get("content"))  # 无图片部分时为 []
    lines = ([text] if text else []) + caps
    return "\n".join(lines).strip()


def build_prompt(messages: list, memory_context: str) -> tuple[Optional[str], str]:
    """把请求的 messages 渲染为 LLM.complete 接口的 (system, prompt) 对：
      * system = 注入的记忆块 + 请求自带的 system 消息
      * prompt = 单条 user 消息，或多轮对话的完整记录
    刻意与具体后端无关：适配任何 LLM（包括测试用的离线 FakeLLM），不限于 litellm chat 模型。"""
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
    n_chunks: int = 6,
    do_recall: bool = True,
    as_of: Optional[float] = None,
    redact_sensitive: bool = False,
    session_id: Optional[str] = None,
) -> dict:
    """检索 → 注入 → 生成 → 返回 OpenAI ChatCompletion 对象（带描述记忆使用情况的 `mnemotrace`
    扩展字段）。不写记忆——由路由在关键路径之外调度。请求生成但没有 LLM 后端时抛出
    NoLLMConfigured。"""
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
                n_chunks=n_chunks,
                session_id=session_id,
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
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    content = response["choices"][0]["message"]["content"] or ""
    yield chunk({"role": "assistant"}, None)
    for i in range(0, len(content), slice_size):
        yield chunk({"content": content[i:i + slice_size]}, None)
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"
