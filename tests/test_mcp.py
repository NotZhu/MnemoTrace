"""MCP 服务器工具测试：通过 FastMCP.call_tool 驱动，注入本地后端（hashing 嵌入器、固定合成模型响应）——
离线、零配置。验证各工具、其 schema，以及 markdown/json 输出。"""
from __future__ import annotations

import json
import re
import tempfile

import pytest

pytest.importorskip("mcp")

from mnemotrace.mcp import server as S  # noqa: E402
from mnemotrace.mcp.backends import LocalBackend, RemoteBackend  # noqa: E402
from tests.model_fixtures import MemoryService  # noqa: E402


@pytest.fixture()
def local_backend():
    d = tempfile.mkdtemp(prefix="mnemotrace_mcp_")
    svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
    S.set_backend(LocalBackend(namespace="me", service=svc))
    yield svc
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
    assert {"mnemotrace_remember", "mnemotrace_recall", "mnemotrace_close_session", "mnemotrace_session_report",
            "mnemotrace_list_sessions", "mnemotrace_search",
            "mnemotrace_list_facts", "mnemotrace_profile", "mnemotrace_agent_status", "mnemotrace_stats",
            "mnemotrace_add_fact", "mnemotrace_update_fact", "mnemotrace_delete_fact", "mnemotrace_get_focus",
            "mnemotrace_set_focus", "mnemotrace_import", "mnemotrace_export", "mnemotrace_forget"} <= set(tools)
    # 顶层平铺的参数（不嵌套在 "params" 之下）
    props = tools["mnemotrace_recall"].inputSchema.get("properties", {})
    assert "query" in props and "max_chunks" in props and "session_id" in props and "as_of" in props
    assert "redact_sensitive" in props
    remember_props = tools["mnemotrace_remember"].inputSchema.get("properties", {})
    assert "content" in remember_props and "session_id" in remember_props and "scope" in remember_props
    add_props = tools["mnemotrace_add_fact"].inputSchema.get("properties", {})
    assert {"subject", "predicate", "object", "sensitive", "category"} <= set(add_props)
    update_props = tools["mnemotrace_update_fact"].inputSchema.get("properties", {})
    assert {"fact_id", "subject", "predicate", "object", "sensitive", "category"} <= set(update_props)
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
    assert tools["mnemotrace_close_session"].annotations.readOnlyHint is False
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
    assert "Answer" in ans


@pytest.mark.asyncio
async def test_recall_can_include_session_working_memory(local_backend):
    local_backend.add_working("me", "today I am checking release notes", session_id="agent-thread-1")

    ctx = await call(
        "mnemotrace_recall",
        query="what am I doing right now?",
        max_chunks=0,
        session_id="agent-thread-1",
    )

    assert "release notes" in ctx


@pytest.mark.asyncio
async def test_remember_scope_working_keeps_state_out_of_durable_facts(local_backend):
    out = await call(
        "mnemotrace_remember",
        content="today I am checking release notes",
        session_id="agent-thread-1",
        scope="working",
    )

    assert "working memory" in out
    assert local_backend.stats("me")["counts"]["working_live"] == 1
    assert local_backend.stats("me")["counts"]["facts_live"] == 0
    ctx = await call(
        "mnemotrace_recall",
        query="what am I doing right now?",
        max_chunks=0,
        session_id="agent-thread-1",
    )
    assert "release notes" in ctx


@pytest.mark.asyncio
async def test_close_session_tool(local_backend):
    await call("mnemotrace_remember", content="I work at Acme.", session_id="s1")
    out = await call("mnemotrace_close_session", session_id="s1")
    assert "Closed session `s1`" in out
    assert "episode" in out

    raw = await call("mnemotrace_close_session", session_id="s1", response_format="json")
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["session_id"] == "s1"


@pytest.mark.asyncio
async def test_session_report_tool_audits_saved_facts(local_backend):
    session = "codex:super-memory:thread"
    await call(
        "mnemotrace_remember",
        content="Project decision: the launch checklist must include committed eval logs.",
        session_id=session,
    )
    await call("mnemotrace_close_session", session_id=session)

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
    await call("mnemotrace_add_fact", predicate="works_at", object="Moonshot AI")
    facts = await call("mnemotrace_list_facts")
    assert "Moonshot AI" in facts and "🔒" in facts  # 用户断言的事实 -> 上锁标记
    assert re.search(r"`ft_[^`]+`", facts)
    prof = await call("mnemotrace_profile")
    assert "User profile" in prof


@pytest.mark.asyncio
async def test_update_and_delete_fact_tools(local_backend):
    await call("mnemotrace_add_fact", predicate="works_at", object="ByteDance")
    raw = await call("mnemotrace_list_facts", response_format="json")
    fid = json.loads(raw)["facts"][0]["id"]

    updated = await call("mnemotrace_update_fact", fact_id=fid, object="Moonshot AI", sensitive=False)
    assert f"Updated fact `{fid}`" in updated
    facts = await call("mnemotrace_list_facts")
    assert "Moonshot AI" in facts
    assert "ByteDance" not in facts

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
    assert local_backend.get_focus("me") == {
        "track": ["project decisions"],
        "mute": ["health details"],
    }

    raw = await call("mnemotrace_get_focus", response_format="json")
    assert json.loads(raw) == {
        "track": ["project decisions"],
        "mute": ["health details"],
    }

    await call("mnemotrace_set_focus", mute=[], response_format="json")
    assert local_backend.get_focus("me") == {"track": ["project decisions"], "mute": []}

    guard = await call("mnemotrace_set_focus")
    assert "provide `track` or `mute`" in guard


