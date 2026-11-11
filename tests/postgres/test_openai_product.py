"""OpenAI-shaped HTTP requests use the same authorized memory space."""
import pytest

from mnemotrace.llm import FakeLLM
from tests.postgres.test_http_product import hdr


@pytest.fixture
def client_with_llm(product_client, product_service):
    model = FakeLLM(handler=lambda prompt, system: "You live in Shenzhen.")
    extraction = product_service.llm

    class Model:
        def complete(self, prompt, system=None, **kwargs):
            if "MNEMOTRACE_WINDOW" in prompt:
                return extraction.complete(prompt, system=system or "", **kwargs)
            return model.complete(prompt, system=system, **kwargs)

    product_service.llm = Model()
    return product_client, product_service, model


def test_chat_completions_endpoint(client_with_llm):
    c, service, model = client_with_llm
    h = hdr("chatuser")
    c.post("/v1/remember", json={"content": "I live in Shenzhen."}, headers=h)
    r = c.post("/v1/chat/completions",
               json={"model": "mnemotrace", "messages": [{"role": "user", "content": "where do I live?"}]},
               headers=h)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "You live in Shenzhen."
    assert data["mnemotrace"]["recalled"] is True and data["mnemotrace"]["remember_queued"] is True
    # FakeLLM 的 system prompt 中应能看到注入的召回记忆
    assert any("Shenzhen" in (call["system"] or "") for call in model.calls)


def test_chat_completions_endpoint_supports_memory_as_of(client_with_llm):
    c, service, model = client_with_llm
    h = hdr("chat_asof")
    c.post("/v1/import", json={
        "format": "records", "consolidate": True,
        "data": [
            {"session_id": "old", "content": "Wei works at Tencent.", "event_time": 1_700_000_000.0},
            {"session_id": "new", "content": "Wei works at Moonshot AI.", "event_time": 1_702_592_000.0},
        ],
    }, headers=h)

    r = c.post("/v1/chat/completions", json={
        "model": "mnemotrace",
        "messages": [{"role": "user", "content": "where did Wei work?"}],
        "memory": {"as_of": 1_700_864_000.0, "remember": False},
    }, headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["mnemotrace"]["as_of"] == 1_700_864_000.0
    assert data["mnemotrace"]["remembered"] is False
    system = model.calls[-1]["system"] or ""
    assert "Tencent" in system
    assert "Moonshot AI" not in system


def test_chat_completions_endpoint_supports_sensitive_redaction(client_with_llm):
    c, service, model = client_with_llm
    h = hdr("chat_redact")
    c.post("/v1/facts", json={"text": 'user has disease diabetes', "predicate": "has_disease", "object": "diabetes"}, headers=h)
    c.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False}, headers=h)

    r = c.post("/v1/chat/completions", json={
        "model": "mnemotrace",
        "messages": [{"role": "user", "content": "what do you know about me?"}],
        "memory": {"redact_sensitive": True, "n_chunks": 0, "remember": False},
    }, headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["mnemotrace"]["redacted_sensitive"] is True
    system = model.calls[-1]["system"] or ""
    assert "Acme" in system
    assert "diabetes" not in system.lower()


def test_chat_completions_endpoint_remembers_with_session_and_scope(client_with_llm):
    c, _, _ = client_with_llm
    h = hdr("chat_session")

    r = c.post("/v1/chat/completions", json={
        "model": "mnemotrace",
        "messages": [{"role": "user", "content": "I work at Acme."}],
        "memory": {"session_id": "codex:repo:thread-1", "scope": "long"},
    }, headers=h)

    assert r.status_code == 200
    data = r.json()
    assert data["mnemotrace"]["session_id"] == "codex:repo:thread-1"
    assert data["mnemotrace"]["remember_queued"] is True
    assert data["mnemotrace"]["remember_scope"] == "long"

    memories = c.get("/v1/memories?include_sensitive=true", headers=h).json()
    assert any(e["session"] == "codex:repo:thread-1" for e in memories["episodes"])


def test_chat_completions_streaming(client_with_llm):
    c, _, _ = client_with_llm
    h = hdr("streamer")
    r = c.post("/v1/chat/completions",
               json={"messages": [{"role": "user", "content": "hi"}], "stream": True}, headers=h)
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "[DONE]" in r.text and "chat.completion.chunk" in r.text


def test_chat_completions_503_without_llm(client_with_llm):
    c, service, model = client_with_llm
    service.llm = None
    r = c.post("/v1/chat/completions",
               json={"messages": [{"role": "user", "content": "hi"}]}, headers=hdr("x"))
    assert r.status_code == 503
