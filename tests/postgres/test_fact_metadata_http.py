"""事实分类和敏感标记跨 HTTP 接口及隐私边界保持一致。"""
from __future__ import annotations

import pytest

from tests.postgres.test_http_product import hdr, seed_facts
from tests.postgres.fact_fixtures import seed_fact


@pytest.fixture
def metadata_client(product_client, product_service):
    return product_client, product_service


def test_http_fact_categories_survive_export_and_sensitivity_is_scoped(metadata_client, product_scope):
    client, service = metadata_client
    headers = hdr("synthetic-alpha")
    context, _ = product_scope("synthetic-alpha")
    fact = seed_fact(service.memory(context), "user", "prefers", "茶", text="用户偏好茶。", categories=["food"])
    dump = client.get("/v1/memories", headers=headers).json()
    assert dump["facts"][0]["categories"] == ["food"]
    assert dump["facts_page"]["items"][0]["categories_source"] == "llm"
    snapshot = client.get("/v1/export", headers=headers).json()
    assert snapshot["format_version"] == 4
    assert snapshot["facts"][0]["categories"] == ["food"]
    endpoint = f"/v1/facts/{fact.id}/sensitivity"
    denied = client.patch(endpoint, headers=hdr("synthetic-beta"), json={"sensitive": True})
    assert denied.status_code == 404
    assert client.patch(endpoint, headers=headers, json={"sensitive": True}).status_code == 200
    assert client.get("/v1/memories", headers=headers).json()["facts"] == []


def test_http_categories_filters_before_paging_and_rejects_invalid_values(metadata_client, product_scope):
    client, service = metadata_client
    context, _ = product_scope("synthetic-topic")
    headers = hdr("synthetic-topic")
    for obj, labels in [("合成茶", ["food"]), ("合成作品", ["music"]), ("合成咖啡", ["food", "music"]), ("未分类", [])]:
        seed_fact(service.memory(context), "user", "prefers", obj, text=f"用户偏好{obj}。", categories=labels)
    page = client.get("/v1/memories", headers=headers,
                      params={"categories": ["food", "music"], "facts_limit": 1}).json()
    assert page["facts_page"]["total"] == 3
    assert set(page["facts"][0]["categories"]) & {"food", "music"}
    for invalid in ["", "Music", "音乐"]:
        assert client.get("/v1/memories", headers=headers, params={"categories": invalid}).status_code == 422


def test_graph_reads_explicit_entity_links(metadata_client, product_scope):
    client, service = metadata_client
    headers = hdr("synthetic-alpha")
    context, _ = product_scope("synthetic-alpha")
    memory = service.memory(context)
    statement = ("user", "works_at", "M. & Co. (Lab)")
    unlinked = seed_fact(memory, *statement, text="user works at M. & Co. (Lab)")
    assert client.get("/v1/graph", headers=headers).json()["edges"] == []
    linked = seed_fact(memory, *statement, text=unlinked.text, linked=True)
    graph = client.get("/v1/graph", headers=headers).json()
    assert {node["name"] for node in graph["nodes"]} == {"user", "M. & Co. (Lab)"}
    assert [edge["fact_id"] for edge in graph["edges"]] == [linked.id]
    assert client.patch(f"/v1/facts/{linked.id}/sensitivity", headers=headers,
                        json={"sensitive": True, "object_id": None}).status_code == 422
    assert client.patch(f"/v1/facts/{linked.id}/sensitivity", headers=headers,
                        json={"sensitive": True}).status_code == 200
    assert client.get("/v1/graph", headers=headers).json()["edges"] == []
    assert client.get("/v1/graph?include_sensitive=true", headers=headers).json()["edges"]


def test_redacted_session_report_omits_fact_metadata(metadata_client, product_scope):
    client, service = metadata_client
    user = "synthetic-alpha"
    from mnemotrace.types import Fact

    context, _ = product_scope(user)
    source_id, = service.remember(context, "Synthetic session statement", "synthetic-session", extract=False)["source_ids"]
    seed_facts(service, context, [Fact("user", "note", "Synthetic private value", user_id=context.user_id,
                                       provenance=[source_id], categories=["health"], categories_source="user", sensitive=True, text='user note Synthetic private value')])
    headers = hdr(user)
    endpoint = "/v1/sessions/report?session_id=synthetic-session"
    hidden, = client.get(endpoint, headers=headers).json()["facts"]
    assert hidden["redacted"] and not {"categories", "categories_source", "relation_id", "object_id", "object_type"} & hidden.keys()
    visible, = client.get(endpoint + "&include_sensitive=true", headers=headers).json()["facts"]
    assert (visible["categories"], visible["categories_source"]) == (["health"], "user")
    assert client.get("/v1/export", headers=headers).json()["facts"] == []
