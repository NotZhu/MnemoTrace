"""分类和对象类型跨 HTTP 接口及隐私边界保持一致。"""
from __future__ import annotations

import pytest

from tests.postgres.test_http_product import hdr, seed_facts


@pytest.fixture
def metadata_client(product_client, product_service):
    return product_client, product_service


def test_http_categories_write_read_reset_export_and_namespace_isolation(metadata_client):
    client, _ = metadata_client
    headers = hdr("synthetic-alpha")
    added = client.post("/v1/facts", headers=headers, json={
        "predicate": "prefers", "object": "茶", "categories": ["food"], "object_kind": "entity", "sensitive": False,
    })
    assert added.status_code == 200
    data = added.json()
    assert (data["categories"], data["categories_source"], data["object_kind"]) == (["food"], "user", "entity")
    endpoint = f"/v1/facts/{data['id']}"
    dump = client.get("/v1/memories", headers=headers).json()
    assert dump["facts"][0]["categories"] == ["food"]
    assert dump["facts_page"]["items"][0]["categories_source"] == "user"
    misc = client.patch(endpoint, headers=headers, json={"categories": ["misc"]}).json()
    assert misc["categories_source"] == "user"
    reset = client.patch(endpoint, headers=headers, json={"categories": []}).json()
    assert (reset["categories"], reset["categories_source"]) == ([], "unset")
    snapshot = client.get("/v1/export", headers=headers).json()
    assert snapshot["format_version"] == 2
    assert snapshot["facts"][0]["categories_source"] == "unset"
    denied = client.patch(endpoint, headers=hdr("synthetic-beta"),
                          json={"categories": ["technology"]})
    assert denied.status_code == 404
    assert client.get("/v1/memories", headers=headers).json()["facts"][0]["categories"] == []


def test_http_categories_filters_before_paging_and_rejects_invalid_values(metadata_client):
    client, _ = metadata_client
    headers = hdr("synthetic-topic")
    for obj, labels in [("合成茶", ["food"]), ("合成作品", ["music"]), ("合成咖啡", ["food", "music"]), ("未分类", [])]:
        assert client.post("/v1/facts", headers=headers, json={
            "predicate": "prefers", "object": obj, "categories": labels, "sensitive": False,
        }).status_code == 200
    page = client.get("/v1/memories", headers=headers,
                      params={"categories": ["food", "music"], "facts_limit": 1}).json()
    assert page["facts_page"]["total"] == 3
    assert set(page["facts"][0]["categories"]) & {"food", "music"}
    for invalid in ["", "Music", "音乐"]:
        assert client.get("/v1/memories", headers=headers, params={"categories": invalid}).status_code == 422


def test_http_object_kind_edit_reprojects_without_changing_fact(metadata_client):
    client, _ = metadata_client
    headers = hdr("synthetic-alpha")
    fact = client.post("/v1/facts", headers=headers, json={
        "predicate": "works_at", "object": "M. & Co. (Lab)", "categories": ["professional_details"],
        "sensitive": False,
    }).json()
    assert fact["object_kind"] == "unknown"
    assert client.get("/v1/graph", headers=headers).json()["edges"] == []
    endpoint = f"/v1/facts/{fact['id']}"
    assert client.patch(endpoint, headers=headers, json={"object_kind": "entity"}).status_code == 200
    graph = client.get("/v1/graph", headers=headers).json()
    assert {node["name"] for node in graph["nodes"]} == {"user", "M. & Co. (Lab)"}
    assert [edge["fact_id"] for edge in graph["edges"]] == [fact["id"]]
    assert client.patch(endpoint, headers=headers, json={"object_kind": "literal"}).status_code == 200
    graph = client.get("/v1/graph", headers=headers).json()
    assert graph["nodes"] == graph["edges"] == []
    stored, = client.get("/v1/memories", headers=headers).json()["facts"]
    assert (stored["id"], stored["object"], stored["categories"]) == (
        fact["id"], "M. & Co. (Lab)", ["professional_details"],
    )


@pytest.mark.parametrize("patch", [
    {"categories": ["音乐"]}, {"categories": "food"}, {"categories": [1]},
    {"categories_source": "llm"}, {"object_kind": "text"},
])
def test_http_invalid_metadata_does_not_mutate_fact(metadata_client, patch):
    client, _ = metadata_client
    headers = hdr("synthetic-alpha")
    fact = client.post("/v1/facts", headers=headers,
                       json={"predicate": "prefers", "object": "茶", "categories": ["food"], "object_kind": "entity", "sensitive": False}).json()
    before = client.get("/v1/memories", headers=headers).json()["facts"]
    result = client.patch(f"/v1/facts/{fact['id']}", headers=headers, json={"object": "咖啡", **patch})
    assert result.status_code == 422
    assert client.get("/v1/memories", headers=headers).json()["facts"] == before


def test_redacted_session_report_omits_fact_metadata(metadata_client, product_scope):
    client, service = metadata_client
    user = "synthetic-alpha"
    from mnemotrace.types import Fact

    context, _ = product_scope(user)
    source_id, = service.remember(context, "Synthetic session statement", "synthetic-session", scope="raw")["source_ids"]
    seed_facts(service, context, [Fact("user", "note", "Synthetic private value", user_id=context.user_id,
                                       provenance=[source_id], categories=["health"], categories_source="user", object_kind="literal", sensitive=True)])
    headers = hdr(user)
    endpoint = "/v1/sessions/report?session_id=synthetic-session"
    hidden, = client.get(endpoint, headers=headers).json()["facts"]
    assert hidden["redacted"] and not {"categories", "categories_source", "object_kind"} & hidden.keys()
    visible, = client.get(endpoint + "&include_sensitive=true", headers=headers).json()["facts"]
    assert (visible["categories"], visible["categories_source"], visible["object_kind"]) == (["health"], "user", "literal")
    assert client.get("/v1/export", headers=headers).json()["facts"] == []
