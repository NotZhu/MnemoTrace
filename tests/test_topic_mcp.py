"""主题元数据跨 MCP 本地后端、HTTP 代理和工具层保持一致。"""
from __future__ import annotations

import json

import pytest

# 收集时检查依赖，异步测试不能等进入函数体后再决定跳过。
pytest.importorskip("mcp")
pytest.importorskip("pytest_asyncio")
httpx = pytest.importorskip("httpx")

from mnemotrace.mcp import server as server  # noqa: E402
from mnemotrace.mcp.backends import LocalBackend, RemoteBackend  # noqa: E402
from mnemotrace.service import MemoryService  # noqa: E402


@pytest.mark.asyncio
async def test_mcp_local_and_proxy_preserve_topic_contract(tmp_path):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    local = LocalBackend(namespace="synthetic", service=service)
    requests = []

    def route(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if request.method == "POST":
            result = service.add_fact("synthetic", **payload)
        else:
            result = service.update_fact("synthetic", request.url.path.rsplit("/", 1)[-1], **payload)
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as transport:
        remote = RemoteBackend("http://synthetic.test", key="sk-synthetic", client=transport)
        for backend, obj in [(local, "茶"), (remote, "咖啡")]:
            added = await backend.add_fact("user", "prefers", obj, topic="food")
            assert (added["topic"], added["topic_source"]) == ("food", "user")
            updated = await backend.update_fact(added["id"], reset_topic=True)
            assert (updated["topic"], updated["topic_source"]) == ("food", "rule")
        assert requests[0]["topic"] == "food" and requests[1]["reset_topic"] is True

        server.set_backend(local)
        try:
            await server.mcp.call_tool("mnemotrace_add_fact", {
                "predicate": "prefers", "object": "jazz", "topic": "music",
            })
            fact = next(f for f in service.get("synthetic").fact_store.values() if f.object == "jazz")
            response = await server.mcp.call_tool("mnemotrace_update_fact", {
                "fact_id": fact.id, "topic": "unknown", "response_format": "json",
            })
            blocks = response[0] if isinstance(response, tuple) else response
            assert json.loads(blocks[0].text)["topic_source"] == "user"
            await server.mcp.call_tool("mnemotrace_update_fact", {"fact_id": fact.id, "reset_topic": True})
            assert (fact.topic, fact.topic_source) == ("music", "rule")
        finally:
            server.set_backend(None)
