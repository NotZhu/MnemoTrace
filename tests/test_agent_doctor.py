from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

from mnemotrace import agent_doctor as D
from mnemotrace.types import Fact


@pytest.mark.parametrize('probe', [D._python_probe, D._stdio_probe])
def test_real_offline_lifecycle_probe(probe, tmp_path, monkeypatch):
    # Subprocesses do not inherit the parent's patched dotenv loader. Use an empty cwd
    # while explicitly importing this checkout, so developer model settings stay out.
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MNEMOTRACE_LLM', '')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.setenv('TEMP', str(tmp_path))
    monkeypatch.setenv('TMP', str(tmp_path))
    result = probe(sys.executable)
    assert result['status'] == 'ok', result
    assert result['data']['remembered'] and result['data']['recalled']


def safe_snapshot():
    return {"include_sensitive": False, "episodes": [], "facts": [asdict(Fact(
        subject="Synthetic", predicate="decision",
        object="MnemoTrace remote doctor verifies cross-agent handoff.",
        text="Project decision: MnemoTrace remote doctor verifies cross-agent handoff.",
    ))]}


def _completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def test_private_file_identity_and_failure_output_are_redacted(tmp_path, monkeypatch, capsys):
    from mnemotrace.credentials import store_key
    reference = store_key("synthetic-doctor-secret", directory=tmp_path / "credentials")
    monkeypatch.setattr(D, "_python_probe", lambda python: {"name": "synthetic", "status": "fail"})
    observed = []
    def remote(url, key):
        observed.append(key)
        return {"name": "remote", "status": "fail", "detail": f"synthetic failure echoed {key}", "data": [key]}
    monkeypatch.setattr(D, "_remote_http_probe", remote)
    with pytest.raises(SystemExit) as stopped:
        D.main(["--api-url", "http://example.invalid", "--api-key-file", reference, "--json"])
    assert stopped.value.code == 1
    assert observed == ["synthetic-doctor-secret"]
    output = capsys.readouterr()
    assert "synthetic-doctor-secret" not in output.out + output.err
    assert "<REDACTED>" in output.out


def test_diagnose_codex_success(monkeypatch):
    probe = {
        "python": "/opt/mnemotrace/bin/python",
        "before_empty": True,
        "remembered": True,
        "source_session": "doctor:source:smoke",
        "target_session": "doctor:target:smoke",
        "focus_ok": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
        "facts_live": 1,
        "summaries": 1,
    }
    stdio_probe = {
        "python": "/opt/mnemotrace/bin/python",
        "isolated_processes": True,
        "missing_tools": [],
        "remembered": True,
        "focused": True,
        "target_preloaded": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "source_session": "doctor:source:stdio",
        "target_session": "doctor:target:stdio",
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
    }

    def fake_run(parts, timeout=30.0):
        if parts[:2] == ["/opt/mnemotrace/bin/python", "-c"]:
            if "StdioServerParameters" in parts[2]:
                return _completed(parts, stdout=json.dumps(stdio_probe) + "\n")
            return _completed(parts, stdout=json.dumps(probe) + "\n")
        if parts == ["/usr/local/bin/codex", "--version"]:
            return _completed(parts, stdout="codex-cli 0.142.2\n")
        if parts == ["/usr/local/bin/codex", "mcp", "--help"]:
            return _completed(parts, stdout="Manage external MCP servers for Codex\nUsage: codex mcp")
        raise AssertionError(parts)

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

    report = D.diagnose(client="codex", python_cmd="/opt/mnemotrace/bin/python")

    assert report["ok"] is True
    assert report["summary"] == {"ok": 4, "warn": 1, "fail": 0}
    assert report["checks"][0]["data"]["facts_live"] == 1
    assert report["checks"][0]["data"]["recalled_source_session"] is True
    assert "doctor:source:smoke" in report["checks"][0]["detail"]
    assert "doctor:target:smoke" in report["checks"][0]["detail"]
    assert report["checks"][1]["name"] == "MCP stdio server"
    assert report["checks"][1]["data"]["missing_tools"] == []
    assert report["checks"][1]["data"]["isolated_processes"] is True
    assert report["checks"][1]["data"]["target_preloaded"] is True
    assert report["checks"][1]["data"]["recalled_source_session"] is True
    assert "launched two" in report["checks"][1]["detail"]
    assert "doctor:source:stdio" in report["checks"][1]["detail"]
    assert "doctor:target:stdio" in report["checks"][1]["detail"]


