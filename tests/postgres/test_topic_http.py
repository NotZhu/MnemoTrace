"""主题元数据跨 HTTP 接口及隐私边界保持一致。"""
from __future__ import annotations

import pytest

from tests.postgres.test_http_product import hdr, seed_facts


@pytest.fixture
def topic_client(product_client, product_service):
    return product_client, product_service


def test_http_topic_write_read_reset_export_and_namespace_isolation(topic_client):
    client, _ = topic_client
    headers = hdr("synthetic-alpha")
    added = client.post("/v1/facts", headers=headers, json={
        "predicate": "prefers", "object": "茶", "topic": "food",
    })
    assert added.status_code == 200
    data = added.json()
    assert (data["topic"], data["topic_source"]) == ("food", "user")
    endpoint = f"/v1/facts/{data['id']}"
    dump = client.get("/v1/memories", headers=headers).json()
    assert dump["facts"][0]["topic"] == "food"
    assert dump["facts_page"]["items"][0]["topic_source"] == "user"
    unknown = client.patch(endpoint, headers=headers, json={"topic": "unknown"}).json()
    assert unknown["topic_source"] == "user"
    reset = client.patch(endpoint, headers=headers, json={"reset_topic": True}).json()
    assert (reset["topic"], reset["topic_source"]) == ("food", "rule")
    snapshot = client.get("/v1/export", headers=headers).json()
    assert "mnemotrace_export_version" not in snapshot
    assert snapshot["facts"][0]["topic_source"] == "rule"
    denied = client.patch(endpoint, headers=hdr("synthetic-beta"),
                          json={"topic": "finance"})
    assert denied.status_code == 404
    assert client.get("/v1/memories", headers=headers).json()["facts"][0]["topic"] == "food"


def test_http_topic_filters_before_paging_and_rejects_invalid_values(topic_client):
    client, _ = topic_client
    headers = hdr("synthetic-topic")
    for obj, topic in [("合成茶", "food"), ("合成作品", "music"), ("合成咖啡", "food"), ("合成未知", "unknown")]:
        assert client.post("/v1/facts", headers=headers, json={
            "predicate": "prefers", "object": obj, "topic": topic,
        }).status_code == 200
    page = client.get("/v1/memories", headers=headers, params={"topic": "food", "facts_limit": 1}).json()
    assert page["facts_page"]["total"] == 2 and page["facts"][0]["topic"] == "food"
    unknown = client.get("/v1/memories", headers=headers, params={"topic": "unknown"}).json()
    assert len(unknown["facts"]) == 1 and unknown["facts"][0]["topic_source"] == "user"
    for invalid in ["", "Music", "音乐"]:
        assert client.get("/v1/memories", headers=headers, params={"topic": invalid}).status_code == 422


@pytest.mark.parametrize("patch", [
    {"topic": "音乐"}, {"topic": ""}, {"topic": []}, {"topic": 1},
    {"topic": "food", "reset_topic": True},
])
def test_http_invalid_topic_does_not_mutate_fact(topic_client, patch):
    client, _ = topic_client
    headers = hdr("synthetic-alpha")
    fact = client.post("/v1/facts", headers=headers,
                       json={"predicate": "prefers", "object": "茶", "topic": "food"}).json()
    before = client.get("/v1/memories", headers=headers).json()["facts"]
    result = client.patch(f"/v1/facts/{fact['id']}", headers=headers, json={"object": "咖啡", **patch})
    assert result.status_code == 422
    assert client.get("/v1/memories", headers=headers).json()["facts"] == before


def test_redacted_session_report_omits_topic_metadata(topic_client, product_scope):
    client, service = topic_client
    user = "synthetic-alpha"
    from mnemotrace.types import Fact

    context, _ = product_scope(user)
    source_id, = service.remember(context, "Synthetic session statement", "synthetic-session", scope="raw")["source_ids"]
    seed_facts(service, context, [Fact("user", "note", "Synthetic private value", user_id=context.user_id,
                                       provenance=[source_id], topic="health", topic_source="user", sensitive=True)])
    headers = hdr(user)
    endpoint = "/v1/sessions/report?session_id=synthetic-session"
    hidden, = client.get(endpoint, headers=headers).json()["facts"]
    assert hidden["redacted"] and "topic" not in hidden and "topic_source" not in hidden
    visible, = client.get(endpoint + "&include_sensitive=true", headers=headers).json()["facts"]
    assert (visible["topic"], visible["topic_source"]) == ("health", "user")
    assert client.get("/v1/export", headers=headers).json()["facts"] == []
