"""MCP tool schemas, scoped local execution, and remote request encoding."""
from __future__ import annotations

import json
import re

import pytest

pytest.importorskip("mcp")

from mnemotrace.mcp import server as S  # noqa: E402
from mnemotrace.mcp.backends import LocalBackend, RemoteBackend  # noqa: E402
from tests.postgres.fact_fixtures import seed_fact  # noqa: E402


@pytest.fixture()
def local_backend(product_service, product_scope):
    context, _ = product_scope()
    backend = LocalBackend(context.user_id, context.space_id, service=product_service)
    S.set_backend(backend)
    try:
        yield backend
    finally:
        S.set_backend(None)


def text_of(res) -> str:
    seq = res[0] if isinstance(res, tuple) else res  # 新版 mcp 返回 (content, structured)
    return seq[0].text


async def call(name: str, **args) -> str:
    return text_of(await S.mcp.call_tool(name, args))


@pytest.mark.asyncio
async def test_tools_are_registered_with_flat_schema():
    tools = {t.name: t for t in await S.mcp.list_tools()}
    assert all(name.startswith("mnemotrace_") for name in tools)
    assert {"mnemotrace_remember", "mnemotrace_recall", "mnemotrace_flush_processing", "mnemotrace_session_report",
            "mnemotrace_list_sessions", "mnemotrace_search",
            "mnemotrace_list_facts", "mnemotrace_profile", "mnemotrace_refresh_profile", "mnemotrace_agent_status", "mnemotrace_stats",
            "mnemotrace_set_fact_sensitive", "mnemotrace_delete_fact", "mnemotrace_get_focus",
            "mnemotrace_set_focus", "mnemotrace_export", "mnemotrace_forget"} <= set(tools)
    assert "mnemotrace_import" not in tools
    # 顶层平铺的参数（不嵌套在 "params" 之下）
    props = tools["mnemotrace_recall"].inputSchema.get("properties", {})
    assert {"query", "max_digests", "as_of"} <= set(props)
    assert "session_id" not in props
    assert "redact_sensitive" in props
    remember_props = tools["mnemotrace_remember"].inputSchema.get("properties", {})
    assert "content" in remember_props and "session_id" in remember_props and remember_props["extract"]["type"] == "boolean"
    assert "scope" not in remember_props
    messages_props = tools["mnemotrace_remember_messages"].inputSchema["properties"]
    assert messages_props["extract"]["type"] == "boolean" and "scope" not in messages_props
    assert "mode" not in messages_props
    sensitivity = tools["mnemotrace_set_fact_sensitive"].inputSchema
    assert set(sensitivity["required"]) == {"fact_id", "sensitive"}
    assert "mnemotrace_add_fact" not in tools and "mnemotrace_update_fact" not in tools
    assert "known_at" not in props
    focus_props = tools["mnemotrace_set_focus"].inputSchema.get("properties", {})
    assert {"track", "mute"} <= set(focus_props)
    status_props = tools["mnemotrace_agent_status"].inputSchema.get("properties", {})
    assert "session_id" in status_props
    report_props = tools["mnemotrace_session_report"].inputSchema.get("properties", {})
    assert {"session_id", "include_sensitive"} <= set(report_props)
    sessions_props = tools["mnemotrace_list_sessions"].inputSchema.get("properties", {})
    assert {"limit", "offset", "q"} <= set(sessions_props)
    search_props = tools["mnemotrace_search"].inputSchema.get("properties", {})
    assert "as_of" in search_props and "redact_sensitive" in search_props
    assert tools["mnemotrace_recall"].annotations.readOnlyHint is True
    assert tools["mnemotrace_get_focus"].annotations.readOnlyHint is True
    assert tools["mnemotrace_set_focus"].annotations.readOnlyHint is False
    assert tools["mnemotrace_stats"].annotations.readOnlyHint is True
    assert tools["mnemotrace_agent_status"].annotations.readOnlyHint is True
    assert tools["mnemotrace_session_report"].annotations.readOnlyHint is True
    assert tools["mnemotrace_list_sessions"].annotations.readOnlyHint is True
    assert tools["mnemotrace_export"].annotations.readOnlyHint is True
    assert tools["mnemotrace_flush_processing"].annotations.readOnlyHint is False
    assert tools["mnemotrace_profile"].annotations.readOnlyHint is True
    assert tools["mnemotrace_refresh_profile"].annotations.readOnlyHint is False
    assert tools["mnemotrace_forget"].annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_remember_then_recall_and_search(local_backend):
    out = await call("mnemotrace_remember", content="My name is Wei and I live in Shenzhen.")
    assert "Remembered" in out
    ctx = await call("mnemotrace_recall", query="Where does the user live?")
    assert "Shenzhen" in ctx
    js = await call("mnemotrace_recall", query="Where does the user live?", response_format="json")
    assert '"context"' in js
    ans = await call("mnemotrace_search", query="Where does the user live?")
    assert "Shenzhen" in ans and "Answer" not in ans


