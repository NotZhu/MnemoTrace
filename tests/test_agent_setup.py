from __future__ import annotations

import json

import pytest

from mnemotrace.agent_setup import (
    POLICY_END_MARKER,
    POLICY_START_MARKER,
    agents_policy_block,
    bootstrap_main,
    codex_toml_block,
    install_agents_policy,
    install_codex_config,
    install_mcp_json_config,
    main,
    mcp_args,
    parse_bootstrap_targets,
    render_agents_policy_result,
    render_codex_config_result,
    render_markdown,
    uninstall_agents_policy,
    uninstall_codex_config,
    uninstall_mcp_json_config,
)
from mnemotrace.credentials import read_key


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "me")
    monkeypatch.setenv("MNEMOTRACE_SPACE_ID", "synthetic-space")


def test_hidden_install_reuses_reference_and_never_prints_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda *a: "synthetic-hidden-key")
    path = tmp_path / "config.toml"
    command = ["--install-codex", "--prompt-api-key", "--codex-config", str(path)]
    main(command)
    first = path.read_text()
    main(command)
    assert path.read_text() == first
    from mnemotrace.agent_config import load_codex_server
    args = load_codex_server(path)["args"]
    reference = args[args.index("--api-key-file") + 1]
    assert read_key(reference) == "synthetic-hidden-key"
    assert "synthetic-hidden-key" not in first + capsys.readouterr().out
    assert len(list((tmp_path / ".mnemotrace/credentials").glob("*.key"))) == 1


def test_missing_credentials_and_cancel_do_not_write(tmp_path, monkeypatch):
    monkeypatch.delenv("MNEMOTRACE_API_KEY")
    path = tmp_path / "config.toml"
    with pytest.raises(SystemExit):
        main(["--install-codex", "--codex-config", str(path)])
    monkeypatch.setattr("getpass.getpass", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(SystemExit):
        main(["--install-codex", "--prompt-api-key", "--codex-config", str(path)])
    assert not path.exists() and not (tmp_path / ".mnemotrace").exists()


def test_uninstall_does_not_read_unrelated_remote_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "synthetic\ninvalid")
    path = tmp_path / "config.toml"
    path.write_text('model = "synthetic"\n')
    main(["--uninstall-codex", "--codex-config", str(path)])
    assert path.read_text() == 'model = "synthetic"\n'


def test_dry_run_and_rotation_preserve_credential_lifecycle(tmp_path):
    path = tmp_path / "config.toml"
    preview = install_codex_config(config_path=str(path), api_key="synthetic-one", dry_run=True)
    assert not path.exists() and not (tmp_path / ".mnemotrace").exists()
    assert "synthetic-one" not in str(preview)
    first = install_codex_config(config_path=str(path), api_key="synthetic-one")
    second = install_codex_config(config_path=str(path), api_key="synthetic-two")
    assert first["api_key_file"] != second["api_key_file"]
    assert read_key(first["api_key_file"]) == "synthetic-one"
    assert read_key(second["api_key_file"]) == "synthetic-two"
    uninstall_codex_config(config_path=str(path))
    assert read_key(second["api_key_file"]) == "synthetic-two"


def test_private_file_install_and_all_recipe_outputs_are_safe(tmp_path):
    from mnemotrace.credentials import check_private, store_key
    reference = store_key("synthetic-recipe-key", directory=tmp_path / "credentials")
    path = tmp_path / ".mcp.json"
    path.write_text('{"mcpServers":{"other":{"args":["synthetic-other-secret"]}}}')
    result = install_mcp_json_config(config_path=str(path), api_key_file=reference)
    check_private(__import__("pathlib").Path(result["backup"]))
    assert read_key(result["api_key_file"]) == "synthetic-recipe-key"
    output = render_markdown(api_key_file=reference)
    assert "synthetic-recipe-key" not in output
    assert "--api-key-file" in output


