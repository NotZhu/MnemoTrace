from __future__ import annotations

import pytest


import json

from examples.cross_agent_lifecycle import (
    VERIFY_PHRASE,
    exercise_lifecycle,
    main,
)


@pytest.fixture
def example_http(product_client, product_scope):
    from urllib.parse import urlsplit

    scope, key = product_scope()
    def call(path, body=None, method='POST'):
        headers = {'Authorization': 'Bearer ' + key}
        if method == 'GET':
            separator = '&' if urlsplit(path).query else '?'
            response = product_client.get(path + separator + 'space_id=' + scope.space_id, headers=headers)
        else:
            response = product_client.request(method, path, json={**(body or {}), 'space_id': scope.space_id},
                                              headers=headers)
        response.raise_for_status()
        return response.json()
    return call


def test_cross_agent_lifecycle_example_http(example_http):
    lines: list[str] = []
    result = exercise_lifecycle(
        example_http,
        namespace="me",
        agent="codex",
        project="super-memory",
        thread="codex-smoke",
        output=lines.append,
    )

    assert result["found"] is True
    assert result["status_before"]["ok"] is True
    assert result["status_before"]["session"]["episodes"] == 0
    assert VERIFY_PHRASE in result["context_after"]
    assert result["status_after_write"]["session"]["episodes"] == 2
    assert result["flushed"]["ok"] is True
    assert result["report"]["episodes"] == 2
    assert len(result["report"]["facts"]) == 2
    assert any(VERIFY_PHRASE in fact["text"] for fact in result["report"]["facts"])
    assert any("PASS: lifecycle saved durable memory" in line for line in lines)

    status_payload = json.dumps(result["status_after_write"], ensure_ascii=False)
    assert VERIFY_PHRASE not in status_payload
    assert "Current task state" not in status_payload


def test_cross_agent_lifecycle_main_http(example_http, monkeypatch, capsys):
    monkeypatch.setattr("examples.cross_agent_lifecycle.http_call", lambda *_: example_http)
    main(["--space-id", "synthetic", "--key", "synthetic"])

    out = capsys.readouterr().out
    assert "mode: http" in out
    assert "agent_status:" in out
    assert "session_report: episodes=2 facts=2 redacted=0" in out
    assert "PASS: lifecycle saved durable memory and the session report audits it." in out


def test_cross_agent_lifecycle_main_exits_when_verify_fails(example_http, monkeypatch):
    def empty_http_call(*_):
        call = example_http

        def wrapper(path: str, body: dict | None = None, method: str = "POST") -> dict:
            if path == "/v1/recall":
                return {"context": ""}
            if path.startswith("/v1/sessions/report"):
                return {"episodes": 3, "facts": [], "redacted": 0}
            return call(path, body, method)

        return wrapper

    monkeypatch.setattr("examples.cross_agent_lifecycle.http_call", empty_http_call)

    with pytest.raises(SystemExit) as exc:
        main(["--space-id", "synthetic", "--key", "synthetic"])

    assert exc.value.code == 1