@pytest.mark.asyncio
async def test_flush_processing_tool(local_backend):
    accepted = await call("mnemotrace_remember", content="I work at Acme.", session_id="s1",
                          processing="buffered", response_format="json")
    operation, = json.loads(accepted)["operation_ids"]
    data = json.loads(await call("mnemotrace_flush_processing", session_id="s1"))
    assert data["ok"]
    status = json.loads(await call("mnemotrace_get_operation", operation_id=operation))
    assert status["core_committed"]


@pytest.mark.asyncio
async def test_session_report_tool_audits_saved_facts(local_backend):
    session = "codex:super-memory:thread"
    await call(
        "mnemotrace_remember",
        content="Project decision: the launch checklist must include committed eval logs.",
        session_id=session,
    )
    await call("mnemotrace_flush_processing", session_id=session)

    report = await call("mnemotrace_session_report", session_id=session)

    assert "MnemoTrace session report" in report
    assert f"Session: `{session}`" in report
    assert "committed eval logs" in report

    raw = await call("mnemotrace_session_report", session_id=session, response_format="json")
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["session_id"] == session
    assert data["facts_added"] >= 1


@pytest.mark.asyncio
async def test_add_fact_list_and_profile(local_backend):
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'works_at', 'Moonshot AI', sensitive=False, text='Synthetic fact: Moonshot AI')
    facts = await call("mnemotrace_list_facts")
    assert "Moonshot AI" in facts
    assert re.search(r"`ft_[^`]+`", facts)
    prof = await call("mnemotrace_profile")
    assert "User profile" in prof


@pytest.mark.asyncio
async def test_explicit_profile_refresh_and_cached_read(local_backend):
    from tests.test_profile_cache import model

    llm = model()
    local_backend.svc.llm = llm
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'likes', 'Synthetic tea', sensitive=False, text='Synthetic fact: Synthetic tea')
    initial = json.loads(await call("mnemotrace_profile", response_format="json"))
    assert initial["updated_at"] is None and not llm.calls
    operation = json.loads(await call("mnemotrace_refresh_profile"))
    assert operation["kind"] == "profile" and operation["operation_id"]
    assert json.loads(await call("mnemotrace_flush_processing"))["ok"]
    completed = json.loads(await call("mnemotrace_profile", response_format="json"))
    assert completed["profile"]["basic"][0]["fact_count"] == 1
    count = len(llm.calls)
    await call("mnemotrace_profile")
    assert len(llm.calls) == count


@pytest.mark.asyncio
async def test_update_and_delete_fact_tools(local_backend):
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'works_at', 'ByteDance', sensitive=False, text='Synthetic fact: ByteDance')
    raw = await call("mnemotrace_list_facts", response_format="json")
    fid = json.loads(raw)["facts"][0]["id"]

    updated = await call("mnemotrace_set_fact_sensitive", fact_id=fid, sensitive=True)
    assert json.loads(updated)["sensitive"] is True
    stored = local_backend.svc.memory(local_backend.context).get_fact(fid)
    assert stored.object == "ByteDance" and stored.sensitive

    guard = await call("mnemotrace_delete_fact", fact_id=fid)
    assert "confirm=true" in guard
    deleted = await call("mnemotrace_delete_fact", fact_id=fid, confirm=True, response_format="json")
    assert json.loads(deleted)["ok"] is True
    facts_after = await call("mnemotrace_list_facts")
    assert "No facts stored yet" in facts_after


