"""Generated syntax and connection values are not diagnostic free text."""
from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import re
import shlex
from pathlib import Path

import pytest

from mnemotrace import agent_setup as setup
from mnemotrace.credentials import read_key, store_key


def _blocks(output, language):
    return re.findall(r"```" + language + r"\n(.*?)```", output, re.S)


def _toml(text):
    try:
        import tomllib
    except ModuleNotFoundError:
        tomllib = pytest.importorskip("tomli")
    return tomllib.load(io.BytesIO(text.encode("utf-8")))


def _servers(output):
    for block in _blocks(output, "toml"):
        yield _toml(block)["mcp_servers"]["mnemotrace"]
    for block in _blocks(output, "json"):
        data = json.loads(block)
        yield data["mcpServers"]["mnemotrace"] if "mcpServers" in data else data
    for block in _blocks(output, "bash"):
        parts = shlex.split(block)
        if parts[:3] == ["claude", "mcp", "add-json"]:
            assert parts[3] == "mnemotrace"
            yield json.loads(parts[4])
        elif parts[:3] == ["codex", "mcp", "add"]:
            assert parts[3] == "mnemotrace"
            start = parts.index("--") + 1
            yield {"command": parts[start], "args": parts[start + 1:]}


@pytest.mark.parametrize("key", ["mnemotrace", "args", "command", "python", "codex", "PASS", "synthetic-long-key"])
@pytest.mark.parametrize("entry", ["markdown", "codex", "mcp", "bootstrap"])
def test_rendered_outputs_preserve_syntax_and_connection_values(tmp_path, monkeypatch, capsys, key, entry):
    reference = store_key(key, directory=tmp_path / ".mnemotrace" / "credentials with spaces")
    url = f"http://mnemotrace.test/{key}"
    python = str(tmp_path / "python bin" / "python")
    session = f"codex:{key}:thread"
    args = ["-m", "mnemotrace.mcp", "--api-url", url, "--api-key-file", reference]
    server = {"type": "stdio", "command": python, "args": args}
    block = '[mcp_servers.mnemotrace]\ncommand = ' + json.dumps(python, ensure_ascii=False) + '\nargs = ' + json.dumps(args, ensure_ascii=False) + '\n'
    common = {"action": "install", "changed": True, "dry_run": False,
              "path": str(tmp_path / f"{key}.config"), "backup": str(tmp_path / f"{key}.backup"),
              "api_key_file": reference}
    codex = {**common, "block": block}
    mcp = {**common, "server_name": f"mnemotrace-{key}", "server": server}
    report = {"ok": True, "client": "codex", "python": python, "api_url": url,
              "summary": {"ok": 1, "warn": 0, "fail": 0},
              "checks": [{"name": "MCP mnemotrace", "status": "ok", "detail": f"Bearer {key}"}]}
    before = copy.deepcopy((codex, mcp, report))
    options = dict(python_cmd=python, api_url=url, api_key=key, doctor_skipped=f"Synthetic reason: Bearer {key}")
    if entry == "markdown":
        setup.main(["--client", "all", "--api-key-file", reference, "--api-url", url,
                    "--python", python, "--session-id", session])
        output = capsys.readouterr().out
    elif entry == "codex":
        output = setup.render_codex_config_result(codex, doctor_report=report, **options)
    elif entry == "mcp":
        output = setup.render_mcp_json_config_result(mcp, doctor_report=report, **options)
    else:
        output = setup.render_bootstrap_result(results=[("Codex", codex), ("MCP JSON", mcp)],
                                              session_id=session, doctor_reports=[report], **options)
    if entry != "bootstrap":
        servers = list(_servers(output))
        assert servers
        for actual in servers:
            assert actual["command"] == python and actual["args"] == args
    if entry != "markdown":
        commands = [shlex.split(b) for b in _blocks(output, "bash")]
        assert len(commands) == 1
        assert commands[0] == ["mnemotrace-agent-doctor", "--client", "none" if entry == "mcp" else "codex",
                               "--python", python, "--api-url", url, "--api-key-file", reference]
        assert common["path"] in output and common["backup"] in output
        assert "Synthetic reason: Bearer <REDACTED>" in output
        assert "PASS MCP mnemotrace: Bearer <REDACTED>" in output
        assert f"Bearer {key}" not in output
    if entry == "mcp":
        assert f"Configured server `mnemotrace-{key}`" in output
    if entry in ("markdown", "bootstrap"):
        assert "mnemotrace_recall" in output and "mnemotrace_close_session" in output
        assert session in output
    if entry == "markdown":
        for source in _blocks(output, "python"):
            tree = ast.parse(source)
            client = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                          and isinstance(node.func, ast.Name) and node.func.id == "OpenAI")
            assert next(arg.value.value for arg in client.keywords if arg.arg == "api_key") == "<API_KEY>"
    assert (codex, mcp, report) == before