def test_python_probe_failure_mentions_mcp_install(monkeypatch):
    def fake_run(parts, timeout=30.0):
        return _completed(parts, returncode=1, stderr="ModuleNotFoundError: No module named 'mcp'")

    monkeypatch.setattr(D, "_run", fake_run)

    report = D.diagnose(client="none", python_cmd="/bad/python")

    assert report["ok"] is False
    assert report["summary"]["fail"] == 1
    assert 'pip install "mnemotrace-memory[mcp]"' in report["checks"][0]["detail"]


def test_diagnose_remote_http_lifecycle_success(monkeypatch):
    probe = {
        "python": "/opt/mnemotrace/bin/python",
        "before_empty": True,
        "remembered": True,
        "source_session": "doctor:source:smoke",
        "target_session": "doctor:target:smoke",
        "focus_ok": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
        "facts_live": 1,
        "summaries": 1,
    }
    stdio_probe = {
        "python": "/opt/mnemotrace/bin/python",
        "isolated_processes": True,
        "missing_tools": [],
        "remembered": True,
        "focused": True,
        "target_preloaded": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "source_session": "doctor:source:stdio",
        "target_session": "doctor:target:stdio",
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
    }
    source_session = ""

    def fake_run(parts, timeout=30.0):
        if parts[:2] == ["/opt/mnemotrace/bin/python", "-c"] and "StdioServerParameters" in parts[2]:
            return _completed(parts, stdout=json.dumps(stdio_probe) + "\n")
        return _completed(parts, stdout=json.dumps(probe) + "\n")

    def fake_http(api_url, api_key, path, body, timeout=30.0):
        nonlocal source_session
        assert api_key == "sk-secret"
        if path == "/v1/facts":
            return {"ok": True}
        if path == "/v1/remember":
            source_session = body["session_id"]
            return {"ok": True}
        if path == "/v1/sessions/close":
            return {"ok": True}
        if path == "/v1/recall":
            return {
                "context": (
                    "Project decision: MnemoTrace remote doctor verifies cross-agent handoff.\n"
                    f"(session: {source_session})"
                )
            }
        raise AssertionError(path)

    def fake_get(api_url, api_key, path, params=None, timeout=30.0):
        assert api_key == "sk-secret"
        if path == "/v1/export":
            assert params == {"include_sensitive": "false"}
            return safe_snapshot()
        if path == "/v1/sessions":
            assert params == {"q": source_session, "limit": 10, "offset": 0}
            return {
                "ok": True,
                "sessions": [{"id": source_session, "facts_added": 1, "episodes": 1}],
                "page": {"total": 1},
            }
        assert path == "/v1/sessions/report"
        assert params == {"session_id": source_session}
        return {
            "ok": True,
            "session_id": source_session,
            "episodes": 1,
            "facts_added": 1,
            "facts": [{
                "text": "Project decision: MnemoTrace remote doctor verifies cross-agent handoff.",
            }],
        }

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D, "_http_json", fake_http)
    monkeypatch.setattr(D, "_http_get_json", fake_get)

    report = D.diagnose(
        client="none",
        python_cmd="/opt/mnemotrace/bin/python",
        api_url="http://mnemotrace.test",
        api_key="sk-secret",
    )

    assert report["ok"] is True
    assert report["summary"] == {"ok": 3, "warn": 1, "fail": 0}
    assert report["checks"][2]["name"] == "Remote HTTP lifecycle"
    assert report["checks"][2]["data"]["recalled_source_session"] is True
    assert report["checks"][2]["data"]["reported"] is True
    assert report["checks"][2]["data"]["sessions_ok"] is True
    rendered = D.render_report(report)
    assert "http://mnemotrace.test" in rendered
    assert "sk-secret" not in rendered


