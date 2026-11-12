"""分类和敏感标记跨 MCP 本地后端、HTTP 代理和工具层保持一致。"""
from __future__ import annotations

import json

import pytest

# 收集时检查依赖，异步测试不能等进入函数体后再决定跳过。
pytest.importorskip("mcp")
pytest.importorskip("pytest_asyncio")
httpx = pytest.importorskip("httpx")

from mnemotrace.mcp import server as server  # noqa: E402
from mnemotrace.mcp.backends import LocalBackend, RemoteBackend  # noqa: E402
from tests.postgres.fact_fixtures import seed_fact  # noqa: E402


@pytest.mark.asyncio
async def test_mcp_local_and_proxy_set_only_sensitivity(product_service, product_scope):
    from mnemotrace.server.app import app

    context, key = product_scope()
    memory = product_service.memory(context)
    fact = seed_fact(memory, "user", "prefers", "茶", text="用户偏好茶。", categories=["food"])
    local = LocalBackend(context.user_id, context.space_id, service=product_service)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as transport:
        remote = RemoteBackend("http://synthetic.test", key=key, space_id=context.space_id, client=transport)
        assert (await local.set_fact_sensitive(fact.id, True))["sensitive"] is True
        assert (await remote.set_fact_sensitive(fact.id, False))["sensitive"] is False
        server.set_backend(remote)
        try:
            response = await server.mcp.call_tool("mnemotrace_set_fact_sensitive", {"fact_id": fact.id, "sensitive": True})
            blocks = response[0] if isinstance(response, tuple) else response
            assert json.loads(blocks[0].text)["sensitive"] is True
        finally:
            server.set_backend(None)
    updated = memory.get_fact(fact.id)
    assert (updated.text, updated.categories, updated.provenance) == (fact.text, fact.categories, fact.provenance)


@pytest.mark.asyncio
async def test_categories_filter_is_forwarded_by_both_backends_and_applied_before_tool_paging(product_service, product_scope):
    from mnemotrace.server.app import app

    service = product_service
    context, key = product_scope()
    for obj, category in [("合成茶", "food"), ("合成作品", "music"), ("合成咖啡", "food")]:
        seed_fact(service.memory(context), "user", "prefers", obj, text=f'用户偏好{obj}。', categories=[category], sensitive=False)
    local = LocalBackend(context.user_id, context.space_id, service=service)
    requests = []

    async def record(request):
        requests.append(dict(request.url.params))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), event_hooks={"request": [record]}) as transport:
        remote = RemoteBackend("http://synthetic.test", key=key, space_id=context.space_id, client=transport)
        for backend in [local, remote]:
            data = await backend.memories(categories=["food"], limit=10, offset=0, include_superseded=False)
            assert len(data["facts"]) == 2 and all("food" in f["categories"] for f in data["facts"])
            server.set_backend(backend)
            try:
                response = await server.mcp.call_tool("mnemotrace_list_facts", {
                    "categories": ["food"], "limit": 1, "response_format": "json",
                })
                blocks = response[0] if isinstance(response, tuple) else response
                page = json.loads(blocks[0].text)
                assert page["total"] == 2 and page["count"] == 1 and page["has_more"]
                assert "food" in page["facts"][0]["categories"]
            finally:
                server.set_backend(None)
        assert len(requests) == 2
        assert all(row["categories"] == "food" and row["space_id"] == context.space_id for row in requests)