@pytest.mark.asyncio
async def test_focus_tools(local_backend):
    out = await call("mnemotrace_set_focus", track=["project decisions"], mute=["health details"])
    assert "project decisions" in out
    assert "health details" in out
    assert local_backend.svc.get_focus(local_backend.context) == {
        "track": ["project decisions"],
        "mute": ["health details"],
    }

    raw = await call("mnemotrace_get_focus", response_format="json")
    assert json.loads(raw) == {
        "track": ["project decisions"],
        "mute": ["health details"],
    }

    await call("mnemotrace_set_focus", mute=[], response_format="json")
    assert local_backend.svc.get_focus(local_backend.context) == {"track": ["project decisions"], "mute": []}

    guard = await call("mnemotrace_set_focus")
    assert "provide `track` or `mute`" in guard


@pytest.mark.asyncio
async def test_stats_tool_is_content_free(local_backend):
    await call("mnemotrace_remember", content="My private diagnosis is diabetes and I work at Acme.")
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'has_disease', 'diabetes', sensitive=True, text='Synthetic fact: diabetes')

    stats = await call("mnemotrace_stats")
    assert "MnemoTrace stats" in stats
    assert "Episodes:" in stats and "Consolidation backlog" in stats
    assert "Graph hygiene:" in stats
    assert "diabetes" not in stats.lower()
    assert "acme" not in stats.lower()

    raw = await call("mnemotrace_stats", response_format="json")
    data = json.loads(raw)
    assert data["counts"]["episodes"] >= 1
    assert "entities" in data["counts"]
    assert "relations" in data["counts"]
    assert "time_range" in data
    assert "diabetes" not in raw.lower()


@pytest.mark.asyncio
async def test_export_tool_defaults_to_share_safe_payload(local_backend):
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'works_at', 'Moonshot AI', sensitive=False, text='Synthetic fact: Moonshot AI')
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'has_disease', 'diabetes', sensitive=True, text='Synthetic fact: diabetes')

    digest = await call("mnemotrace_export")
    assert "MnemoTrace export" in digest
    assert "Include sensitive: False" in digest
    assert "Facts: 1" in digest
    assert "diabetes" not in digest.lower()

    safe = json.loads(await call("mnemotrace_export", response_format="json"))
    assert safe["include_sensitive"] is False
    assert safe["redacted_sensitive"] is True
    assert {f["object"] for f in safe["facts"]} == {"Moonshot AI"}
    assert safe["episodes"] == []
    assert not safe.get("profile")

    full = json.loads(await call(
        "mnemotrace_export",
        include_sensitive=True,
        response_format="json",
    ))
    assert full["include_sensitive"] is True
    assert "diabetes" in {f["object"] for f in full["facts"]}


@pytest.mark.asyncio
async def test_agent_status_tool_is_content_free_and_session_aware(local_backend):
    session = "codex:super-memory:thread"
    await call(
        "mnemotrace_remember",
        content="My private diagnosis is diabetes and I work at Acme.",
        session_id=session,
    )
    await call("mnemotrace_set_focus", track=["project decisions"], mute=["health details"])

    status = await call("mnemotrace_agent_status", session_id=session)

    assert "MnemoTrace agent status" in status
    assert f"Session: `{session}`" in status
    assert "project decisions" in status
    assert "mnemotrace_recall" in status
    assert "diabetes" not in status.lower()
    assert "acme" not in status.lower()
    assert "private launch note" not in status.lower()

    raw = await call("mnemotrace_agent_status", session_id=session, response_format="json")
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["focus"] == {"track": ["project decisions"], "mute": ["health details"]}
    assert "diabetes" not in raw.lower()
    assert "acme" not in raw.lower()


