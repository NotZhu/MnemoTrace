"""分类和对象类型跨 MCP 本地后端、HTTP 代理和工具层保持一致。"""
from __future__ import annotations

import json

import pytest

# 收集时检查依赖，异步测试不能等进入函数体后再决定跳过。
pytest.importorskip("mcp")
pytest.importorskip("pytest_asyncio")
httpx = pytest.importorskip("httpx")

from mnemotrace.mcp import server as server  # noqa: E402
from mnemotrace.mcp.backends import LocalBackend, RemoteBackend  # noqa: E402


@pytest.mark.asyncio
async def test_mcp_local_and_proxy_preserve_fact_metadata_contract(product_service, product_scope):
    service = product_service
    context, key = product_scope()
    local = LocalBackend(context.user_id, context.space_id, service=service)
    requests = []

    def route(request):
        payload = json.loads(request.content)
        requests.append(dict(payload))
        assert payload.pop("space_id") == context.space_id
        if request.method == "POST":
            result = service.add_fact(context, **payload)
        else:
            result = service.update_fact(context, request.url.path.rsplit("/", 1)[-1], **payload)
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as transport:
        remote = RemoteBackend("http://synthetic.test", key=key, space_id=context.space_id, client=transport)
        for backend, obj in [(local, "茶"), (remote, "咖啡")]:
            added = await backend.add_fact("user", "prefers", obj, categories=["food"], object_kind="entity")
            assert (added["categories"], added["categories_source"], added["object_kind"]) == (["food"], "user", "entity")
            updated = await backend.update_fact(added["id"], categories=[])
            assert (updated["categories"], updated["categories_source"]) == ([], "unset")
        assert requests[0]["categories"] == ["food"] and requests[1]["categories"] == []

        server.set_backend(local)
        try:
            await server.mcp.call_tool("mnemotrace_add_fact", {
                "predicate": "prefers", "object": "jazz", "categories": ["music"],
            })
            fact = next(f for f in service.memory(context).facts() if f.object == "jazz")
            response = await server.mcp.call_tool("mnemotrace_update_fact", {
                "fact_id": fact.id, "categories": ["misc"], "response_format": "json",
            })
            blocks = response[0] if isinstance(response, tuple) else response
            assert json.loads(blocks[0].text)["categories_source"] == "user"
            await server.mcp.call_tool("mnemotrace_update_fact", {"fact_id": fact.id, "categories": []})
            edited = service.memory(context).get_fact(fact.id)
            assert (edited.categories, edited.categories_source) == ([], "unset")
        finally:
            server.set_backend(None)


@pytest.mark.asyncio
async def test_categories_filter_is_forwarded_by_both_backends_and_applied_before_tool_paging(product_service, product_scope):
    from mnemotrace.server.app import app

    service = product_service
    context, key = product_scope()
    for obj, category in [("合成茶", "food"), ("合成作品", "music"), ("合成咖啡", "food")]:
        service.add_fact(context, "user", "prefers", obj, categories=[category], sensitive=False)
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