@pytest.mark.asyncio
async def test_stats_tool_is_content_free(local_backend):
    await call("mnemotrace_remember", content="My private diagnosis is diabetes and I work at Acme.")
    await call("mnemotrace_add_fact", predicate="has_disease", object="diabetes")

    stats = await call("mnemotrace_stats")
    assert "MnemoTrace stats" in stats
    assert "Episodes:" in stats and "Consolidation backlog" in stats
    assert "Graph hygiene:" in stats
    assert "diabetes" not in stats.lower()
    assert "acme" not in stats.lower()

    raw = await call("mnemotrace_stats", response_format="json")
    data = json.loads(raw)
    assert data["counts"]["episodes"] >= 1
    assert "graph_orphan_entities" in data["counts"]
    assert "graph_stale_relations" in data["counts"]
    assert "time_range" in data
    assert "diabetes" not in raw.lower()


@pytest.mark.asyncio
async def test_export_tool_defaults_to_share_safe_payload(local_backend):
    await call("mnemotrace_add_fact", predicate="works_at", object="Moonshot AI", sensitive=False)
    await call("mnemotrace_add_fact", predicate="has_disease", object="diabetes", sensitive=True)

    summary = await call("mnemotrace_export")
    assert "MnemoTrace export" in summary
    assert "Include sensitive: False" in summary
    assert "Facts: 1" in summary
    assert "diabetes" not in summary.lower()

    safe = json.loads(await call("mnemotrace_export", response_format="json"))
    assert safe["include_sensitive"] is False
    assert safe["redacted_sensitive"] is True
    assert {f["object"] for f in safe["facts"]} == {"Moonshot AI"}
    assert safe["episodes"] == []
    assert safe["profile"] == ""

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
    local_backend.add_working("me", "today I am drafting a private launch note", session_id=session)
    await call("mnemotrace_set_focus", track=["project decisions"], mute=["health details"])

    status = await call("mnemotrace_agent_status", session_id=session)

    assert "MnemoTrace agent status" in status
    assert f"Session: `{session}`" in status
    assert "Session working memory: 1 live item" in status
    assert "project decisions" in status
    assert "mnemotrace_recall" in status
    assert "diabetes" not in status.lower()
    assert "acme" not in status.lower()
    assert "private launch note" not in status.lower()

    raw = await call("mnemotrace_agent_status", session_id=session, response_format="json")
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["session"]["working_live"] == 1
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
    local_backend.add_working("me", "temporary launch state", session_id=claude_session)

    summary = await call("mnemotrace_list_sessions", q="repo")

    assert "Memory sessions" in summary
    assert codex_session in summary
    assert claude_session in summary
    assert "diabetes" not in summary.lower()
    assert "launch notes" not in summary.lower()
    assert "temporary launch" not in summary.lower()

    raw = await call("mnemotrace_list_sessions", q="claude-code", response_format="json")
    data = json.loads(raw)
    assert data["sessions"][0]["id"] == claude_session
    assert data["sessions"][0]["working_live"] == 1
    assert data["sessions"][0]["facts_sensitive"] >= 1
    assert "diabetes" not in raw.lower()


@pytest.mark.asyncio
async def test_import_tool(local_backend):
    arr = '[{"role":"user","content":"I have a cat named Pixel."}]'
    out = await call("mnemotrace_import", content=arr, format="messages")
    assert "Imported" in out and "episode" in out
    await call("mnemotrace_flush_processing")
    ctx = await call("mnemotrace_recall", query="What pet do I have?")
    assert "Pixel" in ctx


