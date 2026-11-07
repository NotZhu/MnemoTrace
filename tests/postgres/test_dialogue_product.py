"""HTTP and both MCP backends honor the durable dialogue contract."""
import json

import pytest

from mnemotrace.llm import FakeLLM
from tests.test_message_pipeline import message


def test_http_dialogue_acl_conflict_and_strict_roles(product_client, product_scope):
    context, key = product_scope("dialogue")
    _, other_key = product_scope("other")
    headers = {"Authorization": f"Bearer {key}"}
    payload = dict(space_id=context.space_id, session_id="s", messages=[message("a")])
    first = product_client.post("/v1/messages", headers=headers, json=payload)
    assert first.status_code == 202 and first.json()["accepted"] == 1
    repeated = product_client.post("/v1/messages", headers=headers, json=payload)
    assert repeated.json()["accepted"] == 0
    assert repeated.json()["messages"] == first.json()["messages"]
    assert repeated.json()["messages"][0]["message_id"] == "a"
    conflict = product_client.post("/v1/messages", headers=headers, json={
        **payload, "messages": [message("a", "conflict")],
    })
    assert conflict.status_code == 409
    invalid = product_client.post("/v1/messages", headers=headers, json={
        **payload, "messages": [message("b", role="tool")],
    })
    assert invalid.status_code == 422
    denied = product_client.post("/v1/messages", headers={"Authorization": f"Bearer {other_key}"}, json=payload)
    assert denied.status_code == 404 and denied.json()["detail"]["code"] == "space_not_found"
    status = product_client.get("/v1/processing", headers=headers, params={"space_id": context.space_id}).json()
    assert sum(row["kind"] == "extract" for row in status["operations"]) == 1
    assert "content" not in json.dumps(status)
    report = product_client.get("/v1/sessions/report", headers=headers,
                                params={"space_id": context.space_id, "session_id": "s"}).json()
    assert report["episodes_pending"] == 1


@pytest.mark.asyncio
async def test_mcp_local_and_remote_use_same_messages(product_service, product_scope):
    import httpx
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend
    from mnemotrace.server.app import app

    context, key = product_scope()
    local = LocalBackend(context.user_id, context.space_id, service=product_service)
    original = await local.remember_messages([message("a")], "s", mode="historical")
    assert original["status"] == "accepted" and original["messages"][0]["message_id"] == "a"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        remote = RemoteBackend("https://synthetic.invalid", key, context.space_id, client)
        replay = await remote.remember_messages([message("a")], "s", "historical")
    assert replay["accepted"] == 0
    assert replay["source_ids"] == list(original["source_ids"])
    assert replay["operation_ids"] == list(original["operation_ids"])


def test_http_immediate_failure_retains_progress(product_service, product_client, product_scope):
    def broken(*_):
        raise RuntimeError("synthetic model failure")

    product_service.llm = FakeLLM(handler=broken)
    context, key = product_scope()
    headers = {"Authorization": f"Bearer {key}"}
    payload = dict(space_id=context.space_id, session_id="s", processing="immediate",
                   timeout=0.2, messages=[message("a")])
    result = product_client.post("/v1/messages", headers=headers, json=payload)
    assert result.status_code == 503, result.text
    detail = result.json()["detail"]
    assert detail["code"] == "processing_incomplete" and detail["status"] == "timed_out"
    assert detail["stored_raw"] and detail["episode_ids"] and "synthetic model failure" not in result.text
    operation_id, = detail["operation_ids"]
    saved = product_client.get(f"/v1/operations/{operation_id}", headers=headers,
                               params={"space_id": context.space_id}).json()
    assert saved["stored_raw"] and not saved["core_committed"]


@pytest.mark.parametrize("path", ["/v1/remember", "/v1/chat/completions"])
def test_invalid_scope_rejected_before_ingest(product_service, product_client, product_scope, path):
    context, key = product_scope()
    if path == "/v1/remember":
        body = {"space_id": context.space_id, "scope": "invalid", "content": "Synthetic input"}
    else:
        body = {"messages": [{"role": "user", "content": "Synthetic input"}],
                "memory": {"space_id": context.space_id, "scope": "invalid"}}
    response = product_client.post(path, headers={"Authorization": f"Bearer {key}"}, json=body)
    assert response.status_code == 422
    assert not product_service.memory(context).list_sources()
