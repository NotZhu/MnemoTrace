"""OpenAI 兼容的 /v1/chat/completions —— 记忆注入 + 生成 + 后台 remember。
离线验证：hashing 嵌入器、固定抽取响应，以及用于生成的确定性 FakeLLM。"""
from __future__ import annotations



from mnemotrace.server import openai_compat as oc

# --- 纯拼装逻辑（不依赖 web 框架） -------------------------------------


def test_latest_user_text_and_multimodal():
    msgs = [{"role": "system", "content": "be nice"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "text", "text": "where do I live?"}]}]
    assert oc.latest_user_text(msgs) == "where do I live?"


def test_build_prompt_injects_memory_and_system():
    msgs = [{"role": "system", "content": "house rules"},
            {"role": "user", "content": "hi"}]
    system, prompt = oc.build_prompt(msgs, "FACTS: user lives in Berlin")
    assert "RELEVANT MEMORY" in system and "Berlin" in system and "house rules" in system
    assert prompt == "hi"
    # 多轮对话时渲染为一份对话记录
    msgs2 = msgs + [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}]
    _, prompt2 = oc.build_prompt(msgs2, "")
    assert "User:" in prompt2 and prompt2.endswith("Assistant:")


def test_iter_sse_reassembles_exactly():
    resp = {"id": "x", "created": 0, "model": "mnemotrace",
            "choices": [{"message": {"content": "Hello, 世界! " * 10}}]}
    body = "".join(p for p in oc.iter_sse(resp))
    assert body.endswith("data: [DONE]\n\n")
    # 拼接所有 delta 后应还原出原始内容
    import json
    rebuilt = ""
    for line in body.splitlines():
        if line.startswith("data: ") and "[DONE]" not in line:
            delta = json.loads(line[6:])["choices"][0]["delta"]
            rebuilt += delta.get("content", "")
    assert rebuilt == "Hello, 世界! " * 10