def test_failed_private_write_preserves_installed_config(tmp_path, monkeypatch):
    from mnemotrace import credentials
    path = tmp_path / "config.toml"
    installed = install_codex_config(config_path=str(path), api_key="synthetic-one")
    before = path.read_bytes()
    monkeypatch.setattr(credentials.os, "replace", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(credentials.CredentialError):
        install_codex_config(config_path=str(path), api_key="synthetic-two")
    assert path.read_bytes() == before
    assert read_key(installed["api_key_file"]) == "synthetic-one"


def test_mcp_args_remote_and_local():
    assert mcp_args(api_url="http://localhost:8000", space_id="synthetic-space") == [
        "-m", "mnemotrace.mcp", "--space-id", "synthetic-space", "--api-url", "http://localhost:8000", "--api-key-file", "<private-credential-file>",
    ]
    assert mcp_args(api_url=None, space_id="work", user_id="local-me") == [
        "-m", "mnemotrace.mcp", "--space-id", "work", "--user-id", "local-me",
    ]


def test_parse_bootstrap_targets_is_stable_and_validates():
    assert parse_bootstrap_targets("mcp-json,codex,mcp-json") == ["codex", "mcp-json"]
    with pytest.raises(ValueError):
        parse_bootstrap_targets("codex,unknown")


def test_render_codex_setup_contains_lifecycle_and_config():
    out = render_markdown(
        "codex",
        api_url="http://localhost:8000",
        space_id="synthetic-space",
        session_id="codex:repo:thread",
        python_cmd="/opt/mnemotrace/bin/python",
    )

    assert "codex mcp add mnemotrace -- /opt/mnemotrace/bin/python" in out
    assert "[mcp_servers.mnemotrace]" in out
    assert 'command = "/opt/mnemotrace/bin/python"' in out
    assert "--api-url" in out and "--api-key" in out
    assert "mnemotrace_recall" in out
    assert "mnemotrace_remember" in out
    assert "mnemotrace_flush_processing" in out
    assert "mnemotrace_update_fact" in out
    assert "mnemotrace_delete_fact" in out
    assert "mnemotrace_export" in out
    assert "mnemotrace_set_focus" in out
    assert "codex:<project>:<thread>" in out


def test_render_local_claude_setup_uses_explicit_local_identity():
    out = render_markdown(
        "claude-code",
        api_url=None,
        space_id="local-space", user_id="local-me",
        session_id="claude-code:repo:thread",
    )

    assert "claude mcp add-json mnemotrace" in out
    assert "--space-id" in out and "--user-id" in out
    assert "local-me" in out
    assert "--api-key" not in out
    assert ".mcp.json" in out


def test_install_mcp_json_config_creates_file(tmp_path):
    config = tmp_path / ".mcp.json"

    result = install_mcp_json_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="sk-test",
        space_id="synthetic-space",
        python_cmd="/opt/mnemotrace/bin/python",
    )

    assert result["changed"] is True
    assert result["backup"] is None
    data = json.loads(config.read_text())
    assert data["mcpServers"]["mnemotrace"] == {
        "type": "stdio",
        "command": "/opt/mnemotrace/bin/python",
        "args": ["-m", "mnemotrace.mcp", "--space-id", "synthetic-space", "--api-url", "http://mnemotrace.test", "--api-key-file", result["api_key_file"]],
    }


def test_install_mcp_json_config_preserves_other_servers_and_backs_up(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "other": {"command": "other", "args": []},
            "mnemotrace": {"command": "old", "args": ["old"]},
        },
        "notes": {"keep": True},
    }))

    result = install_mcp_json_config(
        config_path=str(config),
        api_url=None,
        api_key=None,
        space_id="local-space", user_id="local-me",
        python_cmd="/opt/mnemotrace/bin/python",
    )

    assert result["changed"] is True
    assert result["backup"] is not None
    data = json.loads(config.read_text())
    assert data["notes"] == {"keep": True}
    assert data["mcpServers"]["other"] == {"command": "other", "args": []}
    assert data["mcpServers"]["mnemotrace"]["args"] == ["-m", "mnemotrace.mcp", "--space-id", "local-space", "--user-id", "local-me"]


def test_install_mcp_json_config_dry_run_does_not_write(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text('{"mcpServers": {}}\n')

    result = install_mcp_json_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="me",
        space_id="synthetic-space",
        python_cmd="python",
        dry_run=True,
    )

    assert result["changed"] is True
    assert result["dry_run"] is True
    assert result["backup"] is None
    assert config.read_text() == '{"mcpServers": {}}\n'


