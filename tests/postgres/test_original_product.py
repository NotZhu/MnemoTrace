"""Authorized source reads expose original text and coverage."""
import json

import pytest

from mnemotrace.llm import FakeLLM
from mnemotrace.store.postgres.errors import SpaceNotFound
from tests.test_pipeline_sources import LONG, DIGEST, respond


def test_explicit_original_http_enforces_scope_and_omits_internal_state(product_service, product_client, product_scope):
    context, key = product_scope("owner")
    other_context, other_key = product_scope("other")
    source, = product_service.remember(context, LONG, "synthetic", extract=False)["source_ids"]
    path = f"/v1/episodes/{source}"
    response = product_client.get(path, params={"space_id": context.space_id},
                                  headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200 and response.json()["content"] == LONG
    assert not {"embedding", "model_product", "lease_token"} & response.json().keys()
    for space in (context, other_context):
        assert product_client.get(path, params={"space_id": space.space_id},
                                  headers={"Authorization": f"Bearer {other_key}"}).status_code == 404


@pytest.mark.asyncio
async def test_local_and_remote_mcp_explicit_original_contract(product_service, product_scope):
    import httpx
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend

    context, key = product_scope()
    source, = product_service.remember(context, "synthetic original", "original", extract=False)["source_ids"]
    local = LocalBackend(context.user_id, context.space_id, service=product_service)
    assert (await local.get_episode(source))["content"] == "synthetic original"
    with pytest.raises(SpaceNotFound):
        await local.get_episode(source, "other")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"content": "synthetic original"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteBackend("https://synthetic.invalid", key, context.space_id, client)
        await remote.get_episode("event/id", "shared space")
        await remote.flush_processing(include_digests=True)
    assert requests[0].url.params["space_id"] == "shared space"
    assert requests[0].url.raw_path.startswith(b"/v1/episodes/event%2Fid?")
    assert json.loads(requests[1].content)["include_digests"] is True


@pytest.mark.asyncio
async def test_mcp_reports_digest_and_original_source_times(product_service, product_scope):
    from mnemotrace.mcp import server
    from mnemotrace.mcp.backends import LocalBackend
    from tests.postgres.test_mcp_product import call

    product_service.llm = FakeLLM(handler=respond)
    context, _ = product_scope()
    server.set_backend(LocalBackend(context.user_id, context.space_id, service=product_service))
    try:
        result = json.loads(await call("mnemotrace_remember", content=LONG, response_format="json"))
        operation, = result["operations"]
        assert operation["core_committed"]
        source, = result["source_ids"]
        original = json.loads(await call("mnemotrace_get_episode", episode_id=source, response_format="json"))
        assert original["content"] == LONG and original["ingested_at"] > 0
        assert original["digest"] in {"", DIGEST}
        assert "event_time" in original
    finally:
        server.set_backend(None)
