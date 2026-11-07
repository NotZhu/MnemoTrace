"""OpenAI 兼容的 /v1/chat/completions —— 记忆注入 + 生成 + 后台 remember。
离线验证：hashing 嵌入器、固定抽取响应，以及用于生成的确定性 FakeLLM。"""
from __future__ import annotations

import shutil
import tempfile

import pytest

from mnemotrace.llm import FakeLLM
from mnemotrace.server import openai_compat as oc
from tests.model_fixtures import MemoryService

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


@pytest.mark.parametrize("model", [None, "mnemotrace", "custom-model"])
def test_chat_completion_uses_recalled_memory(model):
    d = tempfile.mkdtemp(prefix="mnemotrace_oc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.remember("u", "I live in Shenzhen and work at Tencent.")
        seen = {}
        svc.llm = FakeLLM(handler=lambda p, s: seen.setdefault("system", s) and "" or "You live in Shenzhen.")
        body = {"messages": [{"role": "user", "content": "where do I live?"}]}
        if model is not None:
            body["model"] = model
        resp = oc.chat_completion(svc, "u", body)
        assert resp["object"] == "chat.completion"
        assert resp["model"] == (model or "mnemotrace")
        assert "engram" not in resp
        assert resp["choices"][0]["message"]["content"] == "You live in Shenzhen."
        assert resp["mnemotrace"]["recalled"] is True
        assert "Shenzhen" in (seen["system"] or "")  # 记忆应已注入 system prompt
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chat_completion_supports_as_of_memory_view():
    d = tempfile.mkdtemp(prefix="mnemotrace_oc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.import_("u", None, "records", [
            {"session_id": "old", "content": "Wei works at Tencent.", "event_time": 1_700_000_000.0},
            {"session_id": "new", "content": "Wei works at Moonshot AI.", "event_time": 1_702_592_000.0},
        ])
        seen = {}
        svc.llm = FakeLLM(handler=lambda p, s: seen.setdefault("system", s) and "" or "You worked at Tencent.")
        body = {"model": "mnemotrace", "messages": [{"role": "user", "content": "where did Wei work?"}]}
        resp = oc.chat_completion(svc, "u", body, as_of=1_700_864_000.0)

        assert resp["mnemotrace"]["as_of"] == 1_700_864_000.0
        assert "Tencent" in (seen["system"] or "")
        assert "Moonshot AI" not in (seen["system"] or "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chat_completion_can_redact_sensitive_memory():
    d = tempfile.mkdtemp(prefix="mnemotrace_oc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.add_fact("u", "user", "has_disease", "diabetes")
        svc.add_fact("u", "user", "works_at", "Acme")
        seen = {}
        svc.llm = FakeLLM(handler=lambda p, s: seen.setdefault("system", s) and "" or "ok")
        body = {"model": "mnemotrace", "messages": [{"role": "user", "content": "what do you know about me?"}]}
        resp = oc.chat_completion(svc, "u", body, n_chunks=0, redact_sensitive=True)

        assert resp["mnemotrace"]["redacted_sensitive"] is True
        system = seen["system"] or ""
        assert "Acme" in system
        assert "diabetes" not in system.lower()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chat_completion_uses_session_scoped_working_memory():
    d = tempfile.mkdtemp(prefix="mnemotrace_oc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.add_working("u", "today I am checking release notes", session_id="agent-thread-1")
        seen = {}
        svc.llm = FakeLLM(handler=lambda p, s: seen.setdefault("system", s) and "" or "ok")
        body = {"model": "mnemotrace", "messages": [{"role": "user", "content": "what am I doing?"}]}

        resp = oc.chat_completion(svc, "u", body, session_id="agent-thread-1")

        assert resp["mnemotrace"]["session_id"] == "agent-thread-1"
        assert "release notes" in (seen["system"] or "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_chat_completion_without_llm_raises():
    d = tempfile.mkdtemp(prefix="mnemotrace_oc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.llm = None
        with pytest.raises(oc.NoLLMConfigured):
            oc.chat_completion(svc, "u", {"messages": [{"role": "user", "content": "hi"}]})
    finally:
        shutil.rmtree(d, ignore_errors=True)
