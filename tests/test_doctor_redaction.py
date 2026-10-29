"""Report control fields must survive content redaction and caller decisions."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mnemotrace import agent_doctor as doctor
from mnemotrace import agent_setup as setup
from mnemotrace.credentials import redact


def _successful_runtime(monkeypatch, key, remote_status):
    probes = [
        {"name": "Python MCP lifecycle", "status": "ok", "detail": "synthetic runtime"},
        {"name": "MCP stdio server", "status": "ok", "detail": "synthetic transport"},
        {
            "name": "Remote HTTP lifecycle", "status": remote_status,
            "detail": f"Synthetic provider output: Bearer {key}",
            "command": ["HTTP", f"http://example.invalid/{key}", key],
            "data": {
                "ok": True, "count": 3, "status": key,
                "nested": [{"checks": key}, 2, False, None],
            },
        },
    ]
    monkeypatch.setattr(doctor, "_python_probe", lambda *a: probes[0])
    monkeypatch.setattr(doctor, "_stdio_probe", lambda *a: probes[1])
    monkeypatch.setattr(doctor, "_remote_http_probe", lambda *a: probes[2])
    monkeypatch.setattr(doctor, "_client_probe", lambda *a: [])
    return probes


def test_shared_redaction_preserves_dictionary_keys_and_value_types():
    original = {"ok": "ok", "nested": [{"ok": "Bearer ok"}, True, False, 3, None]}
    before = copy.deepcopy(original)
    cleaned = redact(original, "ok")
    assert cleaned == {"ok": "<REDACTED>", "nested": [{"ok": "Bearer <REDACTED>"}, True, False, 3, None]}
    assert original == before


@pytest.mark.parametrize("key", [
    "ok", "warn", "fail", "status", "summary", "checks", "client", "codex",
    "Python MCP lifecycle", "synthetic-long-key",
])
@pytest.mark.parametrize("remote_status", ["ok", "warn", "fail"])
def test_diagnose_preserves_structure_and_sanitizes_content(monkeypatch, key, remote_status):
    probes = _successful_runtime(monkeypatch, key, remote_status)
    before = copy.deepcopy(probes)
    report = doctor.diagnose(
        client="codex", python_cmd=f"runtime/{key}/python",
        api_url=f"http://example.invalid/{key}", api_key=key,
    )
    assert set(report) == {"ok", "client", "python", "api_url", "checks", "summary"}
    assert report["ok"] is (remote_status != "fail")
    assert report["client"] == "codex"
    assert report["summary"] == {
        "ok": 2 + (remote_status == "ok"),
        "warn": 1 + int(remote_status == "warn"), "fail": int(remote_status == "fail"),
    }
    assert report["python"] == "runtime/<REDACTED>/python"
    assert report["api_url"] == "http://example.invalid/<REDACTED>"
    assert [check["name"] for check in report["checks"]] == [p["name"] for p in probes] + ["LLM extraction"]
    assert [check["status"] for check in report["checks"]] == ["ok", "ok", remote_status, "warn"]
    assert report["checks"][-1]["data"] == {"llm_configured": False, "model_call_performed": False}
    remote = report["checks"][2]
    assert remote["detail"] == "Synthetic provider output: Bearer <REDACTED>"
    assert remote["command"] == ["HTTP", "http://example.invalid/<REDACTED>", "<REDACTED>"]
    assert remote["data"] == {
        "ok": True, "count": 3, "status": "<REDACTED>",
        "nested": [{"checks": "<REDACTED>"}, 2, False, None],
    }
    assert probes == before
    remote["data"]["nested"].append("mutated output")
    assert probes == before
    rendered = doctor.render_report(report)
    assert "summary: " + str(report["summary"]["ok"]) + " passed" in rendered
    assert f"{'PASS' if remote_status == 'ok' else remote_status.upper()} Remote HTTP lifecycle" in rendered


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("remote_status", ["ok", "warn", "fail"])
def test_doctor_cli_exit_matches_probes_with_key_ok(monkeypatch, capsys, json_mode, remote_status):
    _successful_runtime(monkeypatch, "ok", remote_status)
    command = ["--api-url", "http://example.invalid/ok", "--api-key", "ok"]
    if json_mode:
        command.append("--json")
    with pytest.raises(SystemExit) as stopped:
        doctor.main(command)
    assert stopped.value.code == int(remote_status == "fail")
    output = capsys.readouterr()
    assert not output.err
    if json_mode:
        report = json.loads(output.out)
        assert report["ok"] is (remote_status != "fail")
        assert report["checks"][2]["status"] == remote_status
        assert report["summary"]["ok"] == 2 + (remote_status == "ok")
    else:
        assert "PASS Python MCP lifecycle" in output.out
        assert f"{'PASS' if remote_status == 'ok' else remote_status.upper()} Remote HTTP lifecycle" in output.out
    assert "Bearer ok" not in output.out
    assert "example.invalid/ok" not in output.out


@pytest.mark.parametrize("entry", ["codex", "mcp", "bootstrap"])
@pytest.mark.parametrize("remote_status", ["ok", "warn", "fail"])
def test_setup_decisions_use_preserved_doctor_report(tmp_path, monkeypatch, capsys, entry, remote_status):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _successful_runtime(monkeypatch, "ok", remote_status)
    command = ["--api-key", "ok", "--api-url", "http://example.invalid"]
    if entry == "codex":
        command += ["--install-codex", "--doctor", "--codex-config", str(tmp_path / "config.toml")]
    elif entry == "mcp":
        command += ["--install-mcp-json", "--doctor", "--mcp-json", str(tmp_path / ".mcp.json")]
    else:
        command += ["--bootstrap", "--codex-config", str(tmp_path / "config.toml"), "--mcp-json", str(tmp_path / ".mcp.json")]
    if remote_status == "fail":
        with pytest.raises(SystemExit) as stopped:
            setup.main(command)
        assert stopped.value.code == 1
    else:
        setup.main(command)
    output = capsys.readouterr()
    assert not output.err
    assert "PASS Python MCP lifecycle" in output.out
    assert f"{'PASS' if remote_status == 'ok' else remote_status.upper()} Remote HTTP lifecycle" in output.out
    assert "Bearer ok" not in output.out