@pytest.mark.asyncio
async def test_list_sessions_tool_is_content_free(local_backend):
    codex_session = "codex:repo/thread 1"
    claude_session = "claude-code:repo/thread 2"
    await call(
        "mnemotrace_remember",
        content="Project decision: launch notes require committed raw logs.",
        session_id=codex_session,
    )
    await call(
        "mnemotrace_remember",
        content="My private diagnosis is diabetes.",
        session_id=claude_session,
    )

    digest = await call("mnemotrace_list_sessions", q="repo")

    assert "Memory sessions" in digest
    assert codex_session in digest
    assert claude_session in digest
    assert "diabetes" not in digest.lower()
    assert "launch notes" not in digest.lower()
    assert "temporary launch" not in digest.lower()

    raw = await call("mnemotrace_list_sessions", q="claude-code", response_format="json")
    data = json.loads(raw)
    assert data["sessions"][0]["id"] == claude_session
    assert data["sessions"][0]["facts_sensitive"] >= 1
    assert "diabetes" not in raw.lower()


@pytest.mark.history_import
@pytest.mark.asyncio
async def test_import_tool(local_backend):
    arr = '[{"role":"user","content":"I have a cat named Pixel."}]'
    out = await call("mnemotrace_import", content=arr, format="messages")
    assert "Accepted" in out and "source" in out
    await call("mnemotrace_flush_processing")
    ctx = await call("mnemotrace_recall", query="What pet do I have?")
    assert "Pixel" in ctx


@pytest.mark.asyncio
async def test_recall_and_search_support_as_of(local_backend):
    for session, text, stamp in (
        ("old", "Wei works at Tencent.", 1_700_000_000.0),
        ("new", "Wei works at Moonshot AI.", 1_702_592_000.0),
    ):
        await call("mnemotrace_remember_messages", session_id=session, processing="immediate",
                   messages=[{"message_id": "m1", "role": "user", "content": text, "event_time": stamp}])

    ctx = await call(
        "mnemotrace_recall",
        query="Where does Wei work?",
        max_digests=0,
        as_of=1_700_864_000.0,
    )
    assert "Tencent" in ctx
    assert "Moonshot AI" not in ctx

    raw = await call(
        "mnemotrace_search",
        query="Where does Wei work?",
        as_of=1_700_864_000.0,
        response_format="json",
    )
    data = json.loads(raw)
    assert data["as_of"] == 1_700_864_000.0
    assert data["status"] == "evidence_only" and any("Tencent" in fact for fact in data["facts"])
    assert all("Moonshot" not in fact for fact in data["facts"])


@pytest.mark.asyncio
async def test_recall_and_search_support_sensitive_redaction(local_backend):
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'has_disease', 'diabetes', sensitive=True, text='Synthetic fact: diabetes')
    seed_fact(local_backend.svc.memory(local_backend.context), "user", 'works_at', 'Acme', sensitive=False, text='Synthetic fact: Acme')

    ctx = await call(
        "mnemotrace_recall",
        query="what do you know about me?",
        max_digests=0,
        redact_sensitive=True,
    )
    assert "Acme" in ctx
    assert "diabetes" not in ctx.lower()

    raw = await call(
        "mnemotrace_search",
        query="what disease do I have?",
        redact_sensitive=True,
        response_format="json",
    )
    data = json.loads(raw)
    assert data["redacted_sensitive"] is True
    assert "diabetes" not in data["answer"].lower()
    assert all("diabetes" not in fact.lower() for fact in data["facts"])


@pytest.mark.asyncio
async def test_remote_backend_forwards_as_of():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    await backend.recall(
        "Where does Wei work?",
        lean=False,

        as_of=1_700_864_000.0,
        redact_sensitive=True,
    )

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/recall"
    assert body["as_of"] == 1_700_864_000.0
    assert body["redact_sensitive"] is True
    assert headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("name, arguments", [
    ("mnemotrace_remember", {"content": "Synthetic note"}),
    ("mnemotrace_remember_messages", {"session_id": "s1", "messages": [
        {"message_id": "m1", "role": "user", "content": "Synthetic dialogue"},
    ]}),
])
@pytest.mark.parametrize("invalid, message", [
    ({"scope": "raw"}, "extract"), ({"extract": "false"}, "extract"),
    ({"extract": 0}, "extract"), ({"mode": "live"}, "mode"),
])
async def test_mcp_write_rejects_old_and_non_boolean_extraction_arguments(name, arguments, invalid, message):
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match=message):
        await S.mcp.call_tool(name, {**arguments, **invalid})


