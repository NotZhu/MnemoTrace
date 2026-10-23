"""HTTP API 冒烟测试：针对重构后基于 MemoryService 的应用 —— 离线运行（hashing 嵌入器、
规则提取器），无需下载模型或 API key。覆盖 React 控制台 + SDK 依赖的所有路由形态，
以及新增的 /v1/import。"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    d = tempfile.mkdtemp(prefix="mnemotrace_srv_")
    # 与全局逐测试隔离保持同一作用域，避免开放模式在后续测试前被清理。
    for name, value in {
        "MNEMOTRACE_DATA_DIR": d,
        "MNEMOTRACE_EMBEDDER": "hashing",
        "MNEMOTRACE_OPEN": "1",
        "MNEMOTRACE_LLM": "",
        "MNEMOTRACE_ANSWERER": "",
        "MNEMOTRACE_API_KEYS": "",
        "MNEMOTRACE_ALLOW_ANONYMOUS": "",
    }.items():
        monkeypatch.setenv(name, value)
    from mnemotrace.server import app as appmod
    appmod._svc = None  # 绑定到测试环境变量的全新单例
    try:
        with TestClient(appmod.app) as c:
            yield c
    finally:
        appmod._svc = None
        shutil.rmtree(d, ignore_errors=True)


def hdr(ns: str) -> dict:
    return {"Authorization": f"Bearer {ns}"}  # open 模式下 -> bearer 内容即命名空间本身


def test_health(client):
    r = client.get("/health")
    data = r.json()
    assert r.status_code == 200 and data["ok"] is True and data["ready"] is True
    assert data["auth_mode"] == "open"
    assert data["anonymous_allowed"] is False
    assert data["embedder"] == "HashingEmbedder"
    assert data["llm_configured"] is False
    assert data["answerer_configured"] is False
    assert data["storage"] == "memory"
    assert data["max_hot_users"] >= data["users_hot"]
    assert data["max_hot_facts"] >= 1
    assert data["version"] == "0.1.0"
    assert "data_dir" not in data and "api_keys" not in data
    assert client.get("/ready").status_code == 200


def test_private_api_responses_are_not_cacheable(client):
    h = hdr("cache_headers")

    memories = client.get("/v1/memories", headers=h)
    exported = client.get("/v1/export", headers=h)

    assert memories.headers["cache-control"] == "no-store"
    assert exported.headers["cache-control"] == "no-store"
    for response in (memories, exported):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_remember_recall_roundtrip(client):
    h = hdr("alice")
    assert client.post("/v1/remember", json={"content": "My name is Wei and I live in Shenzhen."},
                       headers=h).json()["ok"] is True
    dump = client.get("/v1/memories", headers=h).json()
    assert dump["counts"]["episodes"] >= 1
    rec = client.post("/v1/recall", json={"query": "Where does the user live?"}, headers=h).json()
    assert "Shenzhen" in rec["context"] and rec["tokens_est"] > 0


def test_structured_profile_source_count_survives_edit_and_reload(client):
    h = hdr("synthetic-profile-evidence")
    remembered = client.post("/v1/remember", json={
        "content": "I like jazz.", "scope": "long",
    }, headers=h)
    assert remembered.status_code == 200

    def preferences():
        response = client.get("/v1/profile/structured", headers=h)
        assert response.status_code == 200
        return [item for items in response.json()["preferences"].values() for item in items]

    item = next(item for item in preferences() if item["item"] == "jazz")
    assert item["evidence"] == {"kind": "mentions", "count": 1, "source_count": 1}
    changed = client.patch(f"/v1/facts/{item['fact_id']}", json={"object": "classical music"}, headers=h)
    assert changed.status_code == 200

    from mnemotrace.server import app as appmod
    appmod._svc = None
    edited = next(row for row in preferences() if row["fact_id"] == item["fact_id"])
    assert edited["evidence"] == {"kind": "user", "count": 1, "source_count": 1}

    manual = client.post("/v1/facts", json={
        "subject": "user", "predicate": "likes", "object": "synthetic-manual-option",
    }, headers=h)
    assert manual.status_code == 200
    added = next(row for row in preferences() if row["item"] == "synthetic-manual-option")
    assert added["evidence"] == {"kind": "user", "count": 1, "source_count": 0}


def test_tenant_data_rights_operations_remain_isolated(client):
    tenant_a = hdr("isolation-tenant-a")
    tenant_b = hdr("isolation-tenant-b")
    fact_a = client.post(
        "/v1/facts",
        json={"predicate": "release_marker", "object": "alpha-private-marker"},
        headers=tenant_a,
    ).json()
    fact_b = client.post(
        "/v1/facts",
        json={"predicate": "release_marker", "object": "beta-private-marker"},
        headers=tenant_b,
    ).json()

    export_a = client.get("/v1/export", headers=tenant_a).json()
    export_b = client.get("/v1/export", headers=tenant_b).json()
    assert "alpha-private-marker" in str(export_a)
    assert "beta-private-marker" not in str(export_a)
    assert "beta-private-marker" in str(export_b)
    assert "alpha-private-marker" not in str(export_b)

    assert client.patch(
        f"/v1/facts/{fact_b['id']}", json={"object": "cross-tenant-edit"}, headers=tenant_a
    ).status_code == 404
    assert client.delete(f"/v1/facts/{fact_b['id']}", headers=tenant_a).json()["ok"] is False
    assert client.get("/v1/export", headers=tenant_b).json() == export_b

    erased = client.post("/v1/forget", json={"confirm": True}, headers=tenant_a)
    assert erased.status_code == 200 and erased.json()["ok"] is True
    assert "alpha-private-marker" not in str(client.get("/v1/export", headers=tenant_a).json())
    assert "beta-private-marker" in str(client.get("/v1/export", headers=tenant_b).json())
    assert fact_a["id"] != fact_b["id"]


def test_close_session_endpoint_consolidates_summarizes_and_clears_working(client):
    h = hdr("session_close")
    client.post("/v1/import", json={
        "format": "records",
        "consolidate": False,
        "summarize": False,
        "data": [{"session_id": "s1", "content": "I work at Acme."}],
    }, headers=h)
    client.post("/v1/working", json={
        "content": "today I am checking the release notes",
        "session_id": "s1",
    }, headers=h)

    out = client.post("/v1/sessions/close", json={"session_id": "s1"}, headers=h).json()

    assert out["ok"] is True
    assert out["session_id"] == "s1"
    assert out["episodes"] == 1
    assert out["pending_consolidated"] == 1
    assert out["facts_added"] >= 1
    assert out["summaries"] == 1
    assert out["working_cleared"] == 1
    assert client.get("/v1/working?session_id=s1", headers=h).json()["items"] == []
    assert client.get("/v1/memories", headers=h).json()["counts"]["summaries"] == 1


def test_http_handoff_across_agent_sessions_in_same_namespace(client):
    h = hdr("agent_handoff")
    codex_session = "codex:super-memory:handoff-source"
    claude_session = "claude-code:super-memory:handoff-target"

    written = client.post("/v1/remember", json={
        "content": "Project decision: the launch checklist must include committed eval logs.",
        "session_id": codex_session,
        "scope": "long",
    }, headers=h).json()
    assert written["ok"] is True
    assert written["extracted"] >= 1
    assert client.post("/v1/sessions/close", json={"session_id": codex_session}, headers=h).json()["ok"] is True

    recalled = client.post("/v1/recall", json={
        "query": "What launch checklist decision did Codex record?",
        "session_id": claude_session,
        "n_chunks": 3,
    }, headers=h).json()

    assert "committed eval logs" in recalled["context"]
    assert codex_session in recalled["context"]
    assert client.get(f"/v1/working?session_id={claude_session}", headers=h).json()["items"] == []


def test_session_report_endpoint_audits_saved_facts(client):
    h = hdr("session_report")
    session = "codex:super-memory:thread"
    client.post("/v1/remember", json={
        "content": "Project decision: the launch checklist must include committed eval logs.",
        "session_id": session,
        "scope": "long",
    }, headers=h)
    client.post("/v1/sessions/close", json={"session_id": session}, headers=h)

    report = client.get(f"/v1/sessions/report?session_id={session}", headers=h).json()

    assert report["ok"] is True
    assert report["session_id"] == session
    assert report["episodes"] == 1
    assert report["episodes_pending"] == 0
    assert report["facts_added"] >= 1
    assert "committed eval logs" in str(report)


def test_sessions_endpoint_lists_cross_agent_sessions_without_content(client):
    h = hdr("sessions_index")
    codex_session = "codex:super-memory:thread-1"
    claude_session = "claude-code:super-memory:thread-2"
    client.post("/v1/remember", json={
        "content": "Project decision: release notes require committed raw logs.",
        "session_id": codex_session,
        "scope": "long",
    }, headers=h)
    client.post("/v1/remember", json={
        "content": "My private diagnosis is diabetes.",
        "session_id": claude_session,
        "scope": "long",
    }, headers=h)
    client.post("/v1/working", json={
        "content": "temporary launch checklist state",
        "session_id": claude_session,
    }, headers=h)

    data = client.get("/v1/sessions?limit=10", headers=h).json()

    assert data["ok"] is True
    rows = {row["id"]: row for row in data["sessions"]}
    assert {codex_session, claude_session} <= set(rows)
    assert rows[codex_session]["episodes"] == 1
    assert rows[codex_session]["facts_added"] >= 1
    assert rows[claude_session]["working_live"] == 1
    assert rows[claude_session]["facts_sensitive"] >= 1
    assert data["page"]["total"] >= 2
    rendered = str(data).lower()
    assert "release notes" not in rendered
    assert "diabetes" not in rendered
    assert "temporary launch" not in rendered

    filtered = client.get("/v1/sessions?q=claude-code&limit=1", headers=h).json()
    assert filtered["page"]["total"] == 1
    assert filtered["sessions"][0]["id"] == claude_session


def test_stats_endpoint_is_content_free_namespace_observability(client):
    h = hdr("stats")
    client.post("/v1/remember", json={
        "content": "My private diagnosis is diabetes. I work at Acme.",
        "scope": "long",
    }, headers=h)
    client.post("/v1/facts", json={"predicate": "has_disease", "object": "diabetes"}, headers=h)
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Acme"}, headers=h)

    stats = client.get("/v1/stats", headers=h).json()
    assert stats["user"] == "stats"
    assert stats["counts"]["episodes"] >= 1
    assert stats["counts"]["episodes_consolidated"] >= 1
    assert stats["counts"]["episodes_pending"] == 0
    assert stats["counts"]["episodes_ephemeral"] == 0
    assert stats["counts"]["facts_hot"] >= 2
    assert stats["counts"]["facts_cold"] == 0
    assert stats["counts"]["facts_live"] >= 2
    assert stats["counts"]["facts_sensitive"] >= 1
    assert stats["counts"]["entities"] >= 1
    assert stats["counts"]["graph_orphan_entities"] == 0
    assert stats["counts"]["graph_stale_relations"] == 0
    assert stats["time_range"]["first_event_at"] is not None
    assert stats["storage"] == "memory"
    assert stats["max_hot_facts"] >= 1
    assert stats["embedder"] == "HashingEmbedder"
    assert stats["consolidation_backlog"] is False
    rendered = str(stats).lower()
    assert "diabetes" not in rendered
    assert "acme" not in rendered
    assert "private diagnosis" not in rendered
    assert "data_dir" not in stats and "profile" not in stats and "facts" not in stats


def test_agent_status_endpoint_is_content_free_and_session_aware(client):
    h = hdr("agent_status")
    session = "codex:super-memory:thread"
    client.post("/v1/remember", json={
        "content": "My private diagnosis is diabetes. I work at Acme.",
        "session_id": session,
        "scope": "long",
    }, headers=h)
    client.post("/v1/working", json={
        "content": "today I am preparing a private launch note",
        "session_id": session,
    }, headers=h)
    client.put("/v1/focus", json={
        "track": ["project decisions"],
        "mute": ["health details"],
    }, headers=h)

    status = client.get(f"/v1/agent/status?session_id={session}", headers=h).json()

    assert status["ok"] is True
    assert status["user"] == "agent_status"
    assert status["session_id"] == session
    assert status["session"]["episodes"] == 1
    assert status["session"]["working_live"] == 1
    assert status["focus"] == {"track": ["project decisions"], "mute": ["health details"]}
    assert status["counts"]["facts_live"] >= 1
    assert "mnemotrace_recall" in status["tools"]["read_context"]
    rendered = str(status).lower()
    assert "diabetes" not in rendered
    assert "acme" not in rendered
    assert "private launch note" not in rendered


def test_import_sessions_and_raw_data(client):
    h = hdr("importer")
    sessions = [{"session_id": "s1", "title": "Pets",
                 "messages": [{"role": "user", "content": "I have a dog named Mochi."},
                              {"role": "assistant", "content": "Mochi is a cute name!"}]}]
    out = client.post("/v1/import", json={"sessions": sessions}, headers=h).json()
    assert out["ok"] and out["episodes"] == 1
    # 原始数据 + format 路径
    out2 = client.post("/v1/import", json={
        "data": [{"role": "user", "content": "I drive a Tesla Model 3."}], "format": "messages"}, headers=h).json()
    assert out2["episodes"] == 1
    ctx = client.post("/v1/recall", json={"query": "What pet do I have?"}, headers=h).json()["context"]
    assert "Mochi" in ctx
    # 空的 import 请求应返回清晰的 400
    assert client.post("/v1/import", json={}, headers=h).status_code == 400


def test_import_sessions_accept_iso_timestamps(client):
    h = hdr("import_iso")
    out = client.post("/v1/import", json={
        "sessions": [{
            "session_id": "career",
            "messages": [
                {
                    "role": "user",
                    "content": "Wei works at Tencent.",
                    "event_time": "2023-11-14T00:00:00Z",
                },
                {
                    "role": "user",
                    "content": "Wei works at Moonshot AI.",
                    "event_time": "2023-12-14T00:00:00Z",
                },
            ],
        }],
        "summarize": False,
    }, headers=h).json()

    assert out["ok"] and out["sessions"] == 1 and out["episodes"] == 2
    memories = client.get("/v1/memories", headers=h).json()
    facts = memories["facts"]
    assert {f["object"] for f in facts} >= {"Tencent", "Moonshot AI"}
    past = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": False,
        "as_of": 1_700_864_000.0,
    }, headers=h).json()
    assert "Tencent" in past["answer"]


def test_fact_crud_and_focus_policy_graph_export(client):
    h = hdr("editor")
    fid = client.post("/v1/facts", json={"predicate": "works_at", "object": "Tencent"}, headers=h).json()["id"]
    assert client.patch(f"/v1/facts/{fid}", json={"object": "Moonshot AI"}, headers=h).json()["ok"]
    assert client.patch("/v1/facts/nope", json={"object": "x"}, headers=h).status_code == 404
    # focus + policy 经 service 层的往返测试
    assert client.put("/v1/focus", json={"track": ["career"], "mute": []}, headers=h).json()["focus"]["track"] == ["career"]
    assert client.get("/v1/focus", headers=h).json()["track"] == ["career"]
    pol = client.put("/v1/policy", json={"extract_instruction": "Record job changes."}, headers=h).json()
    assert pol["ok"] and pol["policy"]["extract_instruction"] == "Record job changes."
    assert "nodes" in client.get("/v1/graph", headers=h).json()
    exp = client.get("/v1/export", headers=h).json()
    assert exp["mnemotrace_export_version"] == 1 and any(f["object"] == "Moonshot AI" for f in exp["facts"])
    assert client.delete(f"/v1/facts/{fid}", headers=h).json()["ok"] is True


def test_fact_statement_edit_only_accepts_outcomes_and_is_persisted(client):
    h = hdr("synthetic_statement_editor")
    fid = client.post("/v1/facts", json={"predicate": "likes", "object": "USER_PREFERENCE_TOKEN"}, headers=h).json()["id"]
    before = client.get("/v1/audit", headers=h).json()
    finding = next(row for row in before["findings"] if row.get("fact_id") == fid)
    assert finding["display"]
    statement = "演示用户喜欢简洁的回答。"
    for patch in ({"statement": " "}, {"statement": ""}, {"statement": statement, "object": "回答"}):
        assert client.patch(f"/v1/facts/{fid}", json=patch, headers=h).status_code == 422
    assert client.patch(f"/v1/facts/{fid}", json={"statement": statement}, headers=h).status_code == 422
    dump = client.get("/v1/memories", headers=h).json()
    unchanged = next(row for row in dump["facts"] if row["id"] == fid)
    assert unchanged["object"] == "USER_PREFERENCE_TOKEN"
    assert client.patch(f"/v1/facts/{fid}", json={
        "subject": "user", "predicate": "likes", "object": "简洁的回答",
    }, headers=h).status_code == 200
    outcome_id = client.post("/v1/facts", json={
        "subject": "synthetic-session", "predicate": "decision", "object": "原来的结论",
    }, headers=h).json()["id"]
    result = client.patch(f"/v1/facts/{outcome_id}", json={"statement": statement}, headers=h)
    assert result.status_code == 200
    assert result.json()["text"] == result.json()["display"] == statement
    dump = client.get("/v1/memories", headers=h).json()
    edited = next(row for row in dump["facts"] if row["id"] == outcome_id)
    assert edited["object"] == edited["text"] == edited["display"] == statement
    assert edited["source"] == "user"
    from mnemotrace.server import app as appmod
    appmod._svc = None
    reloaded = client.get("/v1/memories", headers=h).json()["facts"]
    assert next(row for row in reloaded if row["id"] == fid)["object"] == "简洁的回答"
    assert next(row for row in reloaded if row["id"] == outcome_id)["object"] == statement


def test_graph_endpoint_supports_as_of_and_edge_audit_fields(client):
    h = hdr("graph_asof")
    client.post("/v1/import", json={
        "format": "records",
        "data": [
            {"session_id": "old", "content": "Wei works at Tencent.", "event_time": 1_700_000_000.0},
            {"session_id": "new", "content": "Wei works at Moonshot AI.", "event_time": 1_702_592_000.0},
        ],
    }, headers=h)

    past = client.get("/v1/graph?as_of=1700864000", headers=h).json()
    assert "Tencent" in {n["name"] for n in past["nodes"]}
    assert "Moonshot AI" not in {n["name"] for n in past["nodes"]}
    assert past["edges"] and all(e["live"] for e in past["edges"])

    edge = past["edges"][0]
    assert edge["fact_id"] and edge["fact_text"]
    assert edge["valid_at"] == 1_700_000_000.0
    assert edge["valid_at_h"]
    assert "invalid_at" in edge and "invalid_at_h" in edge
    assert isinstance(edge["provenance"], list)


def test_graph_endpoint_can_exclude_sensitive_edges(client):
    h = hdr("graph_redact")
    client.post("/v1/facts", json={"predicate": "has_disease", "object": "diabetes"}, headers=h)
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Acme"}, headers=h)

    default_safe = client.get("/v1/graph", headers=h).json()
    assert "diabetes" not in str(default_safe).lower()
    assert "Acme" in str(default_safe)

    full = client.get("/v1/graph?include_sensitive=true", headers=h).json()
    assert "diabetes" in str(full).lower()
    assert "Acme" in str(full)

    safe = client.get("/v1/graph?include_sensitive=false", headers=h).json()
    rendered = str(safe).lower()
    assert "diabetes" not in rendered
    assert "Acme" in str(safe)
    assert all("diabetes" not in e.get("fact_text", "").lower() for e in safe["edges"])


def test_memories_endpoint_supports_pagination_filtering_and_sensitive_create(client):
    h = hdr("memories_page")
    client.post("/v1/remember", json={"content": "First durable episode."}, headers=h)
    client.post("/v1/remember", json={"content": "Second durable episode."}, headers=h)
    client.post("/v1/facts", json={
        "predicate": "works_at",
        "object": "Acme",
        "category": "career",
    }, headers=h)
    client.post("/v1/facts", json={
        "predicate": "favorite_city",
        "object": "Hangzhou",
        "category": "preference",
    }, headers=h)
    client.post("/v1/facts", json={
        "predicate": "project_note",
        "object": "memory console",
        "category": "product",
    }, headers=h)
    client.post("/v1/facts", json={
        "predicate": "security_phrase",
        "object": "blue-lotus",
        "category": "security",
        "sensitive": True,
    }, headers=h)

    safe_default = client.get("/v1/memories?facts_limit=10&episodes_limit=1", headers=h).json()
    assert safe_default["profile"] == ""
    assert safe_default["episodes"] == []
    assert all(not f["sensitive"] for f in safe_default["facts"])

    episode_page = client.get(
        "/v1/memories?facts_limit=0&episodes_limit=1&include_sensitive=true",
        headers=h,
    ).json()
    assert len(episode_page["episodes"]) == 1
    assert episode_page["episodes_page"]["total"] == 2
    assert episode_page["episodes_page"]["has_more"] is True
    assert episode_page["next_offsets"]["episodes"] == episode_page["episodes_page"]["next_offset"]

    page = client.get(
        "/v1/memories?facts_limit=2&facts_offset=0&episodes_limit=1&include_sensitive=false",
        headers=h,
    ).json()
    assert len(page["facts"]) == 2
    assert page["facts_page"]["total"] >= 3
    assert page["facts_page"]["has_more"] is True
    assert page["facts_page"]["next_offset"] == 2
    assert page["episodes"] == []
    assert page["episodes_page"]["total"] == 0
    assert page["episodes_page"]["has_more"] is False
    assert page["next_offsets"] == {
        "facts": page["facts_page"]["next_offset"],
        "episodes": page["episodes_page"]["next_offset"],
    }
    assert "blue-lotus" not in str(page).lower()

    second = client.get(
        "/v1/memories?facts_limit=2&facts_offset=2&include_sensitive=false",
        headers=h,
    ).json()
    assert second["facts_page"]["offset"] == 2
    assert all(not f["sensitive"] for f in second["facts"])

    by_category = client.get(
        "/v1/memories?q=security&include_sensitive=true&episodes_limit=0",
        headers=h,
    ).json()
    assert any(f["object"] == "blue-lotus" and f["sensitive"] for f in by_category["facts"])

    safe_search = client.get(
        "/v1/memories?q=blue-lotus&include_sensitive=false&episodes_limit=0",
        headers=h,
    ).json()
    assert safe_search["facts_page"]["total"] == 0
    assert safe_search["facts"] == []


def _seed_category_filter_facts(user):
    from mnemotrace.server import app as appmod
    from mnemotrace.types import Episode, Fact

    mem = appmod.svc().get(user)
    rows = [
        Fact(subject="synthetic", predicate="note", object="alpha first", category="工作", valid_at=10, user_id=user),
        Fact(subject="synthetic", predicate="note", object="alpha second", category="工作", valid_at=20, user_id=user),
        Fact(subject="synthetic", predicate="note", object="beta latest", category="生活", valid_at=90, user_id=user),
        Fact(subject="synthetic", predicate="note", object="uncategorized", category=" ", valid_at=80, user_id=user),
        Fact(subject="synthetic", predicate="note", object="past alpha", category="旧分类", valid_at=5, invalid_at=6, user_id=user),
        Fact(subject="synthetic-session", predicate="decision", object="synthetic decision", category="会话结论", valid_at=95, user_id=user),
        Fact(subject="synthetic", predicate="note", object="private alpha", category="合成私有分类", sensitive=True, valid_at=100, user_id=user),
    ]
    for index, fact in enumerate(rows):
        store = mem.cold_store if index == 1 else mem.fact_store
        store.upsert(fact.id, [], fact)
    episode = Episode(content="synthetic source record", user_id=user, session_id="synthetic-session")
    mem.episodes_doc.put(episode.id, episode)
    return rows


def test_memories_category_filters_before_pagination_with_status_and_search(client):
    user = "synthetic-category-filter"
    rows = _seed_category_filter_facts(user)
    params = {"kind": "attributes", "status": "live", "facts_limit": 1, "episodes_limit": 0}
    unfiltered = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert unfiltered["facts"][0]["id"] == rows[2].id
    filtered = {**params, "category": "工作", "q": "alpha"}
    first = client.get("/v1/memories", params=filtered, headers=hdr(user)).json()
    second = client.get("/v1/memories", params={**filtered, "facts_offset": 1}, headers=hdr(user)).json()
    assert first["facts_page"]["total"] == 2
    assert first["facts_page"]["has_more"] is True
    assert {first["facts"][0]["id"], second["facts"][0]["id"]} == {rows[0].id, rows[1].id}
    assert first["counts"] == unfiltered["counts"]
    assert set(first["fact_categories"]) == {"工作", "生活", "其他", "旧分类"}
    old = client.get("/v1/memories", params={**params, "status": "superseded", "category": "旧分类"}, headers=hdr(user)).json()
    assert [fact["id"] for fact in old["facts"]] == [rows[4].id]
    other = client.get("/v1/memories", params={**params, "category": "其他"}, headers=hdr(user)).json()
    assert [fact["id"] for fact in other["facts"]] == [rows[3].id]
    empty = client.get("/v1/memories", params={**params, "category": "不存在的分类"}, headers=hdr(user)).json()
    assert empty["facts"] == [] and empty["facts_page"]["total"] == 0
    assert empty["fact_categories"] == first["fact_categories"]


def test_memories_category_facets_respect_visibility_kind_and_namespace(client):
    user = "synthetic-category-facets"
    rows = _seed_category_filter_facts(user)
    params = {"kind": "attributes", "facts_limit": 0, "episodes_limit": 0, "q": "no match"}
    safe = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert set(safe["fact_categories"]) == {"工作", "生活", "其他", "旧分类"}
    private = client.get("/v1/memories", params={**params, "include_sensitive": True}, headers=hdr(user)).json()
    assert "合成私有分类" in private["fact_categories"]
    outcomes = client.get("/v1/memories", params={**params, "kind": "outcomes"}, headers=hdr(user)).json()
    assert outcomes["fact_categories"] == ["会话结论"]
    other_space = client.get("/v1/memories", params=params, headers=hdr("synthetic-empty-space")).json()
    assert other_space["fact_categories"] == []
    with_episodes = client.get("/v1/memories", params={"kind": "attributes", "category": "工作", "include_sensitive": True}, headers=hdr(user)).json()
    assert len(with_episodes["episodes"]) == 1
    assert {fact["id"] for fact in with_episodes["facts"]} == {rows[0].id, rows[1].id}


def test_sensitive_edit_preserves_history_across_reload(client):
    h = hdr("metadata_history")
    claim = {"subject": "demo_user", "predicate": "lives_in", "object": "北京"}
    old_response = client.post("/v1/facts", json=claim, headers=h)
    assert old_response.status_code == 200
    old_id = old_response.json()["id"]
    current_response = client.post("/v1/facts", json={**claim, "object": "上海"}, headers=h)
    assert current_response.status_code == 200
    current_id = current_response.json()["id"]
    url = "/v1/memories?include_sensitive=true&episodes_limit=0"
    original = {f["id"]: f for f in client.get(url, headers=h).json()["facts"]}
    assert original[old_id]["status"] == "superseded"

    for sensitive in (True, False):
        response = client.patch(f"/v1/facts/{old_id}", json={**claim, "sensitive": sensitive}, headers=h)
        assert response.status_code == 200
        # 重新加载持久化状态，不能只在热缓存中保留历史。
        from mnemotrace.server import app as appmod
        appmod._svc = None
        rows = {f["id"]: f for f in client.get(url, headers=h).json()["facts"]}
        assert rows[old_id]["sensitive"] is sensitive
        assert rows[old_id]["status"] == "superseded"
        assert rows[old_id]["invalid_at"] == original[old_id]["invalid_at"]
        assert rows[current_id]["status"] == "live"
        assert rows[current_id]["supersedes"] == old_id
        live = client.get("/v1/memories?status=live&include_sensitive=true", headers=h).json()["facts"]
        assert {f["id"] for f in live} == {current_id}
        graph = client.get("/v1/graph?live_only=true&include_sensitive=true", headers=h).json()
        assert all(edge["fact_id"] != old_id for edge in graph["edges"])
        if sensitive:
            safe = client.get("/v1/memories?include_sensitive=false", headers=h).json()["facts"]
            assert all(f["id"] != old_id for f in safe)


def test_metadata_edit_preserves_extracted_source_across_reload(client):
    h = hdr("metadata_source")
    response = client.post("/v1/remember", json={"content": "I live in Beijing."}, headers=h)
    assert response.status_code == 200
    url = "/v1/memories?include_sensitive=true&episodes_limit=0"
    original = client.get(url, headers=h).json()["facts"][0]
    assert original["source"] == "extracted" and original["provenance"]
    claim = {field: original[field] for field in ("subject", "predicate", "object")}
    fact_id = original["id"]

    for patch in ({"sensitive": True}, {"sensitive": False}, {"category": "测试分类"}, {}):
        response = client.patch(f"/v1/facts/{fact_id}", json={**claim, **patch}, headers=h)
        assert response.status_code == 200
        assert response.json()["source"] == "extracted"
        from mnemotrace.server import app as appmod
        appmod._svc = None
        restored = next(f for f in client.get(url, headers=h).json()["facts"] if f["id"] == fact_id)
        assert restored["source"] == "extracted"
        assert restored["provenance"] == original["provenance"]
        assert restored["status"] == original["status"]
        for field, value in patch.items():
            assert restored[field] == value

    response = client.patch(f"/v1/facts/{fact_id}", json={"object": "Shanghai"}, headers=h)
    assert response.status_code == 200 and response.json()["source"] == "user"
    appmod._svc = None
    restored = next(f for f in client.get(url, headers=h).json()["facts"] if f["id"] == fact_id)
    assert restored["source"] == "user" and restored["object"] == "Shanghai"
    assert restored["provenance"] == original["provenance"]


def test_graph_endpoint_supports_query_live_only_and_limit(client):
    h = hdr("graph_query")
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Acme"}, headers=h)
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Moonshot AI"}, headers=h)
    client.post("/v1/facts", json={"predicate": "likes_food", "object": "ramen"}, headers=h)

    old_company = client.get("/v1/graph?q=Acme&live_only=true", headers=h).json()
    assert "Acme" not in str(old_company)

    current_company = client.get("/v1/graph?q=Moonshot&live_only=true&limit=1", headers=h).json()
    assert len(current_company["edges"]) <= 1
    assert "Moonshot AI" in str(current_company)
    assert all(edge["live"] for edge in current_company["edges"])


def test_recall_endpoint_supports_as_of_for_search_and_lean_context(client):
    h = hdr("recall_asof")
    client.post("/v1/import", json={
        "format": "records",
        "data": [
            {"session_id": "old", "content": "Wei works at Tencent.", "event_time": 1_700_000_000.0},
            {"session_id": "new", "content": "Wei works at Moonshot AI.", "event_time": 1_702_592_000.0},
        ],
    }, headers=h)

    past = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": False,
        "as_of": 1_700_864_000.0,
    }, headers=h).json()
    assert past["as_of"] == 1_700_864_000.0
    assert "Tencent" in past["answer"]
    assert all("Moonshot" not in f for f in past["facts"])

    current = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": False,
    }, headers=h).json()
    assert current["as_of"] is None
    assert "Moonshot AI" in current["answer"]

    ctx = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": True,
        "n_chunks": 0,
        "as_of": 1_700_864_000.0,
    }, headers=h).json()
    assert ctx["as_of"] == 1_700_864_000.0
    assert "Tencent" in ctx["context"]
    assert "Moonshot AI" not in ctx["context"]

    current_ctx = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": True,
        "n_chunks": 0,
    }, headers=h).json()
    assert current_ctx["as_of"] is None
    assert current_ctx["full_tokens"] > ctx["full_tokens"]


def test_recall_endpoint_can_redact_sensitive_facts(client):
    h = hdr("recall_redact")
    client.post("/v1/facts", json={"predicate": "has_disease", "object": "diabetes"}, headers=h)
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Acme"}, headers=h)

    ctx = client.post("/v1/recall", json={
        "query": "what do you know about me?",
        "lean": True,
        "n_chunks": 0,
        "redact_sensitive": True,
    }, headers=h).json()
    assert ctx["redacted_sensitive"] is True
    assert "Acme" in ctx["context"]
    assert "diabetes" not in ctx["context"].lower()

    direct = client.post("/v1/recall", json={
        "query": "what disease do I have?",
        "lean": False,
        "redact_sensitive": True,
    }, headers=h).json()
    assert direct["redacted_sensitive"] is True
    assert "diabetes" not in direct["answer"].lower()
    assert all("diabetes" not in fact.lower() for fact in direct["facts"])


def test_export_without_sensitive_omits_free_text_layers_and_sensitive_graph(client):
    h = hdr("export_redact")
    client.post("/v1/remember", json={
        "content": "My private diagnosis is diabetes. I work at Acme.",
    }, headers=h)
    client.post("/v1/facts", json={"predicate": "has_disease", "object": "diabetes"}, headers=h)
    client.post("/v1/facts", json={"predicate": "works_at", "object": "Acme"}, headers=h)

    default_safe = client.get("/v1/export", headers=h).json()
    assert default_safe["include_sensitive"] is False
    assert default_safe["redacted_sensitive"] is True
    assert "diabetes" not in str(default_safe).lower()
    assert default_safe["profile"] == ""
    assert default_safe["episodes"] == []

    full = client.get("/v1/export?include_sensitive=true", headers=h).json()
    assert full["include_sensitive"] is True
    assert any("diabetes" in f["text"].lower() for f in full["facts"])
    assert full["episodes"]

    safe = client.get("/v1/export?include_sensitive=false", headers=h).json()
    rendered = str(safe).lower()
    assert safe["include_sensitive"] is False
    assert safe["redacted_sensitive"] is True
    assert safe["profile"] == ""
    assert safe["episodes"] == []
    assert "diabetes" not in rendered
    assert any(f["object"] == "Acme" for f in safe["facts"])
    assert all("diabetes" not in e.get("fact_text", "").lower() for e in safe["graph"]["edges"])


@pytest.mark.parametrize("namespace", ["test", "1"])
def test_forget_policy_allows_regular_namespaces_with_confirmation(monkeypatch, namespace):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from mnemotrace.server import app as appmod

    deleted = []

    def forget(user):
        deleted.append(user)
        return {"ok": True}

    monkeypatch.setattr(appmod, "svc", lambda: SimpleNamespace(forget=forget))
    with pytest.raises(HTTPException) as missing_confirmation:
        appmod.forget(user=namespace)
    assert missing_confirmation.value.status_code == 400
    for protected in ("anonymous", "mnemotrace-test"):
        with pytest.raises(HTTPException) as protected_error:
            appmod.forget(req=appmod.ForgetReq(confirm=True), user=protected)
        assert protected_error.value.status_code == 403
    assert deleted == []
    assert appmod.forget(req=appmod.ForgetReq(confirm=True), user=namespace) == {"ok": True}
    assert deleted == [namespace]


@pytest.mark.parametrize("namespace", ["ephemeral", "test", "1"])
def test_forget_clears_namespace(client, namespace):
    h = hdr(namespace)
    client.post("/v1/remember", json={"content": "Temporary note."}, headers=h)
    guard = client.post("/v1/forget", headers=h)
    assert guard.status_code == 400
    assert "confirm=true" in guard.json()["detail"]
    assert client.get("/v1/memories", headers=h).json()["counts"]["episodes"] == 1
    assert client.post("/v1/forget", json={"confirm": True}, headers=h).json()["ok"] is True
    assert client.get("/v1/memories", headers=h).json()["counts"]["episodes"] == 0


def test_forget_rejects_public_demo_and_anonymous_namespaces(client):
    assert client.post("/v1/forget", json={"confirm": True}, headers=hdr("mnemotrace-test")).status_code == 403
    assert client.post("/v1/forget", json={"confirm": True}, headers=hdr("anonymous")).status_code == 403

    d = tempfile.mkdtemp(prefix="mnemotrace_anon_forget_")
    saved = {k: os.environ.get(k) for k in [
        "MNEMOTRACE_DATA_DIR",
        "MNEMOTRACE_EMBEDDER",
        "MNEMOTRACE_OPEN",
        "MNEMOTRACE_API_KEYS",
        "MNEMOTRACE_ALLOW_ANONYMOUS",
    ]}
    os.environ.update(
        MNEMOTRACE_DATA_DIR=d,
        MNEMOTRACE_EMBEDDER="hashing",
        MNEMOTRACE_OPEN="1",
        MNEMOTRACE_ALLOW_ANONYMOUS="1",
    )
    os.environ.pop("MNEMOTRACE_API_KEYS", None)
    try:
        from mnemotrace.server import app as appmod
        appmod._svc = None
        with TestClient(appmod.app) as c:
            assert c.post("/v1/forget").status_code == 403
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from mnemotrace.server import app as appmod
        appmod._svc = None
        shutil.rmtree(d, ignore_errors=True)


def test_static_cache_headers_and_robots(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /ui" in robots.text
    assert "Disallow: /v1/" in robots.text

    root = client.get("/", follow_redirects=False)
    assert root.headers["Cache-Control"] == "no-cache"

    from mnemotrace.server import app as appmod
    if not appmod._spa_built():
        pytest.skip("SPA dist is not built")
    asset_dir = os.path.join(appmod._DIST, "assets")
    asset = sorted(os.listdir(asset_dir))[0]
    ui_head = client.head("/ui")
    assert ui_head.status_code == 200
    assert ui_head.headers["Cache-Control"] == "no-cache"
    resp = client.get(f"/ui/assets/{asset}")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_missing_console_only_explains_build_and_api_still_works(client, monkeypatch, tmp_path):
    from mnemotrace.server import app as appmod

    monkeypatch.setattr(appmod, "_DIST", str(tmp_path / "missing-console"))
    for path in ("/", "/ui", "/ui/", "/ui/facts"):
        response = client.get(path)
        assert response.status_code == 503
        assert "pnpm" in response.text and "build" in response.text
        assert "<script" not in response.text and "<input" not in response.text
        assert response.headers["Cache-Control"] == "no-cache"
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.post("/v1/recall", json={"query": "hi"}, headers=hdr("synthetic-ui-test")).status_code == 200


def test_built_console_redirect_and_deep_links(client, monkeypatch, tmp_path):
    from mnemotrace.server import app as appmod

    (tmp_path / "index.html").write_text("<!doctype html><title>Synthetic console</title>", encoding="utf-8")
    monkeypatch.setattr(appmod, "_DIST", str(tmp_path))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/ui/"
    assert "Synthetic console" in client.get("/ui/facts").text


def test_auth_rejects_when_keys_configured():
    """配置 MNEMOTRACE_API_KEYS 且未启用 open 模式时，错误的 key 应返回 401。"""
    d = tempfile.mkdtemp(prefix="mnemotrace_auth_")
    os.environ.update(MNEMOTRACE_DATA_DIR=d, MNEMOTRACE_EMBEDDER="hashing", MNEMOTRACE_API_KEYS="alice:sk-good")
    os.environ.pop("MNEMOTRACE_OPEN", None)
    try:
        from mnemotrace.server import app as appmod
        appmod._svc = None
        with TestClient(appmod.app) as c:
            health = c.get("/health").json()
            assert health["auth_mode"] == "api_keys"
            assert health["anonymous_allowed"] is False
            assert c.post("/v1/recall", json={"query": "hi"}, headers=hdr("sk-bad")).status_code == 401
            malformed = c.post(
                "/v1/recall",
                json={"query": "hi"},
                headers={"Authorization": "Basic sk-good"},
            )
            assert malformed.status_code == 401
            assert "Authorization: Bearer <token>" in malformed.json()["detail"]
            assert c.post("/v1/recall", json={"query": "hi"},
                          headers={"Authorization": "Bearer sk-good"}).status_code == 200
    finally:
        # 恢复 open 模式，供后续模块使用
        os.environ.pop("MNEMOTRACE_API_KEYS", None)
        os.environ["MNEMOTRACE_OPEN"] = "1"
        shutil.rmtree(d, ignore_errors=True)


def test_api_key_config_supports_rotation_and_rejects_ambiguity(monkeypatch):
    from mnemotrace.server import app as appmod

    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "alice:key-new,alice:key-old,bob:key-b")
    assert appmod._load_keys() == {
        "key-new": "alice",
        "key-old": "alice",
        "key-b": "bob",
    }

    for invalid in ("missing-separator", ":empty-user", "empty-key:"):
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", invalid)
        with pytest.raises(appmod.AuthConfigError):
            appmod._load_keys()

    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "alice:shared,bob:shared")
    with pytest.raises(appmod.AuthConfigError):
        appmod._load_keys()


@pytest.mark.parametrize("configuration", ["api_keys", "spaces"])
def test_non_ascii_key_configuration_returns_503_without_leaking_key(client, monkeypatch, configuration):
    import json

    if configuration == "api_keys":
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", "tenant-a:synthetic-good,tenant-b:中文密钥")
    else:
        monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({
            "synthetic-good": {"home": "tenant-a"}, "中文密钥": {"home": "tenant-b"},
        }))
    response = client.post("/v1/recall", json={"query": "hi"}, headers=hdr("synthetic-good"))
    assert response.status_code == 503
    assert response.json()["detail"] == "invalid API key configuration"
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["auth_mode"] == "invalid"
    ready = client.get("/ready")
    assert ready.status_code == 503
    for result in (response, health, ready):
        assert "synthetic-good" not in result.text and "中文密钥" not in result.text


@pytest.mark.parametrize("mode", ["api_keys", "spaces", "open"])
def test_non_ascii_bearer_is_401_and_valid_key_still_works(client, monkeypatch, mode):
    import json

    if mode == "api_keys":
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", "tenant-a:synthetic-good")
    elif mode == "spaces":
        monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({"synthetic-good": {"home": "tenant-a"}}))
    # Raw header bytes exercise Latin-1 decoding at the actual ASGI request boundary.
    headers = [(b"Authorization", b"Bearer caf\xe9-key")]
    assert client.get("/v1/memories", headers=headers).status_code == 401
    assert client.post("/v1/recall", json={"query": "hi"}, headers=headers).status_code == 401
    valid = client.post("/v1/recall", json={"query": "hi"}, headers=hdr("synthetic-good"))
    assert valid.status_code == 200


def test_disabled_auth_is_live_but_not_ready():
    d = tempfile.mkdtemp(prefix="mnemotrace_disabled_auth_")
    os.environ.update(MNEMOTRACE_DATA_DIR=d, MNEMOTRACE_EMBEDDER="hashing")
    # 显式置空而不是 pop：MemoryService 初始化会 load_dotenv()，pop 掉的变量会被本地 .env 重新注入
    os.environ["MNEMOTRACE_API_KEYS"] = ""
    os.environ["MNEMOTRACE_OPEN"] = ""
    os.environ["MNEMOTRACE_ALLOW_ANONYMOUS"] = ""
    try:
        from mnemotrace.server import app as appmod

        appmod._svc = None
        with TestClient(appmod.app) as c:
            health = c.get("/health")
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert health.json()["ready"] is False
            assert health.json()["auth_mode"] == "disabled"
            assert c.get("/ready").status_code == 503
            assert c.post("/v1/recall", json={"query": "hi"}).status_code == 401
    finally:
        os.environ["MNEMOTRACE_OPEN"] = "1"
        os.environ.pop("MNEMOTRACE_API_KEYS", None)
        shutil.rmtree(d, ignore_errors=True)


def test_request_limit_rejects_before_memory_write():
    d = tempfile.mkdtemp(prefix="mnemotrace_request_limit_")
    os.environ.update(
        MNEMOTRACE_DATA_DIR=d,
        MNEMOTRACE_EMBEDDER="hashing",
        MNEMOTRACE_OPEN="1",
        MNEMOTRACE_MAX_REQUEST_BYTES="128",
    )
    os.environ.pop("MNEMOTRACE_API_KEYS", None)
    try:
        from mnemotrace.server import app as appmod

        appmod._svc = None
        with TestClient(appmod.app) as c:
            declared_response = c.post(
                "/v1/remember",
                content='{"content":"' + ("x" * 512) + '"}',
                headers={**hdr("request-limit"), "Content-Type": "application/json"},
            )
            chunked_response = c.post(
                "/v1/remember",
                content=iter([b'{"content":"', b"y" * 512, b'"}']),
                headers={**hdr("request-limit"), "Content-Type": "application/json"},
            )
            for response in (declared_response, chunked_response):
                assert response.status_code == 413
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
            assert c.get("/v1/stats", headers=hdr("request-limit")).json()["counts"]["episodes"] == 0
    finally:
        os.environ["MNEMOTRACE_OPEN"] = "1"
        os.environ.pop("MNEMOTRACE_MAX_REQUEST_BYTES", None)
        shutil.rmtree(d, ignore_errors=True)


def test_open_mode_requires_bearer_namespace_by_default():
    d = tempfile.mkdtemp(prefix="mnemotrace_open_auth_")
    os.environ.update(MNEMOTRACE_DATA_DIR=d, MNEMOTRACE_EMBEDDER="hashing", MNEMOTRACE_OPEN="1")
    os.environ.pop("MNEMOTRACE_API_KEYS", None)
    os.environ.pop("MNEMOTRACE_ALLOW_ANONYMOUS", None)
    try:
        from mnemotrace.server import app as appmod
        appmod._svc = None
        with TestClient(appmod.app) as c:
            missing = c.post("/v1/recall", json={"query": "hi"})
            assert missing.status_code == 401
            assert "Bearer <namespace>" in missing.json()["detail"]
            malformed = c.post(
                "/v1/recall",
                json={"query": "hi"},
                headers={"Authorization": "Basic demo"},
            )
            assert malformed.status_code == 401
            assert "Authorization: Bearer <token>" in malformed.json()["detail"]
            assert c.post("/v1/recall", json={"query": "hi"}, headers=hdr("demo")).status_code == 200
    finally:
        os.environ["MNEMOTRACE_OPEN"] = "1"
        os.environ.pop("MNEMOTRACE_API_KEYS", None)
        os.environ.pop("MNEMOTRACE_ALLOW_ANONYMOUS", None)
        shutil.rmtree(d, ignore_errors=True)


def test_open_mode_allows_anonymous_only_when_explicitly_enabled():
    d = tempfile.mkdtemp(prefix="mnemotrace_open_anon_")
    os.environ.update(
        MNEMOTRACE_DATA_DIR=d,
        MNEMOTRACE_EMBEDDER="hashing",
        MNEMOTRACE_OPEN="1",
        MNEMOTRACE_ALLOW_ANONYMOUS="1",
    )
    os.environ.pop("MNEMOTRACE_API_KEYS", None)
    try:
        from mnemotrace.server import app as appmod
        appmod._svc = None
        with TestClient(appmod.app) as c:
            assert c.post("/v1/recall", json={"query": "hi"}).status_code == 200
    finally:
        os.environ["MNEMOTRACE_OPEN"] = "1"
        os.environ.pop("MNEMOTRACE_API_KEYS", None)
        os.environ.pop("MNEMOTRACE_ALLOW_ANONYMOUS", None)
        shutil.rmtree(d, ignore_errors=True)


def test_audit_flags_junk_and_says_what_to_do(client):
    """记忆要赢得信任，前提是可被检查。对真实语料做提取时，难免产生不像值的结果 —— 原始 token、
    与谓词重复的对象、被截断的句子、代词主语 —— 不逐条人工审查就完全看不到这些问题。
    每条 finding 都必须说明原因（why）并给出可执行的动作（action）。"""
    h = hdr("audit-ns")
    junk = [
        ("interested_in", "kind_bar_fruit_nut_flavor"),      # 机器生成的 token
        ("occupation", "occupation"),                         # 什么信息都没有
        ("note", 'open keystone."'),                          # 被截断的句子
        ("stores_in", "store/memory_store.py"),               # 代码产物
    ]
    for pred, obj in junk:
        r = client.post("/v1/facts", json={"subject": "user", "predicate": pred, "object": obj}, headers=h)
        assert r.status_code == 200
    client.post("/v1/facts", json={"subject": "user", "predicate": "works_at", "object": "Moonshot AI"},
                headers=h)

    data = client.get("/v1/audit", headers=h).json()
    assert data["total_findings"] >= 4, data
    kinds = set(data["by_kind"])
    assert {"machine_token", "empty_value"} <= kinds, kinds
    for f in data["findings"]:
        assert f["why"] and f["action"], f  # 无法据以行动的 finding 就是噪音
    # 干净的事实不应被标记
    assert not any("Moonshot" in str(f.get("text", "")) for f in data["findings"])


def test_audit_finding_can_be_fixed_in_place(client):
    """核心是形成闭环：被标记 -> 修正 -> 消失，全程不离开控制台。修正还必须生效
    （source=user），否则自动提取会悄悄把它改回去。"""
    h = hdr("audit-fix")
    created = client.post("/v1/facts",
                          json={"subject": "user", "predicate": "likes", "object": "small_luxury_ships"},
                          headers=h).json()
    fact_id = created.get("id") or created.get("fact", {}).get("id")
    assert fact_id

    before = client.get("/v1/audit", headers=h).json()
    assert any(f.get("fact_id") == fact_id for f in before["findings"])

    r = client.patch(f"/v1/facts/{fact_id}", json={"object": "small luxury cruises"}, headers=h)
    assert r.status_code == 200
    assert r.json().get("source") == "user", r.json()

    after = client.get("/v1/audit", headers=h).json()
    assert not any(f.get("fact_id") == fact_id for f in after["findings"])
    assert after["total_findings"] < before["total_findings"]


def test_audit_sorts_before_limit_and_preserves_equal_priority_order(client):
    h = hdr("synthetic-audit-order")
    # 故意先写较低优先级项目；limit=1 时也必须返回最后写入的红色问题。
    for subject, predicate, obj in [
        ("synthetic-a", "path", "demo/config.json"),
        ("synthetic-b", "likes", "MACHINE_TOKEN_SYNTHETIC"),
        ("synthetic-c", "note", "[clipped text"),
        ("synthetic-d", "occupation", "occupation"),
    ]:
        assert client.post("/v1/facts", json={"subject": subject, "predicate": predicate, "object": obj}, headers=h).status_code == 200
    full = client.get("/v1/audit?limit=60", headers=h).json()
    assert [row["kind"] for row in full["findings"]] == [
        "empty_value", "machine_token", "fragment", "code_artifact",
    ]
    limited = client.get("/v1/audit?limit=1", headers=h).json()
    assert [row["kind"] for row in limited["findings"]] == ["empty_value"]
    assert limited["total_findings"] == full["total_findings"] == 4
    assert limited["by_kind"] == full["by_kind"]
    assert limited["truncated"] is True


def test_import_agent_sessions_defaults_to_outcomes_only_and_feed_stats(client):
    """watcher 上报的 agent 会话带 `metadata.source` 但不带 `consolidate` 键：服务端必须把它们
    存起来留到 close 时再蒸馏，绝不逐轮提取（RuleExtractor 曾把 12 个真实轮次变成 11 条垃圾
    fact）；同时 stats 必须记录记忆最后一次被喂入的时间。"""
    h = hdr("feed-ns")
    transcript = [{"role": "user", "content": "The config file is nearly empty. This is now clear: EKOS "
                                              "is deployed on an Ubuntu server."},
                  {"role": "assistant", "content": "This confirms the deployment; next is nginx."}]
    body = {"sessions": [
        {"session_id": "claude-code:a", "event_time": 1_756_000_000.0,
         "metadata": {"source": "agent_session"}, "messages": transcript},
        {"session_id": "codex:b", "event_time": 1_756_000_100.0,
         "metadata": {"source": "agent_session"}, "messages": transcript},
        {"session_id": "plain", "messages": [{"role": "user", "content": "I live in Shenzhen and work "
                                                                         "at Moonshot AI."}]},
    ]}
    r = client.post("/v1/import", json=body, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["episodes"] == 3
    assert data["facts_deferred"] == 2 and data["deferred_reason"] == "outcomes_only", data
    assert data["facts_added"] >= 1  # 普通 session 仍会走规则提取器

    closed = client.post("/v1/sessions/close", json={"session_id": "claude-code:a"}, headers=h).json()
    assert closed["pending_consolidated"] == 0 and closed["facts_added"] == 0, closed

    feed = client.get("/v1/stats", headers=h).json()["feed"]
    assert feed["sessions"] == 2 and feed["conclusions"] == 0
    assert feed["last_fed_at"] is not None and feed["last_fed_at_h"], feed

    status = client.get("/v1/agent/status", headers=h).json()
    assert status["feed"]["last_fed_at"] is not None and status["feed"]["sessions"] == 2

    # 未挂 LLM 时请求提取：返回的 reason 会说明原因，且不做任何提取
    r = client.post("/v1/import", json={"consolidate": True, "sessions": [
        {"session_id": "codex:c", "metadata": {"source": "agent_session"}, "messages": transcript}]},
        headers=h).json()
    assert r["facts_deferred"] == 1 and r["deferred_reason"] == "no_llm", r

    # 从未被喂入的命名空间会如实上报
    empty = client.get("/v1/stats", headers=hdr("never-fed")).json()["feed"]
    assert empty == {"last_fed_at": None, "last_fed_at_h": None, "sessions": 0, "conclusions": 0}


def test_memories_outcome_kind_and_revision_contract(client):
    from mnemotrace.server import app as appmod
    user = "synthetic-pagination"
    svc = appmod.svc()
    with svc.write_lock(user):
        mem = svc.get(user)
        for i in range(61):
            mem.add_fact("synthetic-session", "decision", f"Synthetic decision {i}", user_id=user, valid_at=100+i)
        mem.add_fact("synthetic-session", "lesson", "Synthetic older lesson", user_id=user, valid_at=1)
        svc._save(user, mem)
    params = {"kind": "outcomes", "outcome_kind": "lesson", "facts_limit": 1}
    first = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert first["facts_page"]["total"] == 1
    assert first["facts"][0]["predicate"] == "lesson"
    assert client.get("/v1/memories", params={"outcome_kind": "invalid"}, headers=hdr(user)).status_code == 422
    svc.add_fact(user, "synthetic-session", "finding", "Synthetic new finding")
    conflict = client.get("/v1/memories", params={**params, "expected_revision": first["data_revision"], "evaluated_at": first["evaluated_at"]}, headers=hdr(user))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "memory_revision_changed"
    assert "facts" not in conflict.json()


@pytest.mark.parametrize("params", [
    {"expected_revision": "empty"}, {"evaluated_at": "1000"},
    *[{"expected_revision": "empty", "evaluated_at": value} for value in ["-1", "nan", "inf", "-inf"]],
])
def test_memories_invalid_browse_context_returns_parameter_error(client, params):
    response = client.get("/v1/memories", params=params, headers=hdr("synthetic-context"))
    assert response.status_code == 422
    assert "evaluated_at" in str(response.json())


def test_memories_http_echoes_context_and_freezes_time(client, monkeypatch):
    from mnemotrace.server import app as appmod
    svc, user = appmod.svc(), "synthetic-clock"
    with svc.write_lock(user):
        mem = svc.get(user)
        for name, valid in [("future", 1100), ("current", 900), ("older", 800)]:
            mem.add_fact(name, "decision", name, user_id=user, valid_at=valid)
        svc._save(user, mem)
    clock = [1000]
    monkeypatch.setattr("mnemotrace.util.time.time", lambda: clock[0])
    params = {"facts_limit": 1, "status": "live"}
    first = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert first["evaluated_at"] == 1000
    clock[0] = 1200
    second = client.get("/v1/memories", params={**params, "facts_offset": first["facts_page"]["next_offset"],
                        "expected_revision": first["data_revision"], "evaluated_at": first["evaluated_at"]}, headers=hdr(user)).json()
    assert second["evaluated_at"] == first["evaluated_at"]
    assert second["facts_page"]["total"] == 2
    assert [first["facts"][0]["object"], second["facts"][0]["object"]] == ["current", "older"]
