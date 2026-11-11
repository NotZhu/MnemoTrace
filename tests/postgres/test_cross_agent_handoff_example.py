from __future__ import annotations

import pytest


from examples.cross_agent_handoff import exercise_handoff, main


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


def test_cross_agent_handoff_example_http(example_http):
    lines: list[str] = []
    result = exercise_handoff(
        example_http,
        namespace="me",
        project="super-memory",
        output=lines.append,
    )

    assert result["found"] is True
    assert result["remembered"]["ok"] is True
    assert result["flushed_source"]["ok"] is True
    assert result["flushed_target"]["ok"] is True
    assert "committed eval logs" in result["context"]
    source_id, = result["remembered"]["source_ids"]
    source = example_http(f"/v1/episodes/{source_id}", method="GET")
    assert source["session_id"] == result["source_session"]
    assert any("PASS: target agent recalled" in line for line in lines)


def test_cross_agent_handoff_main_http(example_http, monkeypatch, capsys):
    monkeypatch.setattr("examples.cross_agent_handoff.http_call", lambda *_: example_http)
    main(["--space-id", "synthetic", "--key", "synthetic"])

    out = capsys.readouterr().out
    assert "mode: http" in out
    assert "PASS: target agent recalled the source agent's durable memory." in out


def test_cross_agent_handoff_main_exits_when_verify_fails(example_http, monkeypatch):
    def empty_http_call(*_):
        call = example_http

        def wrapper(path: str, body: dict | None = None, method: str = "POST") -> dict:
            if path == "/v1/recall":
                return {"context": ""}
            return call(path, body, method)

        return wrapper

    monkeypatch.setattr("examples.cross_agent_handoff.http_call", empty_http_call)

    with pytest.raises(SystemExit) as exc:
        main(["--space-id", "synthetic", "--key", "synthetic"])

    assert exc.value.code == 1