@pytest.mark.asyncio
async def test_remote_backend_forwards_remember_extract():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "extract": False}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.remember("Synthetic reusable decision", session_id="s1", extract=False)

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/remember"
    assert body == {"space_id": "synthetic-space", "content": "Synthetic reusable decision", "session_id": "s1",
                    "extract": False, "processing": "immediate", "agent_id": None}
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["extract"] is False


@pytest.mark.asyncio
async def test_remote_backend_updates_and_deletes_fact():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self):
            self.patches = []
            self.deletes = []

        async def patch(self, url, json=None, headers=None):
            self.patches.append((url, json, headers))
            return FakeResponse({"ok": True, "id": "fact/a b", "text": "user works at Moonshot AI"})

        async def delete(self, url, headers=None):
            self.deletes.append((url, headers))
            return FakeResponse({"ok": True})

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    updated = await backend.set_fact_sensitive("fact/a b", False)
    deleted = await backend.delete_fact("fact/a b")

    patch_url, body, patch_headers = client.patches[-1]
    assert patch_url == "http://mnemotrace.test/v1/facts/fact%2Fa%20b/sensitivity"
    assert body == {"space_id": "synthetic-space", "sensitive": False}
    assert patch_headers["Authorization"] == "Bearer sk-test"
    delete_url, delete_headers = client.deletes[-1]
    assert delete_url == "http://mnemotrace.test/v1/facts/fact%2Fa%20b?space_id=synthetic-space"
    assert delete_headers["Authorization"] == "Bearer sk-test"
    assert updated["ok"] is True and deleted["ok"] is True


@pytest.mark.asyncio
async def test_remote_backend_gets_and_sets_focus():
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self):
            self.gets = []
            self.puts = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse({"track": ["project decisions"], "mute": []})

        async def put(self, url, json=None, headers=None):
            self.puts.append((url, json, headers))
            return FakeResponse({"ok": True, "focus": json})

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    current = await backend.get_focus()
    updated = await backend.set_focus(track=["project decisions"], mute=None)

    get_url, get_headers = client.gets[-1]
    assert get_url == "http://mnemotrace.test/v1/focus?space_id=synthetic-space"
    assert get_headers["Authorization"] == "Bearer sk-test"
    put_url, body, put_headers = client.puts[-1]
    assert put_url == "http://mnemotrace.test/v1/focus"
    assert body == {"space_id": "synthetic-space", "track": ["project decisions"]}
    assert put_headers["Authorization"] == "Bearer sk-test"
    assert current["track"] == ["project decisions"]
    assert updated["focus"]["track"] == ["project decisions"]


@pytest.mark.asyncio
async def test_remote_backend_flushes_session():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "session_id": "s1"}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None, timeout=None):
            assert timeout == 40
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.flush_processing("s1")

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/processing/flush"
    assert body == {"space_id": "synthetic-space", "session_id": "s1", "timeout": 30,
                    "retry_failed": False, "include_digests": False}
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["session_id"] == "s1"


@pytest.mark.asyncio
async def test_remote_backend_fetches_session_report():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "session_id": "codex:repo/thread 1", "facts": []}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.session_report("codex:repo/thread 1", include_sensitive=True, limit=10, offset=100)

    url, headers = client.gets[-1]
    assert url == (
        "http://mnemotrace.test/v1/sessions/report?"
        "session_id=codex%3Arepo%2Fthread+1&include_sensitive=true&limit=10&offset=100&space_id=synthetic-space"
    )
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_remote_backend_fetches_sessions_index():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "sessions": [{"id": "claude-code:repo/thread 2"}], "page": {}}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.sessions(limit=2, offset=4, q="claude-code")

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/sessions?limit=2&offset=4&q=claude-code&space_id=synthetic-space"
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["sessions"][0]["id"] == "claude-code:repo/thread 2"


