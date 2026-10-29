"""把聊天历史批量导入 忆痕 —— 使用已配置的 LLM。

    python examples/batch_import.py

演示 「代码导航」 adoption 层的导入路径：取一份导出的历史（这里是一个极小的内联 ChatGPT 风格导出
和一个 OpenAI messages 数组），用 mnemotrace.connectors 归一化，一次性批量导入，然后再从中召回。
同一个 `parse(...)` 同时支撑 `python -m mnemotrace.connectors`、`POST /v1/import` 端点和
MCP 的 `mnemotrace_import` 工具。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import Memory  # noqa: E402
from mnemotrace.connectors import parse, sniff  # noqa: E402

# 一个迷你版 ChatGPT `conversations.json`（单会话，含一个活跃的 2 节点线程）。
CHATGPT_EXPORT = [{
    "title": "Moving to Berlin",
    "create_time": 1_700_000_000.0,
    "current_node": "b",
    "mapping": {
        "a": {"id": "a", "parent": None, "children": ["b"],
              "message": {"author": {"role": "user"}, "create_time": 1_700_000_000.0,
                          "content": {"content_type": "text",
                                      "parts": ["I'm moving to Berlin next month for a job at Acme."]}}},
        "b": {"id": "b", "parent": "a", "children": [],
              "message": {"author": {"role": "assistant"}, "create_time": 1_700_000_100.0,
                          "content": {"content_type": "text", "parts": ["Exciting! Berlin is great."]}}},
    },
}]

# 一个 OpenAI 风格的 messages 数组（另一个独立"会话"）。
OPENAI_MESSAGES = [
    {"role": "user", "content": "By the way, I'm vegetarian and allergic to peanuts."},
    {"role": "assistant", "content": "Noted — I'll keep that in mind."},
]


def main() -> None:
    import os
    from mnemotrace.llm.providers import load_dotenv, make_llm
    from mnemotrace.extraction import LLMNotConfiguredError
    load_dotenv()
    model = os.environ.get("MNEMOTRACE_LLM", "").strip()
    if not model:
        raise SystemExit(str(LLMNotConfiguredError()))
    mem = Memory(llm=make_llm(model))

    print("== Detect + parse two different export formats ==")
    chatgpt_sessions = parse(CHATGPT_EXPORT)  # 自动嗅探格式
    openai_sessions = parse(OPENAI_MESSAGES, format="messages")
    print(f"   chatgpt export  -> sniffed as '{sniff(CHATGPT_EXPORT)}', {len(chatgpt_sessions)} session(s)")
    print(f"   openai messages -> {len(openai_sessions)} session(s)")

    print("\n== Batch-import both (one consolidation pass each) ==")
    print("  ", mem.import_messages(chatgpt_sessions, user_id="me"))
    print("  ", mem.import_messages(openai_sessions, user_id="me"))

    print("\n== Recall from the imported memory ==")
    for q in ["Where am I moving and why?", "What are my dietary restrictions?"]:
        ctx = mem.lean_context(q, user_id="me", n_chunks=2)
        snippet = " ".join(ctx.split())[:160]
        print(f"   Q: {q}\n      -> {snippet}…\n")

    # 单行等价写法：mem.import_data(raw_text_or_object, format="auto", user_id="me")


if __name__ == "__main__":
    main()