def test_diagnose_validates_codex_config(monkeypatch, tmp_path):
    probe = {
        "python": "/opt/mnemotrace/bin/python",
        "before_empty": True,
        "remembered": True,
        "source_session": "doctor:source:smoke",
        "target_session": "doctor:target:smoke",
        "focus_ok": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
        "facts_live": 1,
        "summaries": 1,
    }
    stdio_probe = {
        "python": "/opt/mnemotrace/bin/python",
        "isolated_processes": True,
        "missing_tools": [],
        "remembered": True,
        "focused": True,
        "target_preloaded": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "source_session": "doctor:source:stdio",
        "target_session": "doctor:target:stdio",
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
    }
    from mnemotrace.credentials import store_key
    reference = store_key("sk-secret", directory=tmp_path / "credentials")
    config = tmp_path / "config.toml"
    config.write_text(
        "[mcp_servers.mnemotrace]\n"
        'command = "/opt/mnemotrace/bin/python"\n'
        'args = ' + json.dumps(["-m", "mnemotrace.mcp", "--api-url", "http://mnemotrace.test", "--api-key-file", reference], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    source_session = ""

    def fake_run(parts, timeout=30.0):
        if parts[:2] == ["/opt/mnemotrace/bin/python", "-c"] and "StdioServerParameters" in parts[2]:
            return _completed(parts, stdout=json.dumps(stdio_probe) + "\n")
        return _completed(parts, stdout=json.dumps(probe) + "\n")

    def fake_http(api_url, api_key, path, body, timeout=30.0):
        nonlocal source_session
        if path == "/v1/facts":
            return {"ok": True}
        if path == "/v1/remember":
            source_session = body["session_id"]
            return {"ok": True}
        if path == "/v1/sessions/close":
            return {"ok": True}
        if path == "/v1/recall":
            return {
                "context": (
                    "Project decision: MnemoTrace remote doctor verifies cross-agent handoff.\n"
                    f"(session: {source_session})"
                )
            }
        raise AssertionError(path)

    def fake_get(api_url, api_key, path, params=None, timeout=30.0):
        if path == "/v1/export":
            assert params == {"include_sensitive": "false"}
            return safe_snapshot()
        if path == "/v1/sessions":
            assert params == {"q": source_session, "limit": 10, "offset": 0}
            return {
                "ok": True,
                "sessions": [{"id": source_session, "facts_added": 1, "episodes": 1}],
                "page": {"total": 1},
            }
        assert path == "/v1/sessions/report"
        assert params == {"session_id": source_session}
        return {
            "ok": True,
            "session_id": source_session,
            "episodes": 1,
            "facts_added": 1,
            "facts": [{
                "text": "Project decision: MnemoTrace remote doctor verifies cross-agent handoff.",
            }],
        }

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D, "_http_json", fake_http)
    monkeypatch.setattr(D, "_http_get_json", fake_get)

    report = D.diagnose(
        client="none",
        python_cmd="/opt/mnemotrace/bin/python",
        api_url="http://mnemotrace.test",
        api_key="sk-secret",
        codex_config=str(config),
    )

    assert report["ok"] is True
    check = next(c for c in report["checks"] if c["name"] == "Codex config")
    assert check["status"] == "ok"
    assert "sk-secret" not in D.render_report(report)


def test_diagnose_validates_mcp_json_config(monkeypatch, tmp_path):
    probe = {
        "python": "/opt/mnemotrace/bin/python",
        "before_empty": True,
        "remembered": True,
        "source_session": "doctor:source:smoke",
        "target_session": "doctor:target:smoke",
        "focus_ok": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
        "facts_live": 1,
        "summaries": 1,
    }
    stdio_probe = {
        "python": "/opt/mnemotrace/bin/python",
        "isolated_processes": True,
        "missing_tools": [],
        "remembered": True,
        "focused": True,
        "target_preloaded": True,
        "status_ok": True,
        "report_ok": True,
        "sessions_ok": True,
        "export_ok": True,
        "source_session": "doctor:source:stdio",
        "target_session": "doctor:target:stdio",
        "recalled": True,
        "closed": True,
        "recalled_phrase": True,
        "recalled_source_session": True,
    }
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "mnemotrace": {
                "type": "stdio",
                "command": "/opt/mnemotrace/bin/python",
                "args": ["-m", "mnemotrace.mcp", "--namespace", "me"],
            }
        }
    }))

    def fake_run(parts, timeout=30.0):
        if parts[:2] == ["/opt/mnemotrace/bin/python", "-c"] and "StdioServerParameters" in parts[2]:
            return _completed(parts, stdout=json.dumps(stdio_probe) + "\n")
        return _completed(parts, stdout=json.dumps(probe) + "\n")

    monkeypatch.setattr(D, "_run", fake_run)

    report = D.diagnose(
        client="none",
        python_cmd="/opt/mnemotrace/bin/python",
        mcp_json=str(config),
    )

    assert report["ok"] is True
    assert next(c for c in report["checks"] if c["name"] == "MCP JSON config")["status"] == "ok"