def test_install_agents_policy_creates_managed_block(tmp_path):
    agents = tmp_path / "AGENTS.md"

    result = install_agents_policy(agents_file=str(agents), session_id="codex:repo:thread")

    assert result["changed"] is True
    assert result["backup"] is None
    text = agents.read_text()
    assert POLICY_START_MARKER in text
    assert POLICY_END_MARKER in text
    assert "codex:repo:thread" in text
    assert text == agents_policy_block("codex:repo:thread")


def test_install_agents_policy_replaces_only_managed_block_and_backs_up(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Existing Rules\n\n"
        "Keep this project rule.\n\n"
        f"{POLICY_START_MARKER}\nold MnemoTrace instructions\n{POLICY_END_MARKER}\n\n"
        "Keep this footer.\n"
    )

    result = install_agents_policy(
        agents_file=str(agents),
        session_id="claude-code:repo:new-thread",
    )

    assert result["changed"] is True
    assert result["backup"] is not None
    backup = (tmp_path / result["backup"]).read_text()
    assert "old MnemoTrace instructions" in backup
    text = agents.read_text()
    assert "# Existing Rules" in text
    assert "Keep this project rule." in text
    assert "Keep this footer." in text
    assert "old MnemoTrace instructions" not in text
    assert "claude-code:repo:new-thread" in text
    assert text.count(POLICY_START_MARKER) == 1


def test_install_agents_policy_dry_run_does_not_write(tmp_path):
    agents = tmp_path / "AGENTS.md"

    result = install_agents_policy(
        agents_file=str(agents),
        session_id="codex:repo:thread",
        dry_run=True,
    )

    assert result["changed"] is True
    assert result["backup"] is None
    assert not agents.exists()
    report = render_agents_policy_result(result)
    assert "Would install" in report
    assert POLICY_START_MARKER in report


def test_uninstall_agents_policy_removes_only_managed_block_and_backs_up(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Existing Rules\n\n"
        "Keep this project rule.\n\n"
        f"{agents_policy_block('codex:repo:thread')}\n"
        "Keep this footer.\n"
    )

    result = uninstall_agents_policy(agents_file=str(agents))

    assert result["changed"] is True
    assert result["backup"] is not None
    backup = (tmp_path / result["backup"]).read_text()
    assert POLICY_START_MARKER in backup
    text = agents.read_text()
    assert POLICY_START_MARKER not in text
    assert POLICY_END_MARKER not in text
    assert "# Existing Rules" in text
    assert "Keep this project rule." in text
    assert "Keep this footer." in text


def test_uninstall_mcp_json_config_removes_only_mnemotrace_server(tmp_path):
    config = tmp_path / ".mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "mnemotrace": {"command": "python", "args": ["-m", "mnemotrace.mcp"]},
            "other": {"command": "other", "args": []},
        }
    }))

    result = uninstall_mcp_json_config(config_path=str(config))

    assert result["changed"] is True
    assert result["backup"] is not None
    data = json.loads(config.read_text())
    assert "mnemotrace" not in data["mcpServers"]
    assert data["mcpServers"]["other"] == {"command": "other", "args": []}


def test_render_openai_setup_contains_memory_extension_and_flush_call():
    out = render_markdown(
        "openai",
        api_url="http://mnemotrace.test",
        space_id="synthetic-space",
        session_id="app:product:conversation",
    )

    assert "OpenAI(base_url='http://mnemotrace.test/v1'" in out
    assert "'session_id': session_id" in out
    assert "'scope': 'auto'" in out
    assert "/v1/processing/flush" in out
    assert "Authorization: Bearer <API_KEY>" in out
    assert "sk-test" not in out


def test_install_codex_config_creates_file(tmp_path):
    config = tmp_path / "codex" / "config.toml"

    result = install_codex_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="sk-test",
        space_id="synthetic-space",
        python_cmd="/opt/mnemotrace/bin/python",
    )

    assert result["changed"] is True
    assert result["backup"] is None
    text = config.read_text()
    assert "[mcp_servers.mnemotrace]" in text
    assert 'command = "/opt/mnemotrace/bin/python"' in text
    assert '"--api-url", "http://mnemotrace.test", "--api-key-file"' in text
    assert "sk-test" not in text


