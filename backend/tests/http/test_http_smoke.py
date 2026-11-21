"""验证 HTTP 闭环的请求超时和异常清理。"""

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "http_smoke.py"


def test_recall_read_timeout_preserves_other_budgets() -> None:
    """召回可等待模型返回，连接及后续请求仍沿用客户端默认值。"""
    sent: list[dict[str, float]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request.extensions["timeout"])
        return httpx.Response(200)

    send = runpy.run_path(str(_SCRIPT))["_request"]
    with httpx.Client(
        base_url="http://test", timeout=3, transport=httpx.MockTransport(respond)
    ) as client:
        send(client, "POST", "/v1/spaces/example/recall", 200, read_timeout=45)
        send(client, "GET", "/readyz", 200)

    assert sent == [
        {"connect": 3, "read": 45, "write": 3, "pool": 3},
        {"connect": 3, "read": 3, "write": 3, "pool": 3},
    ]


@pytest.mark.parametrize("missing_response", [False, True])
@pytest.mark.parametrize("space_visible", [True, False])
def test_cleanup_after_uncertain_space_creation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing_response: bool,
    space_visible: bool,
) -> None:
    """创建响应未确认时只清理唯一名称匹配的空间。"""
    name = ""
    deleted: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal name
        path = request.url.path
        if path in ("/livez", "/readyz"):
            return httpx.Response(200)
        if path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.1.0"})
        if request.method == "POST" and path == "/v1/spaces":
            name = json.loads(request.content)["name"]
            if missing_response:
                raise httpx.ReadError("连接断开", request=request)
            return httpx.Response(503, json={"code": "commit_outcome_unknown"})
        if request.method == "GET" and path == "/v1/spaces":
            if request.url.params.get("cursor") is None:
                return httpx.Response(
                    200,
                    json={
                        "items": [{"name": name + "2", "space_id": "unrelated"}],
                        "next_cursor": "second-page",
                    },
                )
            assert request.url.params["cursor"] == "second-page"
            return httpx.Response(
                200,
                json={
                    "items": [{"name": name, "space_id": "created"}]
                    if space_visible
                    else [],
                    "next_cursor": None,
                },
            )
        assert request.method == "DELETE"
        deleted.append(path)
        return httpx.Response(204)

    original_client = httpx.Client

    def client(
        *, base_url: httpx.URL, headers: dict[str, str], trust_env: bool
    ) -> httpx.Client:
        return original_client(
            base_url=base_url,
            headers=headers,
            trust_env=trust_env,
            transport=httpx.MockTransport(respond),
        )

    main: Callable[[], int] = runpy.run_path(str(_SCRIPT))["main"]
    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(sys, "argv", ["http_smoke.py"])
    monkeypatch.setenv("MNEMOTRACE_NEXT_SMOKE_TOKEN", "synthetic-test-token")

    assert main() == 1
    assert name.startswith("HTTP 验收 ")
    stderr = capsys.readouterr().err
    if space_visible:
        assert deleted == ["/v1/spaces/created"]
        assert "未能确认清理" not in stderr
    else:
        assert not deleted
        assert name in stderr