def test_config_probe_fails_on_mismatched_python(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "mnemotrace": {"command": "wrong-python", "args": ["-m", "mnemotrace.mcp"]}
        }
    }))

    check = D._mcp_json_config_probe(
        str(config),
        server_name="mnemotrace",
        python_cmd="/opt/mnemotrace/bin/python",
        api_url=None,
        api_key=None,
    )

    assert check["status"] == "fail"
    assert "command does not match" in check["detail"]


def test_config_probe_validates_local_namespace(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "mnemotrace": {
                "command": "/opt/mnemotrace/bin/python",
                "args": ["-m", "mnemotrace.mcp", "--namespace", "local-me"],
            }
        }
    }))

    ok = D._mcp_json_config_probe(
        str(config),
        server_name="mnemotrace",
        python_cmd="/opt/mnemotrace/bin/python",
        api_url=None,
        api_key="local-me",
    )
    wrong = D._mcp_json_config_probe(
        str(config),
        server_name="mnemotrace",
        python_cmd="/opt/mnemotrace/bin/python",
        api_url=None,
        api_key="other-user",
    )

    assert ok["status"] == "ok"
    assert ok["data"]["namespace_ok"] is True
    assert wrong["status"] == "fail"
    assert wrong["data"]["namespace_ok"] is False
    assert "local namespace" in wrong["detail"]


def test_remote_http_probe_reports_unauthorized(monkeypatch):
    def fake_http(api_url, api_key, path, body, timeout=30.0):
        raise urllib.error.HTTPError(
            api_url + path,
            401,
            "Unauthorized",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(D, "_http_json", fake_http)

    check = D._remote_http_probe("http://mnemotrace.test", "bad-key")

    assert check["status"] == "fail"
    assert "HTTP 401" in check["detail"]
    assert "bad-key" not in str(check)


def test_render_report_is_actionable():
    text = D.render_report({
        "ok": False,
        "client": "codex",
        "python": "python",
        "summary": {"ok": 1, "warn": 0, "fail": 1},
        "checks": [
            {"name": "Python MCP lifecycle", "status": "ok", "detail": "ready"},
            {"name": "Codex CLI", "status": "fail", "detail": "`codex` not found on PATH."},
        ],
    })

    assert "PASS Python MCP lifecycle" in text
    assert "FAIL Codex CLI" in text
    assert "Fix the failing check" in text