def test_install_codex_config_replaces_mnemotrace_table_and_backs_up(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "other"\n\n'
        "[mcp_servers.mnemotrace]\n"
        'command = "old-python"\n'
        'args = ["old"]\n\n'
        "[profiles.default]\n"
        'approval_policy = "never"\n'
    )

    result = install_codex_config(
        config_path=str(config),
        api_url=None,
        api_key=None,
        space_id="local-space", user_id="local-me",
        python_cmd="/opt/mnemotrace/bin/python",
    )

    assert result["changed"] is True
    assert result["backup"] is not None
    assert "old-python" in (tmp_path / result["backup"]).read_text()
    text = config.read_text()
    assert 'model = "gpt-5"' in text
    assert "[mcp_servers.other]" in text
    assert "[profiles.default]" in text
    assert "old-python" not in text
    assert '"--user-id", "local-me"' in text


def test_install_codex_config_dry_run_does_not_write(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')

    result = install_codex_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="me",
        space_id="synthetic-space",
        python_cmd="python",
        dry_run=True,
    )

    assert result["changed"] is True
    assert result["dry_run"] is True
    assert result["backup"] is None
    assert config.read_text() == 'model = "gpt-5"\n'
    report = render_codex_config_result(result, python_cmd="python")
    assert "Would install" in report
    assert "[mcp_servers.mnemotrace]" in report


def test_render_codex_config_result_verify_command_matches_remote_mode(tmp_path):
    config = tmp_path / "config.toml"
    result = install_codex_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="sk-test",
        space_id="synthetic-space",
        python_cmd="/opt/mnemotrace/bin/python",
        dry_run=True,
    )

    report = render_codex_config_result(
        result,
        python_cmd="/opt/mnemotrace/bin/python",
        api_url="http://mnemotrace.test",
        api_key="sk-test",
    )

    assert (
        "mnemotrace-agent-doctor --client codex --python /opt/mnemotrace/bin/python "
        "--api-url http://mnemotrace.test --api-key-file"
    ) in report


def test_render_codex_config_result_verify_command_matches_local_scope(tmp_path):
    config = tmp_path / "config.toml"
    result = install_codex_config(
        config_path=str(config),
        api_url=None,
        api_key=None,
        space_id="local-space", user_id="local-me",
        python_cmd="/opt/mnemotrace/bin/python",
        dry_run=True,
    )

    report = render_codex_config_result(
        result,
        python_cmd="/opt/mnemotrace/bin/python",
        api_url=None,
        space_id="local-space", user_id="local-me",
    )

    assert (
        "mnemotrace-agent-doctor --client codex --python /opt/mnemotrace/bin/python "
        "--user-id local-me"
    ) in report
    assert '"--user-id", "local-me"' in report


def test_render_codex_config_result_includes_doctor_report(tmp_path):
    config = tmp_path / "config.toml"
    result = install_codex_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="me",
        space_id="synthetic-space",
        python_cmd="python",
    )
    report = {
        "ok": True,
        "client": "codex",
        "python": "python",
        "summary": {"ok": 4, "warn": 0, "fail": 0},
        "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
    }

    text = render_codex_config_result(result, python_cmd="python", doctor_report=report)

    assert "Doctor result:" in text
    assert "PASS MCP stdio server" in text
    assert "Cross-agent memory runtime is ready" in text


