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
    first = product_client.post("/v1/remember", headers=headers, json=payload)
    assert first.status_code == 202 and first.json()['task']['state'] == 'pending'
    repeated = product_client.post("/v1/remember", headers=headers, json=payload)
    assert repeated.json()["task"] == first.json()["task"]
    assert repeated.json()["messages"] == first.json()["messages"]
    assert repeated.json()["messages"][0]["message_id"] == "a"
    conflict = product_client.post("/v1/remember", headers=headers, json={
        **payload, "messages": [message("a", "conflict")],
    })
    assert conflict.status_code == 409
    invalid = product_client.post("/v1/remember", headers=headers, json={
        **payload, "messages": [message("b", role="tool")],
    })
    assert invalid.status_code == 422
    denied = product_client.post("/v1/remember", headers={"Authorization": f"Bearer {other_key}"}, json=payload)
    assert denied.status_code == 404 and denied.json()["detail"]["code"] == "space_not_found"
    status = product_client.get("/v1/processing", headers=headers, params={"space_id": context.space_id}).json()
    assert status["operations"] == [first.json()["task"]]
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
    original = await local.remember("Synthetic item", "s", message_id="a", event_time=100)
    assert original["status"] == "accepted" and original["messages"][0]["message_id"] == "a"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        remote = RemoteBackend("https://synthetic.invalid", key, context.space_id, client)
        replay = await remote.remember([message("a")], "s")
    assert replay["status"] == "accepted"
    assert replay["source_ids"] == list(original["source_ids"])
    assert replay["task"] == original["task"]


def test_http_wait_returns_terminal_error_and_original_identity(product_service, product_client, product_scope):
    class ContextTooLarge(RuntimeError):
        code = 'context_length_exceeded'

    def broken(*_):
        raise ContextTooLarge("synthetic provider error")

    product_service.llm = FakeLLM(handler=broken)
    context, key = product_scope()
    headers = {"Authorization": f"Bearer {key}"}
    payload = dict(space_id=context.space_id, session_id="s", messages=[message("a")])
    accepted = product_client.post("/v1/remember", headers=headers, json=payload).json()
    operation_id = accepted["operation_id"]
    assert product_service.memory(context)._processing.run_next_stage(operation_id)
    result = product_client.post("/v1/remember", headers=headers, json=dict(payload, wait=True))
    assert result.status_code == 422, result.text
    detail = result.json()["detail"]
    assert detail["code"] == "model_budget_exceeded" and detail["status"] == "failed"
    assert detail["source_ids"] == accepted["source_ids"]
    assert detail["operation_id"] == operation_id and "synthetic provider error" not in result.text


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