@pytest.mark.parametrize("entry", ["codex", "mcp", "bootstrap"])
def test_local_namespace_is_separate_from_redaction_key(tmp_path, entry):
    namespace = "mnemotrace"
    server = {"command": "python", "args": ["-m", "mnemotrace.mcp", "--namespace", namespace]}
    result = {"action": "install", "changed": True, "path": "synthetic.config", "server": server,
              "block": '[mcp_servers.mnemotrace]\ncommand = "python"\nargs = ' + json.dumps(server["args"], ensure_ascii=False)}
    options = dict(api_url=None, api_key=None, namespace=namespace)
    if entry == "codex":
        output = setup.render_codex_config_result(result, **options)
    elif entry == "mcp":
        output = setup.render_mcp_json_config_result(result, **options)
    else:
        output = setup.render_bootstrap_result(results=[("Codex", result)], python_cmd="python",
                                              session_id="codex:synthetic:thread", **options)
    verify = shlex.split(_blocks(output, "bash")[0])
    assert verify[-2:] == ["--api-key", namespace]
    assert "--api-key-file" not in verify


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("entry", ["codex", "mcp", "bootstrap"])
def test_cli_output_matches_installed_config(tmp_path, monkeypatch, capsys, entry, dry_run):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex = tmp_path / "config.toml"
    mcp = tmp_path / ".mcp.json"
    command = ["--api-key", "mnemotrace", "--api-url", "http://mnemotrace.test",
               "--codex-config", str(codex), "--mcp-json", str(mcp)]
    if entry == "bootstrap":
        command += ["--bootstrap", "--no-doctor"]
    else:
        command += ["--install-codex" if entry == "codex" else "--install-mcp-json"]
    if dry_run:
        command.append("--dry-run")
    setup.main(command)
    output = capsys.readouterr().out
    verify = shlex.split(_blocks(output, "bash")[-1])
    assert verify[0] == "mnemotrace-agent-doctor"
    expected_args = ["-m", "mnemotrace.mcp", "--api-url", "http://mnemotrace.test",
                     "--api-key-file", verify[-1]]
    if entry != "bootstrap":
        assert next(_servers(output))["args"] == expected_args
    if dry_run:
        assert verify[-1] == "<private-credential-file>"
        assert not codex.exists() and not mcp.exists() and not (tmp_path / ".mnemotrace").exists()
    else:
        if entry in ("codex", "bootstrap"):
            assert _toml(codex.read_text(encoding="utf-8"))["mcp_servers"]["mnemotrace"]["args"] == expected_args
        if entry in ("mcp", "bootstrap"):
            actual = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["mnemotrace"]["args"]
            assert actual[:-1] == expected_args[:-1]
            assert read_key(actual[-1]) == "mnemotrace"
            if entry == "mcp":
                assert actual == expected_args


def test_pure_generators_have_no_key_input_and_keep_direct_output(tmp_path):
    for generator in (setup.mcp_args, setup.codex_toml_block, setup.render_markdown):
        assert "api_key" not in inspect.signature(generator).parameters
    reference = str(tmp_path / ".mnemotrace" / "credentials with spaces" / "synthetic.key")
    options = dict(api_url="http://mnemotrace.test", api_key_file=reference, namespace=None)
    expected = setup.mcp_args(**options)
    block = setup.codex_toml_block(**options, python_cmd="python")
    assert _toml(block)["mcp_servers"]["mnemotrace"]["args"] == expected
    for server in _servers(setup.render_markdown(**options)):
        assert server["args"] == expected
    assert not list(tmp_path.iterdir())


def test_bootstrap_preserves_generated_managed_policy():
    policy = setup.agents_policy_block("codex:mnemotrace:thread")
    output = setup.render_bootstrap_result(
        results=[("AGENTS.md policy", {"action": "install", "changed": True, "path": "AGENTS.md", "block": policy})],
        python_cmd="python", api_url="http://mnemotrace.test", api_key="mnemotrace",
        session_id="codex:mnemotrace:thread",
    )
    assert _blocks(output, "text")[0] == policy
