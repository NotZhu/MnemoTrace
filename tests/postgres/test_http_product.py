"""HTTP contracts backed by disposable PostgreSQL spaces."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


@pytest.fixture
def client(product_client):
    return product_client


def hdr(label):
    return {'X-Synthetic-Scope': label}


def test_health(client):
    response = client.get("/health")
    data = response.json()
    assert response.status_code == 200 and data["ok"] and data["ready"]
    assert data["auth_mode"] == "api_keys"
    assert data["anonymous_allowed"] is False
    assert data["embedder"] == "lexical"
    assert data["llm_configured"] and data["storage"] == "postgres"
    assert "data_dir" not in data and "api_keys" not in data
    assert client.get("/ready").status_code == 200


@pytest.mark.parametrize('method', ['get', 'post', 'delete'])
def test_working_route_has_been_removed(client, method):
    assert getattr(client, method)('/v1/working', headers=hdr('retired-route')).status_code == 404


@pytest.mark.parametrize('path,payload', [
    ('/v1/remember', dict(content='Synthetic task', scope='raw')),
    ('/v1/remember', dict(content='Synthetic task', extract='false')),
    ('/v1/remember', dict(content='Synthetic task', extract=0)),
    ('/v1/recall', dict(query='Synthetic query', session_id='s')),
    ('/v1/processing/flush', dict(include_summaries=True)),
])
def test_retired_parameters_are_rejected_before_writing(client, path, payload):
    h = hdr('retired-parameters')
    assert client.post(path, json=payload, headers=h).status_code == 422
    counts = client.get('/v1/memories', headers=h).json()['counts']
    assert counts['episodes'] == counts['facts'] == 0
    assert 'working_live' not in counts


def test_digest_policy_uses_only_current_field_names(client):
    h = hdr('digest-policy')
    response = client.put('/v1/policy', json=dict(digest_system='Synthetic source digest policy'), headers=h)
    assert response.is_success
    policy = client.get('/v1/policy', headers=h).json()
    assert policy['policy']['digest_system'] == 'Synthetic source digest policy'
    assert 'summary_system' not in policy['defaults']
    assert client.put('/v1/policy', json=dict(summary_system='Retired policy'), headers=h).status_code == 422


@pytest.mark.history_import
@pytest.mark.parametrize("payload", [{"facts": []}, {"episodes": []}, {"facts": "bad", "episodes": []}])
def test_incomplete_snapshot_is_rejected_instead_of_message_ingestion(client, payload):
    response = client.post("/v1/import", json={"data": payload}, headers=hdr("synthetic-invalid-import"))
    assert response.status_code == 400
    assert client.get("/v1/memories", headers=hdr("synthetic-invalid-import")).json()["facts"] == []


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


def test_recall_answers_with_extraction_model_unless_answerer_overrides(client, runtime, monkeypatch):
    from mnemotrace.llm.fake import FakeLLM
    from mnemotrace.service import MemoryService
    from mnemotrace.server import app

    h = hdr("synthetic-answer-fallback")
    assert client.post("/v1/facts", json={"text": 'user lives in Shenzhen', "predicate": "lives_in", "object": "Shenzhen"}, headers=h).is_success
    assert client.post("/v1/remember", json={"content": "I live in Shenzhen.", "extract": False}, headers=h).is_success
    llm = FakeLLM(responses=["Shenzhen"])

    for answerer, expected in ((None, "Shenzhen"), (FakeLLM(responses=["Explicit answerer"]), "Explicit answerer")):
        monkeypatch.setattr(app, "_svc", MemoryService(runtime, llm=llm, answerer=answerer))
        response = client.post("/v1/recall", json={"query": "Where do I live?", "answer": True}, headers=h)
        assert response.is_success
        assert response.json()["answer"] == expected
        assert response.json()["full_tokens"] == 5


def test_tenant_data_rights_operations_remain_isolated(client):
    tenant_a = hdr("isolation-tenant-a")
    tenant_b = hdr("isolation-tenant-b")
    fact_a = client.post(
        "/v1/facts",
        json={"text": 'user release marker alpha-private-marker', "predicate": "release_marker", "object": "alpha-private-marker", "sensitive": False},
        headers=tenant_a,
    ).json()
    fact_b = client.post(
        "/v1/facts",
        json={"text": 'user release marker beta-private-marker', "predicate": "release_marker", "object": "beta-private-marker", "sensitive": False},
        headers=tenant_b,
    ).json()

    export_a = client.get("/v1/export", headers=tenant_a).json()
    export_b = client.get("/v1/export", headers=tenant_b).json()
    assert "alpha-private-marker" in str(export_a)
    assert "beta-private-marker" not in str(export_a)
    assert "beta-private-marker" in str(export_b)
    assert "alpha-private-marker" not in str(export_b)

    assert client.patch(
        f"/v1/facts/{fact_b['id']}", json={"subject": "user", "predicate": "release_marker", "object": "cross-tenant-edit", "text": "Cross-tenant edit."}, headers=tenant_a
    ).status_code == 404
    assert client.delete(f"/v1/facts/{fact_b['id']}", headers=tenant_a).json()["ok"] is False
    assert client.get("/v1/export", headers=tenant_b).json() == export_b

    erased = client.post("/v1/forget", json={"confirm": True}, headers=tenant_a)
    assert erased.status_code == 200 and erased.json()["ok"] is True
    assert "alpha-private-marker" not in str(client.get("/v1/export", headers=tenant_a).json())
    assert "beta-private-marker" in str(client.get("/v1/export", headers=tenant_b).json())
    assert fact_a["id"] != fact_b["id"]


def test_http_handoff_across_agent_sessions_in_same_namespace(client):
    h = hdr("agent_handoff")
    codex_session = "codex:super-memory:handoff-source"

    written = client.post("/v1/remember", json={
        "content": "Project decision: the launch checklist must include committed eval logs.",
        "session_id": codex_session,
        "extract": True,
    }, headers=h).json()
    assert written["ok"] is True
    assert written["operations"][0]["core_committed"]
    assert client.post("/v1/processing/flush", json={"session_id": codex_session}, headers=h).json()["ok"] is True

    recalled = client.post("/v1/recall", json={
        "query": "What launch checklist decision did Codex record?",

        "n_chunks": 3,
    }, headers=h).json()

    assert "committed eval logs" in recalled["context"]
    assert codex_session in recalled["context"]


def test_session_report_endpoint_audits_saved_facts(client):
    h = hdr("session_report")
    session = "codex:super-memory:thread"
    client.post("/v1/remember", json={
        "content": "Project decision: the launch checklist must include committed eval logs.",
        "session_id": session,
        "extract": True,
    }, headers=h)
    client.post("/v1/processing/flush", json={"session_id": session}, headers=h)

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
        "extract": True,
    }, headers=h)
    client.post("/v1/remember", json={
        "content": "My private diagnosis is diabetes.",
        "session_id": claude_session,
        "extract": True,
    }, headers=h)

    data = client.get("/v1/sessions?limit=10", headers=h).json()

    assert data["ok"] is True
    rows = {row["id"]: row for row in data["sessions"]}
    assert {codex_session, claude_session} <= set(rows)
    assert rows[codex_session]["episodes"] == 1
    assert rows[codex_session]["facts_added"] >= 1
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
        "extract": True,
    }, headers=h)
    client.post("/v1/facts", json={"text": 'user has disease diabetes', "predicate": "has_disease", "object": "diabetes", "object_kind": "literal"}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False, "object_kind": "entity"}, headers=h)

    stats = client.get("/v1/stats", headers=h).json()
    assert stats["space_id"]
    assert stats["counts"]["episodes"] >= 1
    assert stats["counts"]["episodes_consolidated"] >= 1
    assert stats["counts"]["episodes_pending"] == 0
    assert stats["counts"]["facts"] >= 2
    assert stats["counts"]["facts_live"] >= 2
    assert stats["counts"]["facts_sensitive"] >= 1
    assert stats["counts"]["entities"] >= 1
    assert stats["storage"] == "postgres"
    assert stats["embedder"] == "lexical"
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
        "extract": True,
    }, headers=h)
    client.put("/v1/focus", json={
        "track": ["project decisions"],
        "mute": ["health details"],
    }, headers=h)

    status = client.get(f"/v1/agent/status?session_id={session}", headers=h).json()

    assert status["ok"] is True
    assert status["space_id"]
    assert status["session_id"] == session
    assert status["session"]["episodes"] == 1
    assert status["focus"] == {"track": ["project decisions"], "mute": ["health details"]}
    assert status["counts"]["facts_live"] >= 1
    assert "mnemotrace_recall" in status["tools"]["read_context"]
    rendered = str(status).lower()
    assert "diabetes" not in rendered
    assert "acme" not in rendered
    assert "private launch note" not in rendered


@pytest.mark.history_import
@pytest.mark.parametrize("malformed", [None, {"role": "user", "content": "invalid\x00text"}])
def test_import_reports_invalid_sessions_without_losing_durable_receipts(client, malformed):
    headers = hdr("synthetic-partial-import")
    payload = {"consolidate": False, "sessions": [
        {"session_id": "valid-first", "messages": [{"role": "user", "content": "First synthetic source"}]},
        {"session_id": "invalid-middle", "messages": [malformed]},
        {"session_id": "valid-last", "messages": [{"role": "user", "content": "Last synthetic source"}]},
    ]}
    response = client.post("/v1/import", json=payload, headers=headers)
    assert response.status_code == 200
    report = response.json()
    assert not report["ok"] and report["failed"] == 1 and report["episodes"] == 2
    failure, = report["failures"]
    assert failure["session_id"] == "invalid-middle" and failure["code"] == "invalid_parameter"
    assert not failure["stored_raw"]
    assert [item["session_id"] for item in report["results"]] == ["valid-first", "valid-last"]
    for result in report["results"]:
        source_id, = result["source_ids"]
        assert client.get(f"/v1/episodes/{source_id}", headers=headers).status_code == 200
    replay = client.post("/v1/import", json=payload, headers=headers).json()
    assert replay["skipped"] == 2 and replay["episodes"] == 0


@pytest.mark.history_import
def test_import_with_null_session_and_no_other_input_returns_validation_result(client):
    response = client.post("/v1/import", json={"sessions": [None]}, headers=hdr("invalid-only"))
    assert response.status_code == 200
    assert response.json()["failures"] == [
        {"session_id": None, "session_index": 0, "code": "invalid_parameter",
         "stored_raw": False, "operation_ids": []},
    ]


@pytest.mark.history_import
def test_import_sessions_and_raw_data(client):
    h = hdr("importer")
    sessions = [{"session_id": "s1", "title": "Pets",
                 "messages": [{"role": "user", "content": "I have a dog named Mochi."},
                              {"role": "assistant", "content": "Mochi is a cute name!"}]}]
    out = client.post("/v1/import", json={"consolidate": True, "sessions": sessions}, headers=h).json()
    assert out["ok"] and out["episodes"] == 2
    # 原始数据 + format 路径
    out2 = client.post("/v1/import", json={"consolidate": True,
        "data": [{"role": "user", "content": "I drive a Tesla Model 3."}], "format": "messages"}, headers=h).json()
    assert out2["episodes"] == 1
    ctx = client.post("/v1/recall", json={"query": "What pet do I have?"}, headers=h).json()["context"]
    assert "Mochi" in ctx
    # 空的 import 请求应返回清晰的 400
    assert client.post("/v1/import", json={}, headers=h).status_code == 400


@pytest.mark.history_import
def test_import_sessions_accept_iso_timestamps(client):
    h = hdr("import_iso")
    out = client.post("/v1/import", json={"consolidate": True,
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
        "generate_digests": False,
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
    assert past["status"] == "evidence_only" and any("Tencent" in fact for fact in past["facts"])


def test_fact_crud_and_focus_policy_graph_export(client):
    h = hdr("editor")
    fid = client.post("/v1/facts", json={"text": 'user works at Tencent', "predicate": "works_at", "object": "Tencent"}, headers=h).json()["id"]
    assert client.patch(f"/v1/facts/{fid}", json={"subject": "user", "predicate": "works_at", "object": "Moonshot AI", "text": "用户在 Moonshot AI 工作。", "sensitive": False}, headers=h).json()["ok"]
    assert client.patch("/v1/facts/nope", json={"subject": "user", "predicate": "likes", "object": "x", "text": "用户喜欢 x。"}, headers=h).status_code == 404
    # focus + policy 经 service 层的往返测试
    assert client.put("/v1/focus", json={"track": ["career"], "mute": []}, headers=h).json()["focus"]["track"] == ["career"]
    assert client.get("/v1/focus", headers=h).json()["track"] == ["career"]
    pol = client.put("/v1/policy", json={"extract_instruction": "Record job changes."}, headers=h).json()
    assert pol["ok"] and pol["policy"]["extract_instruction"] == "Record job changes."
    assert "nodes" in client.get("/v1/graph", headers=h).json()
    exp = client.get("/v1/export", headers=h).json()
    assert exp["format_version"] == 3 and any(f["object"] == "Moonshot AI" for f in exp["facts"])
    assert client.delete(f"/v1/facts/{fid}", headers=h).json()["ok"] is True


def test_complete_fact_text_edit_is_persisted_and_statement_is_rejected(client):
    h = hdr("synthetic_statement_editor")
    fid = client.post("/v1/facts", json={"text": 'user likes USER_PREFERENCE_TOKEN', "predicate": "likes", "object": "USER_PREFERENCE_TOKEN", "sensitive": False}, headers=h).json()["id"]
    statement = "演示用户喜欢简洁的回答。"
    for patch in ({"statement": " "}, {"statement": ""}, {"statement": statement, "object": "回答"}):
        assert client.patch(f"/v1/facts/{fid}", json=patch, headers=h).status_code == 422
    assert client.patch(f"/v1/facts/{fid}", json={"statement": statement}, headers=h).status_code == 422
    dump = client.get("/v1/memories", headers=h).json()
    unchanged = next(row for row in dump["facts"] if row["id"] == fid)
    assert unchanged["object"] == "USER_PREFERENCE_TOKEN"
    assert client.patch(f"/v1/facts/{fid}", json={
        "subject": "user", "predicate": "likes", "object": "简洁的回答", "text": statement, "sensitive": False,
    }, headers=h).status_code == 200
    outcome_id = client.post("/v1/facts", json={"text": 'synthetic-session decision 原来的结论',
        "subject": "synthetic-session", "predicate": "decision", "object": "原来的结论",
    }, headers=h).json()["id"]
    result = client.patch(f"/v1/facts/{outcome_id}", json={"subject": "synthetic-session", "predicate": "decision", "object": statement, "text": statement, "sensitive": False}, headers=h)
    assert result.status_code == 200
    assert result.json()["text"] == statement and 'display' not in result.json()
    dump = client.get("/v1/memories", headers=h).json()
    edited = next(row for row in dump["facts"] if row["id"] == outcome_id)
    assert edited["object"] == edited["text"] == statement and 'display' not in edited
    assert edited["source"] == "user"
    reloaded = client.get("/v1/memories", headers=h).json()["facts"]
    assert next(row for row in reloaded if row["id"] == fid)["object"] == "简洁的回答"
    assert next(row for row in reloaded if row["id"] == outcome_id)["object"] == statement


def test_graph_endpoint_supports_as_of_and_edge_audit_fields(client):
    h = hdr("graph_asof")
    for session, text, stamp in (
        ("old", "Wei works at Tencent.", 1_700_000_000.0),
        ("new", "Wei works at Moonshot AI.", 1_702_592_000.0),
    ):
        response = client.post("/v1/messages", json={
            "session_id": session, "processing": "immediate",
            "messages": [{"message_id": "m1", "role": "user", "content": text, "event_time": stamp}],
        }, headers=h)
        assert response.is_success


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
    client.post("/v1/facts", json={"text": 'user visited Synthetic Clinic', "predicate": "visited", "object": "Synthetic Clinic",
                                  "object_kind": "entity", "sensitive": True}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False, "object_kind": "entity"}, headers=h)

    default_safe = client.get("/v1/graph", headers=h).json()
    assert "Synthetic Clinic" not in str(default_safe)
    assert "Acme" in str(default_safe)

    full = client.get("/v1/graph?include_sensitive=true", headers=h).json()
    assert "Synthetic Clinic" in str(full)
    assert "Acme" in str(full)

    safe = client.get("/v1/graph?include_sensitive=false", headers=h).json()
    assert "Synthetic Clinic" not in str(safe)
    assert "Acme" in str(safe)
    assert all("Synthetic Clinic" not in e.get("fact_text", "") for e in safe["edges"])


def test_memories_endpoint_supports_pagination_filtering_and_sensitive_create(client):
    h = hdr("memories_page")
    client.post("/v1/remember", json={"content": "First durable episode."}, headers=h)
    client.post("/v1/remember", json={"content": "Second durable episode."}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Acme',
        "predicate": "works_at",
        "object": "Acme",
        "categories": ["professional_details"], "sensitive": False,
    }, headers=h)
    client.post("/v1/facts", json={"text": 'user favorite city Hangzhou',
        "predicate": "favorite_city",
        "object": "Hangzhou",
        "categories": ["user_preferences"], "sensitive": False,
    }, headers=h)
    client.post("/v1/facts", json={"text": 'user project note memory console',
        "predicate": "project_note",
        "object": "memory console",
        "categories": ["technology"], "sensitive": False,
    }, headers=h)
    client.post("/v1/facts", json={"text": 'user security phrase blue-lotus',
        "predicate": "security_phrase",
        "object": "blue-lotus",
        "categories": ["health"],
        "sensitive": True,
    }, headers=h)

    safe_default = client.get("/v1/memories?facts_limit=10&episodes_limit=1", headers=h).json()
    assert "profile" not in safe_default
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


def seed_facts(service, context, facts):
    from mnemotrace.store.contracts import (
        ChangeSet, CommitConditions, RecordKind, RecordRef, RecordWrite, SourceLink, StoredRecord,
    )
    from mnemotrace.store.postgres.dao import new_operation_id

    dao = service.memory(context).dao
    with dao.read_snapshot() as view:
        snapshot = view.snapshot
    writes = tuple(RecordWrite(StoredRecord.from_record(context, fact)) for fact in facts)
    links = tuple(SourceLink(write.ref, RecordRef(context, RecordKind.SOURCE, source_id))
                  for write, fact in zip(writes, facts) for source_id in fact.provenance)
    dao.commit(ChangeSet(CommitConditions(snapshot), new_operation_id(), writes=writes, source_links=links))


def test_focus_filters_retrieval_and_tracks_without_rewriting_facts(client, product_service, product_scope):
    label = "synthetic-retrieval-focus"
    h = hdr(label)
    context, _ = product_scope(label)
    response = client.post("/v1/facts", json={"text": 'user likes violet cactus', "predicate": "likes", "object": "violet cactus", "sensitive": False}, headers=h)
    fact_id = response.json()["id"]
    assert client.post("/v1/remember", json={"content": "violet cactus raw evidence", "extract": False}, headers=h).is_success
    memory = product_service.memory(context)
    query = "violet cactus"
    request = memory._query(query, top_k=5)

    def salience(view):
        return next(c.salience for c in view.candidates(request).candidates if c.record.ref.record_id == fact_id)

    assert client.put("/v1/focus", json={"track": ["VIOLET"]}, headers=h).is_success
    with memory.dao.read_snapshot() as view:
        assert salience(view.focused()) == pytest.approx(salience(view) + 0.5)
    assert "violet cactus" in client.post("/v1/recall", json={"query": query}, headers=h).json()["context"]
    assert client.put("/v1/focus", json={"track": [], "mute": ["VIOLET"]}, headers=h).is_success
    lean = client.post("/v1/recall", json={"query": query}, headers=h)
    assert lean.is_success and "violet cactus" not in lean.json()["context"]
    direct = client.post("/v1/recall", json={"query": query, "lean": False}, headers=h)
    assert direct.is_success and direct.json()["facts"] == []
    assert not memory.retrieve_episodes(query).episodes
    assert fact_id in {row["id"] for row in client.get("/v1/memories", headers=h).json()["facts"]}


def test_session_report_pages_all_facts_and_keeps_redaction_total(client, product_service, product_scope):
    from mnemotrace.types import Fact

    label = "synthetic-report-pages"
    session_id = "paged-report"
    context, _ = product_scope(label)
    source, = product_service.remember(context, "Synthetic report source", session_id, extract=False)["source_ids"]
    rows = [Fact("synthetic", "note", f"report value {i}", user_id=context.user_id,
                 valid_at=100 + i, sensitive=i == 0, provenance=[source], text=' '.join(('synthetic', 'note', f'report value {i}')))
            for i in range(102)]
    seed_facts(product_service, context, rows)
    headers = hdr(label)
    url = "/v1/sessions/report"
    params = {"session_id": session_id}
    first = client.get(url, headers=headers, params=params).json()
    assert len(first["facts"]) == 100 and first["facts_page"]["next_offset"] == 100
    assert first["facts_added"] == 102 and first["facts_redacted"] == 1
    assert not any(row["redacted"] for row in first["facts"])

    last = client.get(url, headers=headers, params={**params, "offset": 100}).json()
    assert last["facts_redacted"] == 1 and last["facts_page"]["next_offset"] is None
    assert {row["id"] for row in first["facts"] + last["facts"]} == {row.id for row in rows}
    hidden = last["facts"][-1]
    assert hidden["id"] == rows[0].id and hidden["redacted"] and "object" not in hidden
    private = client.get(url, headers=headers, params={**params, "offset": 100, "include_sensitive": True}).json()
    assert private["facts_redacted"] == 0 and private["facts"][-1]["object"] == rows[0].object


def _seed_category_filter_facts(service, context):
    from mnemotrace.types import Fact

    user = context.user_id
    rows = [
        Fact(subject="synthetic", predicate="note", object="alpha first", categories=["professional_details"], categories_source="user", valid_at=10, user_id=user, text='synthetic note alpha first'),
        Fact(subject="synthetic", predicate="note", object="alpha second", categories=["professional_details"], categories_source="user", valid_at=20, user_id=user, text='synthetic note alpha second'),
        Fact(subject="synthetic", predicate="note", object="beta latest", categories=["personal_details"], categories_source="user", valid_at=90, user_id=user, text='synthetic note beta latest'),
        Fact(subject="synthetic", predicate="note", object="uncategorized", valid_at=80, user_id=user, text='synthetic note uncategorized'),
        Fact(subject="synthetic", predicate="note", object="past alpha", categories=["milestones"], categories_source="user", valid_at=5, invalid_at=6, user_id=user, text='synthetic note past alpha'),
        Fact(subject="synthetic-session", predicate="decision", object="synthetic decision", categories=["technology"], categories_source="user", valid_at=95, user_id=user, text='synthetic-session decision synthetic decision'),
        Fact(subject="synthetic", predicate="note", object="private alpha", categories=["health"], categories_source="user", sensitive=True, valid_at=100, user_id=user, text='synthetic note private alpha'),
    ]
    seed_facts(service, context, rows)
    service.remember(context, "synthetic source record", "synthetic-session", extract=False)
    return rows


def test_memories_category_filters_before_pagination_with_status_and_search(client, product_service, product_scope):
    user = "synthetic-category-filter"
    rows = _seed_category_filter_facts(product_service, product_scope(user)[0])
    params = {"kind": "attributes", "status": "live", "facts_limit": 1, "episodes_limit": 0}
    unfiltered = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert unfiltered["facts"][0]["id"] == rows[2].id
    filtered = {**params, "categories": ["professional_details"], "q": "alpha"}
    first = client.get("/v1/memories", params=filtered, headers=hdr(user)).json()
    second = client.get("/v1/memories", params={**filtered, "facts_offset": 1}, headers=hdr(user)).json()
    assert first["facts_page"]["total"] == 2
    assert first["facts_page"]["has_more"] is True
    assert {first["facts"][0]["id"], second["facts"][0]["id"]} == {rows[0].id, rows[1].id}
    assert first["counts"] == unfiltered["counts"]
    assert set(first["fact_categories"]) == {"professional_details", "personal_details", "milestones"}
    old = client.get("/v1/memories", params={**params, "status": "superseded", "categories": ["milestones"]}, headers=hdr(user)).json()
    assert [fact["id"] for fact in old["facts"]] == [rows[4].id]
    empty = client.get("/v1/memories", params={**params, "categories": ["misc"]}, headers=hdr(user)).json()
    assert empty["facts"] == [] and empty["facts_page"]["total"] == 0
    assert empty["fact_categories"] == first["fact_categories"]


def test_memories_category_facets_respect_visibility_kind_and_space(client, product_service, product_scope):
    user = "synthetic-category-facets"
    rows = _seed_category_filter_facts(product_service, product_scope(user)[0])
    params = {"kind": "attributes", "facts_limit": 0, "episodes_limit": 0, "q": "no match"}
    safe = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert set(safe["fact_categories"]) == {"professional_details", "personal_details", "milestones"}
    private = client.get("/v1/memories", params={**params, "include_sensitive": True}, headers=hdr(user)).json()
    assert "health" in private["fact_categories"]
    outcomes = client.get("/v1/memories", params={**params, "kind": "outcomes"}, headers=hdr(user)).json()
    assert outcomes["fact_categories"] == ["technology"]
    other_space = client.get("/v1/memories", params=params, headers=hdr("synthetic-empty-space")).json()
    assert other_space["fact_categories"] == []
    with_episodes = client.get("/v1/memories", params={"kind": "attributes", "categories": ["professional_details"], "include_sensitive": True}, headers=hdr(user)).json()
    assert len(with_episodes["episodes"]) == 1
    assert {fact["id"] for fact in with_episodes["facts"]} == {rows[0].id, rows[1].id}


def test_sensitive_edit_preserves_history_across_reload(client):
    h = hdr("metadata_history")
    claim = {"subject": "demo_user", "predicate": "lives_in", "object": "北京", "text": "演示用户住在北京。"}
    old_response = client.post("/v1/facts", json=claim, headers=h)
    assert old_response.status_code == 200
    old_id = old_response.json()["id"]
    current_response = client.post("/v1/facts", json={**claim, "object": "上海", "text": "演示用户住在上海。", "cardinality": "single"}, headers=h)
    assert current_response.status_code == 200
    current_id = current_response.json()["id"]
    url = "/v1/memories?include_sensitive=true&episodes_limit=0"
    original = {f["id"]: f for f in client.get(url, headers=h).json()["facts"]}
    assert original[old_id]["status"] == "superseded"

    for sensitive in (True, False):
        response = client.patch(f"/v1/facts/{old_id}", json={**claim, "sensitive": sensitive}, headers=h)
        assert response.status_code == 200
        # 重新加载持久化状态，不能只在热缓存中保留历史。
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
    claim = {field: original[field] for field in ("subject", "predicate", "object", "text")}
    fact_id = original["id"]

    for patch in ({"sensitive": True}, {"sensitive": False}, {"categories": ["hobbies"]}, {}):
        response = client.patch(f"/v1/facts/{fact_id}", json={**claim, **patch}, headers=h)
        assert response.status_code == 200
        assert response.json()["source"] == "extracted"
        restored = next(f for f in client.get(url, headers=h).json()["facts"] if f["id"] == fact_id)
        assert restored["source"] == "extracted"
        assert restored["provenance"] == original["provenance"]
        assert restored["status"] == original["status"]
        for field, value in patch.items():
            assert restored[field] == value

    response = client.patch(f"/v1/facts/{fact_id}", json={**claim, "object": "Shanghai", "text": "用户住在上海。"}, headers=h)
    assert response.status_code == 200 and response.json()["source"] == "user"
    restored = next(f for f in client.get(url, headers=h).json()["facts"] if f["id"] == fact_id)
    assert restored["source"] == "user" and restored["object"] == "Shanghai"
    assert restored["provenance"] == original["provenance"]


def test_graph_endpoint_supports_query_live_only_and_limit(client):
    h = hdr("graph_query")
    client.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False, "object_kind": "entity"}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Moonshot AI', "predicate": "works_at", "object": "Moonshot AI", "sensitive": False, "cardinality": "single", "object_kind": "entity"}, headers=h)
    client.post("/v1/facts", json={"text": 'user likes food ramen', "predicate": "likes_food", "object": "ramen"}, headers=h)

    old_company = client.get("/v1/graph?q=Acme&live_only=true", headers=h).json()
    assert "Acme" not in str(old_company)

    current_company = client.get("/v1/graph?q=Moonshot&live_only=true&limit=1", headers=h).json()
    assert len(current_company["edges"]) <= 1
    assert "Moonshot AI" in str(current_company)
    assert all(edge["live"] for edge in current_company["edges"])


def test_graph_limit_and_completeness_follow_space_filters(client, product_service, product_scope):
    from mnemotrace.types import Fact

    user = "synthetic_graph_limit"
    context, _ = product_scope(user)
    older = [Fact("synthetic_user", f"link_{i}", f"old_{i}", user_id=context.user_id,
                  valid_at=100, invalid_at=200, object_kind="entity", text=' '.join(('synthetic_user', f'link_{i}'.replace('_', ' '), f'old_{i}'))) for i in range(121)]
    current = Fact("synthetic_user", "uses", "current_target", user_id=context.user_id, valid_at=100, object_kind="entity", text='synthetic_user uses current_target')
    private = Fact("synthetic_user", "private_link", "private_target", user_id=context.user_id,
                   valid_at=100, sensitive=True, object_kind="entity", text='synthetic_user private link private_target')
    seed_facts(product_service, context, [*older, current, private])

    broad = client.get("/v1/graph?limit=120", headers=hdr(user)).json()
    assert len(broad["edges"]) == 120 and broad["has_more"] is True
    for query in ["live_only=true", "q=current_target", "live_only=true&limit=1"]:
        response = client.get(f"/v1/graph?{query}", headers=hdr(user))
        assert response.status_code == 200
        graph = response.json()
        assert [edge["fact_id"] for edge in graph["edges"]] == [current.id]
        assert graph["has_more"] is False
        assert {node["name"] for node in graph["nodes"]} == {"synthetic_user", "current_target"}
    private = client.get("/v1/graph?live_only=true&limit=1&include_sensitive=true", headers=hdr(user)).json()
    assert private["has_more"] is True
    assert client.get("/v1/graph?limit=1", headers=hdr("synthetic_other")).json() == {
        "nodes": [], "edges": [], "has_more": False,
    }


def test_recall_endpoint_supports_as_of_for_search_and_lean_context(client):
    h = hdr("recall_asof")
    for session, text, stamp in (
        ("old", "Wei works at Tencent.", 1_700_000_000.0),
        ("new", "Wei works at Moonshot AI.", 1_702_592_000.0),
    ):
        response = client.post("/v1/messages", json={
            "session_id": session, "processing": "immediate",
            "messages": [{"message_id": "m1", "role": "user", "content": text, "event_time": stamp}],
        }, headers=h)
        assert response.is_success


    past = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": False,
        "as_of": 1_700_864_000.0,
    }, headers=h).json()
    assert past["as_of"] == 1_700_864_000.0
    assert past["status"] == "evidence_only" and any("Tencent" in fact for fact in past["facts"])
    assert all("Moonshot" not in f for f in past["facts"])

    current = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": False,
    }, headers=h).json()
    assert current["as_of"] is None
    assert current["status"] == "evidence_only" and any("Moonshot AI" in fact for fact in current["facts"])

    ctx = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": True,
        "answer": True,
        "n_chunks": 0,
        "as_of": 1_700_864_000.0,
    }, headers=h).json()
    assert ctx["as_of"] == 1_700_864_000.0
    assert "Tencent" in ctx["context"]
    assert "Moonshot AI" not in ctx["context"]

    current_ctx = client.post("/v1/recall", json={
        "query": "Where does Wei work?",
        "lean": True,
        "answer": True,
        "n_chunks": 0,
    }, headers=h).json()
    assert current_ctx["as_of"] is None
    assert current_ctx["full_tokens"] > ctx["full_tokens"]


def test_recall_endpoint_can_redact_sensitive_facts(client):
    h = hdr("recall_redact")
    client.post("/v1/facts", json={"text": 'user has disease diabetes', "predicate": "has_disease", "object": "diabetes", "object_kind": "literal"}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False, "object_kind": "entity"}, headers=h)

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
    client.post("/v1/facts", json={"text": 'user has disease diabetes', "predicate": "has_disease", "object": "diabetes", "object_kind": "literal"}, headers=h)
    client.post("/v1/facts", json={"text": 'user works at Acme', "predicate": "works_at", "object": "Acme", "sensitive": False, "object_kind": "entity"}, headers=h)

    default_safe = client.get("/v1/export", headers=h).json()
    assert default_safe["include_sensitive"] is False
    assert default_safe["redacted_sensitive"] is True
    assert "Synthetic Clinic" not in str(default_safe)
    assert not default_safe.get("profile")
    assert default_safe["episodes"] == []

    full = client.get("/v1/export?include_sensitive=true", headers=h).json()
    assert full["include_sensitive"] is True
    assert any("diabetes" in f["text"].lower() for f in full["facts"])
    assert full["episodes"]

    safe = client.get("/v1/export?include_sensitive=false", headers=h).json()
    rendered = str(safe).lower()
    assert safe["include_sensitive"] is False
    assert safe["redacted_sensitive"] is True
    assert not safe.get("profile")
    assert safe["episodes"] == []
    assert "diabetes" not in rendered
    assert any(f["object"] == "Acme" for f in safe["facts"])
    assert all("diabetes" not in e.get("fact_text", "").lower() for e in safe["graph"]["edges"])


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


def test_static_cache_headers_and_robots(client, monkeypatch, tmp_path):
    from mnemotrace.server import app as appmod

    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>Synthetic console</title>", encoding="utf-8")
    (assets / "app.js").write_text("/* synthetic asset */", encoding="utf-8")
    monkeypatch.setattr(appmod, "_DIST", str(tmp_path))

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /ui" in robots.text
    assert "Disallow: /v1/" in robots.text

    root = client.get("/", follow_redirects=False)
    assert root.headers["Cache-Control"] == "no-cache"

    ui_head = client.head("/ui")
    assert ui_head.status_code == 200
    assert ui_head.headers["Cache-Control"] == "no-cache"
    resp = client.get("/ui/assets/app.js")
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


def test_request_limit_rejects_before_memory_write(client, product_scope, monkeypatch):
    context, key = product_scope("synthetic-size")
    monkeypatch.setenv("MNEMOTRACE_MAX_REQUEST_BYTES", "1024")
    oversized = client.post("/v1/remember", headers={"Authorization": f"Bearer {key}"},
                            json={"space_id": context.space_id, "content": "x" * 2048})
    assert oversized.status_code == 413
    assert client.get("/v1/stats", headers=hdr("synthetic-size")).json()["counts"]["episodes"] == 0


@pytest.mark.history_import
def test_import_agent_sessions_defaults_to_buffered_messages_and_feed_stats(client):
    """watcher 历史会话默认按消息 buffered 接收，并保留来源与摄入统计。"""
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
    assert data["episodes"] == 5
    assert data["queued"] and all(not op["core_committed"] for row in data["results"] for op in row["operations"])
    assert client.post("/v1/processing/flush", headers=h, json={}).json()["ok"]
    result = client.post("/v1/processing/flush", json={"session_id": "claude-code:a"}, headers=h).json()
    assert result["ok"] and all(op["core_committed"] for op in result["operations"])

    feed = client.get("/v1/stats", headers=h).json()["feed"]
    assert feed["sessions"] == 2 and feed["conclusions"] == 0
    assert feed["last_fed_at"] is not None and feed["last_fed_at_h"], feed

    status = client.get("/v1/agent/status", headers=h).json()
    assert status["feed"]["last_fed_at"] is not None and status["feed"]["sessions"] == 2

    from mnemotrace.server import app as appmod
    appmod.svc().llm = None
    # 未挂 LLM 时请求提取：在写入前拒绝。
    r = client.post("/v1/import", json={"consolidate": True, "sessions": [
        {"session_id": "codex:c", "metadata": {"source": "agent_session"}, "messages": transcript}]},
        headers=h).json()
    assert r["detail"]["code"] == "llm_not_configured", r

    # 从未被喂入的命名空间会如实上报
    empty = client.get("/v1/stats", headers=hdr("never-fed")).json()["feed"]
    assert empty == {"last_fed_at": None, "last_fed_at_h": None, "sessions": 0, "conclusions": 0}


def test_memories_outcome_kind_and_revision_contract(client, product_service, product_scope):
    from mnemotrace.types import Fact

    user = "synthetic-pagination"
    context, _ = product_scope(user)
    facts = [Fact("synthetic-session", "decision", f"Synthetic decision {i}", user_id=context.user_id,
                  valid_at=100+i, text=' '.join(('synthetic-session', 'decision', f'Synthetic decision {i}'))) for i in range(61)]
    facts.append(Fact("synthetic-session", "lesson", "Synthetic older lesson", user_id=context.user_id, valid_at=1, text='synthetic-session lesson Synthetic older lesson'))
    seed_facts(product_service, context, facts)
    params = {"kind": "outcomes", "outcome_kind": "lesson", "facts_limit": 1}
    first = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert first["facts_page"]["total"] == 1
    assert first["facts"][0]["predicate"] == "lesson"
    assert client.get("/v1/memories", params={"outcome_kind": "invalid"}, headers=hdr(user)).status_code == 422
    product_service.add_fact(context, "synthetic-session", "finding", "Synthetic new finding", text="Synthetic new finding")
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
    assert "evaluation time" in str(response.json())


def test_memories_http_echoes_context_and_freezes_time(client, product_service, product_scope, monkeypatch):
    from datetime import datetime, timezone
    from mnemotrace.store.postgres import dao
    from mnemotrace.types import Fact

    user = "synthetic-clock"
    context, _ = product_scope(user)
    seed_facts(product_service, context, [
        Fact(name, "decision", name, user_id=context.user_id, valid_at=valid, text=' '.join((name, 'decision', name)))
        for name, valid in [("future", 1100), ("current", 900), ("older", 800)]
    ])
    clock = [1000]
    original_space_row = dao._space_row
    monkeypatch.setattr(dao, "_space_row", lambda sql: (*original_space_row(sql)[:3],
                                                       datetime.fromtimestamp(clock[0], timezone.utc)))
    params = {"facts_limit": 1, "status": "live"}
    first = client.get("/v1/memories", params=params, headers=hdr(user)).json()
    assert first["evaluated_at"] == 1000
    clock[0] = 1200
    second = client.get("/v1/memories", params={**params, "facts_offset": first["facts_page"]["next_offset"],
                        "expected_revision": first["data_revision"], "evaluated_at": first["evaluated_at"]}, headers=hdr(user)).json()
    assert second["evaluated_at"] == first["evaluated_at"]
    assert second["facts_page"]["total"] == 2
    assert [first["facts"][0]["object"], second["facts"][0]["object"]] == ["current", "older"]


@pytest.mark.parametrize('clock_skew', [0, -60, 60])
@pytest.mark.parametrize('source_entry', ['raw', 'note'])
def test_recall_temporal_options_share_baseline_and_do_not_reinforce(client, product_service, product_scope,
                                                                  monkeypatch, clock_skew, source_entry):
    from mnemotrace.types import Episode

    def client_clock_source(*args, **kwargs):
        source = Episode(*args, **kwargs)
        source.ingested_at += clock_skew
        return source

    monkeypatch.setattr('mnemotrace.memory.Episode', client_clock_source)
    label = "recall_temporal_baseline"
    context, _ = product_scope(label)
    memory = product_service.memory(context)

    def remember_source(content, session_id, *, event_time):
        if source_entry == 'raw':
            return memory.remember_raw(content, session_id, event_time=event_time)
        return memory.remember(content, session_id, event_time=event_time, processing='buffered',
                               extract=True)

    fact = memory.add_fact("Ana", "works_at", "Orbit", valid_at=100, text='Ana works at Orbit')
    remember_source("Ana works at Orbit.", "earlier", event_time=100)
    with memory.dao.read_snapshot() as view:
        known_at = view.at.timestamp()
    remember_source("Ana used to work on a different synthetic project.", "late-arrival", event_time=100)
    remember_source("Ana plans a synthetic project.", "future", event_time=400)

    def recall(**options):
        response = client.post("/v1/recall", json={"query": "Where does Ana work?", **options}, headers=hdr(label))
        assert response.status_code == 200
        return response.json()

    for lean in (True, False):
        for cutoff in ({"as_of": 200}, {"valid_at": 200}, {"known_at": known_at}):
            recall(lean=lean, **cutoff)
    assert memory.get_fact(fact.id).access_count == 0
    assert recall(as_of=200, answer=True)["full_tokens"] == recall(valid_at=200, answer=True)["full_tokens"]
    assert recall(valid_at=200, known_at=known_at, answer=True)["full_tokens"] == 5
    assert recall(answer=True)["full_tokens"] > recall(valid_at=200, answer=True)["full_tokens"]
    assert memory.get_fact(fact.id).access_count > 0