@pytest.mark.asyncio
async def test_recall_and_search_support_as_of(local_backend):
    records = json.dumps([
        {"session_id": "old", "content": "Wei works at Tencent.", "event_time": 1_700_000_000.0},
        {"session_id": "new", "content": "Wei works at Moonshot AI.", "event_time": 1_702_592_000.0},
    ])
    await call("mnemotrace_import", content=records, format="records")
    await call("mnemotrace_flush_processing")

    ctx = await call(
        "mnemotrace_recall",
        query="Where does Wei work?",
        max_chunks=0,
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
    assert "Tencent" in data["answer"]
    assert all("Moonshot" not in fact for fact in data["facts"])


@pytest.mark.asyncio
async def test_recall_and_search_support_sensitive_redaction(local_backend):
    await call("mnemotrace_add_fact", predicate="has_disease", object="diabetes")
    await call("mnemotrace_add_fact", predicate="works_at", object="Acme")

    ctx = await call(
        "mnemotrace_recall",
        query="what do you know about me?",
        max_chunks=0,
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    await backend.recall(
        "Where does Wei work?",
        lean=False,
        session_id="s1",
        as_of=1_700_864_000.0,
        redact_sensitive=True,
    )

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/recall"
    assert body["session_id"] == "s1"
    assert body["as_of"] == 1_700_864_000.0
    assert body["redact_sensitive"] is True
    assert headers["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_remote_backend_forwards_remember_scope():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "scope": "working"}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.remember("temporary state", session_id="s1", scope="working")

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/remember"
    assert body == {"content": "temporary state", "session_id": "s1", "scope": "working", "processing": "immediate"}
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["scope"] == "working"


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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    updated = await backend.update_fact("fact/a b", object="Moonshot AI", sensitive=False)
    deleted = await backend.delete_fact("fact/a b")

    patch_url, body, patch_headers = client.patches[-1]
    assert patch_url == "http://mnemotrace.test/v1/facts/fact%2Fa%20b"
    assert body == {"object": "Moonshot AI", "sensitive": False}
    assert patch_headers["Authorization"] == "Bearer sk-test"
    delete_url, delete_headers = client.deletes[-1]
    assert delete_url == "http://mnemotrace.test/v1/facts/fact%2Fa%20b"
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    current = await backend.get_focus()
    updated = await backend.set_focus(track=["project decisions"], mute=None)

    get_url, get_headers = client.gets[-1]
    assert get_url == "http://mnemotrace.test/v1/focus"
    assert get_headers["Authorization"] == "Bearer sk-test"
    put_url, body, put_headers = client.puts[-1]
    assert put_url == "http://mnemotrace.test/v1/focus"
    assert body == {"track": ["project decisions"]}
    assert put_headers["Authorization"] == "Bearer sk-test"
    assert current["track"] == ["project decisions"]
    assert updated["focus"] == {"track": ["project decisions"]}


@pytest.mark.asyncio
async def test_remote_backend_closes_session():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True, "session_id": "s1"}

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json=None, headers=None):
            self.posts.append((url, json, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.close_session("s1", summarize=False, clear_working=False)

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/sessions/close"
    assert body == {"session_id": "s1", "summarize": False, "clear_working": False}
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.session_report("codex:repo/thread 1", include_sensitive=True)

    url, headers = client.gets[-1]
    assert url == (
        "http://mnemotrace.test/v1/sessions/report?"
        "session_id=codex%3Arepo%2Fthread+1&include_sensitive=true"
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.sessions(limit=2, offset=4, q="claude-code")

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/sessions?limit=2&offset=4&q=claude-code"
    assert headers["Authorization"] == "Bearer sk-test"
    assert data["sessions"][0]["id"] == "claude-code:repo/thread 2"


@pytest.mark.asyncio
async def test_remote_backend_exports_memory():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"include_sensitive": False, "facts": [], "episodes": []}

    class FakeClient:
        def __init__(self):
            self.gets = []

        async def get(self, url, headers=None):
            self.gets.append((url, headers))
            return FakeResponse()

    client = FakeClient()
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    safe = await backend.export()
    full = await backend.export(include_sensitive=True)

    assert client.gets[0][0] == "http://mnemotrace.test/v1/export?include_sensitive=false"
    assert client.gets[0][1]["Authorization"] == "Bearer sk-test"
    assert client.gets[1][0] == "http://mnemotrace.test/v1/export?include_sensitive=true"
    assert "mnemotrace_export_version" not in safe
    assert "mnemotrace_export_version" not in full


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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.forget()

    url, body, headers = client.posts[-1]
    assert url == "http://mnemotrace.test/v1/forget"
    assert body == {"confirm": True}
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.stats()

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/stats"
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
    backend = RemoteBackend("http://mnemotrace.test", key="sk-test", client=client)
    data = await backend.agent_status(session_id="codex:repo:thread")

    url, headers = client.gets[-1]
    assert url == "http://mnemotrace.test/v1/agent/status?session_id=codex%3Arepo%3Athread"
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
async def test_audit_markdown_names_the_slot_of_a_group_finding(local_backend):
    """`slot_overflow` 这类发现描述的是整个 (subject, predicate) 槽位，因此没有 `text` 字段——
    而 `text` 正是 markdown 渲染器逐条打印的事实字段。若没有回退处理，智能体会拿到一个空的加粗块，
    被要求"清空该槽位"却不知道是哪个槽位——而这条发现恰恰是唯一 remedy 为不可逆批量删除的发现。"""
    from mnemotrace.types import Fact

    mem = local_backend.get("me")
    for i in range(4):
        junk = Fact(subject="user", predicate="occupation", object=f"borrowed title {i}", user_id="me")
        junk.embedding = mem.embedder.embed(junk.text)
        mem.fact_store.upsert(junk.id, junk.embedding, junk)

    md = await call("mnemotrace_audit")
    assert "`slot_overflow`: 1" in md
    assert "**** —" not in md               # 不能渲染出空的加粗块
    assert "**user occupation ×4**" in md   # 指明是哪个槽位、规模多大
    assert "borrowed title 0" in md         # 行动建议里让读者查看的样本