@pytest.mark.asyncio
async def test_remote_backend_keeps_all_fact_categories():
    import httpx

    def respond(request):
        assert request.url.path == "/v1/memories"
        assert request.url.params.get_list("categories") == ["food", "music"]
        assert request.url.params.get_list("space_id") == ["synthetic-space"]
        return httpx.Response(200, json={"facts": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
        assert await backend.memories(categories=["food", "music"], limit=10, offset=0,
                                      include_superseded=False) == {"facts": []}


@pytest.mark.asyncio
async def test_remote_backend_exports_memory():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"format_version": 4, "include_sensitive": False, "facts": [], "episodes": []}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    safe = await backend.export()
    full = await backend.export(include_sensitive=True)

    assert client.gets[0][0] == "http://mnemotrace.test/v1/export?include_sensitive=false&space_id=synthetic-space"
    assert client.gets[0][1]["Authorization"] == "Bearer sk-test"
    assert client.gets[1][0] == "http://mnemotrace.test/v1/export?include_sensitive=true&space_id=synthetic-space"
    assert safe["format_version"] == 4
    assert full["format_version"] == 4


@pytest.mark.asyncio
async def test_remote_backend_forget_sends_confirm():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "message": "All memory erased."}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.forget()

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/forget"
    assert body == {"confirm": True, "space_id": "synthetic-space"}
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_remote_backend_fetches_stats():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"counts": {"episodes": 0}, "time_range": {}, "consolidation_backlog": False}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.stats()

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/stats?space_id=synthetic-space"
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["counts"]["episodes"] == 0


@pytest.mark.asyncio
async def test_remote_backend_fetches_agent_status():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "session_id": "codex:repo:thread"}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", space_id="synthetic-space", client=client)
    data = await backend.agent_status(session_id="codex:repo:thread")

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/agent/status?session_id=codex%3Arepo%3Athread&space_id=synthetic-space"
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_forget_requires_confirm(local_backend):
    await call("mnemotrace_remember", content="ephemeral note")
    guard = await call("mnemotrace_forget")  # 未带 confirm
    assert "confirm=true" in guard
    done = await call("mnemotrace_forget", confirm=True)
    assert "erased" in done.lower()
    facts = await call("mnemotrace_list_facts")
    assert "No facts stored yet" in facts


@pytest.mark.asyncio
async def test_audit_markdown_identifies_the_missing_edge(local_backend):
    memory = local_backend.svc.memory(local_backend.context)
    fact = seed_fact(memory, 'user', 'works_at', 'Private statement', linked=True, sensitive=False, text='user works at Private statement')
    with memory.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = %s', (fact.id,))
    report = await call('mnemotrace_audit')
    assert '`missing_edge`: 1' in report and fact.id in report
    assert fact.object not in report


def test_remote_error_preserves_structured_detail_for_conflict_and_validation():
    """#12：MCP 远程错误不应把 409/422/500 降级为裸状态码，需如实透传结构化 detail。"""
    import httpx

    def make(status, detail):
        req = httpx.Request('POST', 'http://x/v1/messages')
        resp = httpx.Response(status, json={'detail': detail}, request=req)
        return httpx.HTTPStatusError('err', request=req, response=resp)

    for status, code in [(409, 'message_conflict'), (409, 'extraction_conflict'),
                         (422, 'invalid_request'), (500, 'extraction_failed')]:
        detail = {'code': code, 'message': 'x', 'stored_raw': True, 'episode_ids': ['e1']}
        msg = S._err(make(status, detail))
        assert 'returned HTTP' not in msg, msg
        assert code in msg, msg
    # 无结构 detail 的状态码仍保持原有引导文案
    req = httpx.Request('POST', 'http://x/v1/x')
    plain = httpx.Response(418, json={'detail': 'plain'}, request=req)
    assert 'HTTP 418' in S._err(httpx.HTTPStatusError('e', request=req, response=plain))