def test_main_install_codex_with_doctor_runs_doctor(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    calls = []

    def fake_doctor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "api_url": kwargs["api_url"],
            "summary": {"ok": 4, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_doctor)

    main([
        "--install-codex",
        "--doctor",
        "--codex-config",
        str(config),
        "--python",
        "/opt/mnemotrace/bin/python",
        "--api-url",
        "http://mnemotrace.test",
        "--api-key",
        "me",
    ])

    assert calls[0]["python_cmd"] == "/opt/mnemotrace/bin/python"
    assert calls[0]["api_url"] == "http://mnemotrace.test"
    assert calls[0]["api_key"] == "me"
    assert calls[0]["codex_config"] == str(config)
    out = capsys.readouterr().out
    assert "Installed:" in out
    assert "Doctor result:" in out
    assert "PASS MCP stdio server" in out


def test_main_install_codex_dry_run_skips_doctor(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"

    def fake_doctor(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_doctor)

    main([
        "--install-codex",
        "--doctor",
        "--dry-run",
        "--codex-config",
        str(config),
    ])

    assert not config.exists()
    out = capsys.readouterr().out
    assert "Would install" in out
    assert "Doctor skipped" in out


def test_main_install_codex_with_failed_doctor_exits_nonzero(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"

    def fake_doctor(**kwargs):
        return {
            "ok": False,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "summary": {"ok": 1, "warn": 0, "fail": 1},
            "checks": [{"name": "MCP stdio server", "status": "fail", "detail": "boom"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_doctor)

    with pytest.raises(SystemExit) as exc:
        main([
            "--install-codex",
            "--doctor",
            "--codex-config",
            str(config),
        ])

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL MCP stdio server" in out


def test_main_install_mcp_json_with_doctor_runs_doctor(tmp_path, monkeypatch, capsys):
    config = tmp_path / ".mcp.json"
    calls = []

    def fake_doctor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "client": kwargs["client"],
            "python": kwargs["python_cmd"],
            "api_url": kwargs["api_url"],
            "summary": {"ok": 3, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fake_doctor)

    main([
        "--install-mcp-json",
        "--doctor",
        "--doctor-client",
        "claude-code",
        "--mcp-json",
        str(config),
        "--python",
        "/opt/mnemotrace/bin/python",
        "--api-url",
        "http://mnemotrace.test",
        "--api-key",
        "me",
    ])

    assert calls[0]["client"] == "claude-code"
    assert calls[0]["python_cmd"] == "/opt/mnemotrace/bin/python"
    assert calls[0]["api_url"] == "http://mnemotrace.test"
    assert calls[0]["api_key"] == "me"
    assert calls[0]["mcp_json"] == str(config)
    out = capsys.readouterr().out
    assert "Installed:" in out
    assert "Doctor result:" in out
    assert "PASS MCP stdio server" in out


def test_main_install_mcp_json_accepts_doctor_client_none(tmp_path, monkeypatch, capsys):
    config = tmp_path / ".mcp.json"
    calls = []

    def fake_doctor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "client": kwargs["client"],
            "python": kwargs["python_cmd"],
            "summary": {"ok": 2, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fake_doctor)

    main([
        "--install-mcp-json",
        "--doctor",
        "--doctor-client",
        "none",
        "--mcp-json",
        str(config),
    ])

    assert calls[0]["client"] == "none"
    assert "Doctor result:" in capsys.readouterr().out


def test_main_install_mcp_json_dry_run_skips_doctor(tmp_path, monkeypatch, capsys):
    config = tmp_path / ".mcp.json"

    def fake_doctor(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fake_doctor)

    main([
        "--install-mcp-json",
        "--doctor",
        "--dry-run",
        "--mcp-json",
        str(config),
    ])

    assert not config.exists()
    out = capsys.readouterr().out
    assert "Would install" in out
    assert "Doctor skipped" in out


def test_main_bootstrap_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"

    def fail_doctor(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fail_doctor)
    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fail_doctor)

    main([
        "--bootstrap",
        "--dry-run",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
        "--session-id",
        "codex:repo:thread",
    ])

    assert not codex.exists()
    assert not mcp_json.exists()
    out = capsys.readouterr().out
    assert "# MnemoTrace Agent Bootstrap" in out
    assert "Codex: would install" in out
    assert "MCP JSON: would install" in out
    assert "Doctor skipped" in out
    assert "Suggested AGENTS.md note" in out


def test_main_bootstrap_install_policy_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"
    agents = tmp_path / "AGENTS.md"

    def fail_doctor(**kwargs):
        raise AssertionError(kwargs)

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fail_doctor)
    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fail_doctor)

    main([
        "--bootstrap",
        "--install-policy",
        "--dry-run",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
        "--agents-file",
        str(agents),
        "--session-id",
        "codex:repo:thread",
    ])

    assert not codex.exists()
    assert not mcp_json.exists()
    assert not agents.exists()
    out = capsys.readouterr().out
    assert "Codex: would install" in out
    assert "MCP JSON: would install" in out
    assert "AGENTS.md policy: would install" in out
    assert "Managed AGENTS.md policy block" in out
    assert "codex:repo:thread" in out


def test_main_bootstrap_installs_both_and_runs_codex_doctor(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"
    calls = []

    def fake_codex_doctor(**kwargs):
        calls.append(("codex", kwargs))
        return {
            "ok": True,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "summary": {"ok": 4, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_codex_doctor)

    main([
        "--bootstrap",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
        "--python",
        "/opt/mnemotrace/bin/python",
        "--api-url",
        "http://mnemotrace.test",
        "--api-key",
        "me",
    ])

    assert calls[0][0] == "codex"
    assert calls[0][1]["python_cmd"] == "/opt/mnemotrace/bin/python"
    assert calls[0][1]["api_url"] == "http://mnemotrace.test"
    assert calls[0][1]["api_key"] == "me"
    assert calls[0][1]["codex_config"] == str(codex)
    assert calls[0][1]["mcp_json"] == str(mcp_json)
    assert "[mcp_servers.mnemotrace]" in codex.read_text()
    assert json.loads(mcp_json.read_text())["mcpServers"]["mnemotrace"]["command"] == "/opt/mnemotrace/bin/python"
    out = capsys.readouterr().out
    assert "Codex: installed" in out
    assert "MCP JSON: installed" in out
    assert "Doctor result:" in out


def test_main_bootstrap_local_scope_is_installed_and_verified(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"
    calls = []

    def fake_codex_doctor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "summary": {"ok": 4, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_codex_doctor)

    main([
        "--bootstrap",
        "--local",
        "--space-id",
        "local-space",
        "--user-id",
        "local-me",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
        "--python",
        "/opt/mnemotrace/bin/python",
    ])

    assert calls[0]["api_url"] is None
    assert calls[0]["user_id"] == "local-me"
    assert '"--user-id", "local-me"' in codex.read_text()
    server = json.loads(mcp_json.read_text())["mcpServers"]["mnemotrace"]
    assert server["args"] == ["-m", "mnemotrace.mcp", "--space-id", "local-space", "--user-id", "local-me"]
    out = capsys.readouterr().out
    assert "--user-id local-me" in out
    assert "Codex: installed" in out
    assert "MCP JSON: installed" in out


def test_main_bootstrap_install_policy_no_doctor_writes_all_targets(tmp_path, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"
    agents = tmp_path / "AGENTS.md"

    main([
        "--bootstrap",
        "--install-policy",
        "--no-doctor",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
        "--agents-file",
        str(agents),
        "--python",
        "/opt/mnemotrace/bin/python",
        "--session-id",
        "codex:repo:thread",
    ])

    assert "[mcp_servers.mnemotrace]" in codex.read_text()
    assert json.loads(mcp_json.read_text())["mcpServers"]["mnemotrace"]["command"] == "/opt/mnemotrace/bin/python"
    text = agents.read_text()
    assert POLICY_START_MARKER in text
    assert "codex:repo:thread" in text
    out = capsys.readouterr().out
    assert "Codex: installed" in out
    assert "MCP JSON: installed" in out
    assert "AGENTS.md policy: installed" in out
    assert "Doctor skipped: --no-doctor was set" in out


def test_main_bootstrap_mcp_json_only_runs_mcp_doctor(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"
    mcp_json = tmp_path / ".mcp.json"
    calls = []

    def fake_mcp_doctor(**kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "client": kwargs["client"],
            "python": kwargs["python_cmd"],
            "summary": {"ok": 2, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_mcp_doctor", fake_mcp_doctor)

    main([
        "--bootstrap",
        "--bootstrap-targets",
        "mcp-json",
        "--doctor-client",
        "none",
        "--codex-config",
        str(codex),
        "--mcp-json",
        str(mcp_json),
    ])

    assert calls[0]["client"] == "none"
    assert calls[0]["mcp_json"] == str(mcp_json)
    assert not codex.exists()
    assert mcp_json.exists()
    assert "MCP JSON: installed" in capsys.readouterr().out


def test_main_bootstrap_failed_doctor_exits_nonzero(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"

    def fake_codex_doctor(**kwargs):
        return {
            "ok": False,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "summary": {"ok": 1, "warn": 0, "fail": 1},
            "checks": [{"name": "MCP stdio server", "status": "fail", "detail": "boom"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_codex_doctor)

    with pytest.raises(SystemExit) as exc:
        main(["--bootstrap", "--bootstrap-targets", "codex", "--codex-config", str(codex)])

    assert exc.value.code == 1
    assert "FAIL MCP stdio server" in capsys.readouterr().out


def test_main_install_and_uninstall_policy(tmp_path, capsys):
    agents = tmp_path / "AGENTS.md"

    main([
        "--install-policy",
        "--agents-file",
        str(agents),
        "--session-id",
        "codex:repo:thread",
    ])

    assert POLICY_START_MARKER in agents.read_text()
    out = capsys.readouterr().out
    assert "Installed:" in out

    main(["--uninstall-policy", "--agents-file", str(agents)])

    assert POLICY_START_MARKER not in agents.read_text()
    out = capsys.readouterr().out
    assert "Uninstalled:" in out


def test_bootstrap_main_adds_bootstrap_flag(tmp_path, monkeypatch, capsys):
    codex = tmp_path / "codex.toml"

    def fake_codex_doctor(**kwargs):
        return {
            "ok": True,
            "client": "codex",
            "python": kwargs["python_cmd"],
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "checks": [{"name": "MCP stdio server", "status": "ok", "detail": "ready"}],
        }

    monkeypatch.setattr("mnemotrace.agent_setup.run_codex_doctor", fake_codex_doctor)

    bootstrap_main(["--bootstrap-targets", "codex", "--codex-config", str(codex)])

    assert "[mcp_servers.mnemotrace]" in codex.read_text()
    assert "# MnemoTrace Agent Bootstrap" in capsys.readouterr().out


def test_bootstrap_main_help_uses_bootstrap_program_name(capsys):
    with pytest.raises(SystemExit) as exc:
        bootstrap_main(["--help"])

    assert exc.value.code == 0
    assert "usage: mnemotrace-agent-bootstrap" in capsys.readouterr().out


def test_uninstall_codex_config_removes_only_mnemotrace_table_and_backs_up(tmp_path):
    config = tmp_path / "config.toml"
    block = codex_toml_block(
        api_url="http://mnemotrace.test",
        space_id="synthetic-space",
        python_cmd="python",
    )
    config.write_text(
        'model = "gpt-5"\n\n'
        f"{block}\n"
        "[mcp_servers.other]\n"
        'command = "other"\n'
    )

    result = uninstall_codex_config(config_path=str(config))

    assert result["changed"] is True
    assert result["backup"] is not None
    assert "[mcp_servers.mnemotrace]" in (tmp_path / result["backup"]).read_text()
    text = config.read_text()
    assert "[mcp_servers.mnemotrace]" not in text
    assert "[mcp_servers.other]" in text
    assert 'model = "gpt-5"' in text


def test_codex_config_backups_do_not_overwrite_within_same_second(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5"\n')
    monkeypatch.setattr("mnemotrace.agent_setup.time.strftime", lambda fmt: "20260626235959")

    installed = install_codex_config(
        config_path=str(config),
        api_url="http://mnemotrace.test",
        api_key="me",
        space_id="synthetic-space",
        python_cmd="python",
    )
    uninstalled = uninstall_codex_config(config_path=str(config))

    assert installed["backup"] != uninstalled["backup"]
    assert installed["backup"].endswith("config.toml.mnemotrace-bak-20260626235959")
    assert uninstalled["backup"].endswith("config.toml.mnemotrace-bak-20260626235959-1")
    assert (tmp_path / installed["backup"]).exists()
    assert (tmp_path / uninstalled["backup"]).exists()
