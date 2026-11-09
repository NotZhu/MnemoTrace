"""生成或安装跨 agent 使用 忆痕 记忆的配置片段。

默认模式刻意保持只读：只打印可直接复制的配置方案，绝不改动用户的
Claude/Codex/Cursor 配置。显式的 install/uninstall 标志可以先备份再管理 Codex 的
config.toml，因为 Codex 是第一个能用 doctor 做端到端验证的适配器。

配置和命令保留功能参数的精确值；只有自由诊断内容在进入模板前脱敏。
禁止对成品全文替换 Key，否则合法参数与固定标识重名也会被破坏。
"""
from __future__ import annotations

import argparse
import os
import json
import shlex
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

from .agent_config import ConfigReadError, load_codex_server
from .credentials import (
    CredentialError,
    credential_path,
    private_write,
    read_config,
    read_key,
    redact,
    resolve_key,
    store_key,
)

CLIENTS = ("all", "claude-code", "codex", "cursor", "openai")
DOCTOR_CLIENTS = ("none", "claude-code", "codex", "cursor", "openai")
BOOTSTRAP_TARGETS = ("codex", "mcp-json")
POLICY_START_MARKER = "<!-- MNEMOTRACE MEMORY START -->"
POLICY_END_MARKER = "<!-- MNEMOTRACE MEMORY END -->"
PRIVATE_CREDENTIAL_FILE = "<private-credential-file>"


def _credential_file_argument(path: str | None) -> str:
    if not path or path == PRIVATE_CREDENTIAL_FILE:
        return PRIVATE_CREDENTIAL_FILE
    return str(credential_path(path))


def mcp_args(*, api_url: str | None, space_id: str | None, api_key_file: str | None = None, user_id: str | None = None) -> list[str]:
    args = ["-m", "mnemotrace.mcp", "--space-id", space_id or "<SPACE_ID>"]
    if api_url:
        args += ["--api-url", api_url]
        args += ["--api-key-file", _credential_file_argument(api_key_file)]
    else:
        args += ["--user-id", user_id or "<USER_ID>"]
    return args


def _stdio_server(args: list[str], python_cmd: str) -> dict:
    return {"type": "stdio", "command": python_cmd, "args": args}


def _json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _shell(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def codex_config_path(path: str | None = None) -> Path:
    return Path(path).expanduser() if path else Path.home() / ".codex" / "config.toml"


def mcp_json_path(path: str | None = None) -> Path:
    return Path(path).expanduser() if path else Path.cwd() / ".mcp.json"


def agents_file_path(path: str | None = None) -> Path:
    return Path(path).expanduser() if path else Path.cwd() / "AGENTS.md"


def _toml_string(value: str) -> str:
    """Encode one TOML basic string, without JSON's surrogate-pair escapes."""
    escapes = {'"': '\\"', '\\': '\\\\', '\b': '\\b', '\t': '\\t', '\n': '\\n', '\f': '\\f', '\r': '\\r'}
    encoded = []
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CredentialError("Codex config strings must contain valid Unicode scalar values.")
        if char in escapes:
            encoded.append(escapes[char])
        elif code < 0x20 or code == 0x7F:
            encoded.append(f"\\u{code:04X}")
        else:
            encoded.append(char)
    return '"' + "".join(encoded) + '"'


def codex_toml_block(*, api_url: str | None,
                     space_id: str | None, python_cmd: str, api_key_file: str | None = None, user_id: str | None = None) -> str:
    args = mcp_args(api_url=api_url, space_id=space_id, api_key_file=api_key_file, user_id=user_id)
    toml_args = ", ".join(_toml_string(a) for a in args)
    return "\n".join([
        "[mcp_servers.mnemotrace]",
        f"command = {_toml_string(python_cmd)}",
        f"args = [{toml_args}]",
        "",
    ])


def _toml_header(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _find_toml_table(lines: list[str], header: str) -> tuple[int, int] | None:
    start = None
    for i, line in enumerate(lines):
        if _toml_header(line) == header:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = _toml_header(lines[j])
        if stripped.startswith("[") and stripped.endswith("]"):
            end = j
            break
    return start, end


def _replace_toml_table(text: str, header: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    span = _find_toml_table(lines, header)
    if span is not None:
        start, end = span
        return "".join(lines[:start]) + block + "".join(lines[end:])
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix.strip():
        prefix += "\n"
    return prefix + block


def _remove_toml_table(text: str, header: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    span = _find_toml_table(lines, header)
    if span is None:
        return text, False
    start, end = span
    return "".join(lines[:start] + lines[end:]), True


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S")
    candidate = path.with_name(f"{path.name}.mnemotrace-bak-{stamp}")
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = path.with_name(f"{path.name}.mnemotrace-bak-{stamp}-{i}")
        if not candidate.exists():
            return candidate
        i += 1


def agents_policy_block(session_id: str) -> str:
    return "\n".join([
        POLICY_START_MARKER,
        _bootstrap_policy(session_id),
        POLICY_END_MARKER,
        "",
    ])


def _managed_policy_span(text: str) -> tuple[int, int] | None:
    start = text.find(POLICY_START_MARKER)
    end = text.find(POLICY_END_MARKER)
    if start == -1 and end == -1:
        return None
    if start == -1 or end == -1 or end < start:
        raise ValueError("AGENTS.md contains malformed MnemoTrace memory policy markers")
    end += len(POLICY_END_MARKER)
    return start, end


def _replace_managed_policy_block(text: str, block: str) -> str:
    span = _managed_policy_span(text)
    if span is not None:
        start, end = span
        return text[:start] + block + text[end:]
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix.strip():
        prefix += "\n"
    return prefix + block


def _remove_managed_policy_block(text: str) -> tuple[str, bool]:
    span = _managed_policy_span(text)
    if span is None:
        return text, False
    start, end = span
    after = text[:start] + text[end:]
    while "\n\n\n" in after:
        after = after.replace("\n\n\n", "\n\n")
    return after, True


def install_agents_policy(
    *,
    agents_file: str | None = None,
    session_id: str = "codex:super-memory:thread-123",
    dry_run: bool = False,
) -> dict:
    path = agents_file_path(agents_file)
    before = path.read_text() if path.exists() else ""
    block = agents_policy_block(session_id)
    after = _replace_managed_policy_block(before, block)
    changed = after != before
    backup = None
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = _backup_path(path)
            shutil.copy2(path, backup)
        path.write_text(after)
    return {
        "ok": True,
        "action": "install",
        "changed": changed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "block": block,
    }


def uninstall_agents_policy(*, agents_file: str | None = None, dry_run: bool = False) -> dict:
    path = agents_file_path(agents_file)
    if not path.exists():
        return {
            "ok": True,
            "action": "uninstall",
            "changed": False,
            "dry_run": dry_run,
            "path": str(path),
            "backup": None,
        }
    before = path.read_text()
    after, removed = _remove_managed_policy_block(before)
    backup = None
    if removed and not dry_run:
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        path.write_text(after)
    return {
        "ok": True,
        "action": "uninstall",
        "changed": removed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
    }


def install_codex_config(
    *,
    config_path: str | None = None,
    api_url: str | None = "http://localhost:8000",
    api_key: str | None = None,
    api_key_file: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    python_cmd: str = "python",
    dry_run: bool = False,
) -> dict:
    path = codex_config_path(config_path)
    before = read_config(path)
    api_key_file = _credential_reference(path, "codex", api_url, api_key, api_key_file, dry_run, "mnemotrace")
    block = codex_toml_block(
        api_url=api_url,
        api_key_file=api_key_file,
        space_id=space_id,
        user_id=user_id,
        python_cmd=python_cmd,
    )
    after = _replace_toml_table(before, "[mcp_servers.mnemotrace]", block)
    changed = after != before
    backup = None
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = _backup_path(path)
            private_write(backup, path.read_bytes())
        private_write(path, after.encode("utf-8"))
    return {
        "ok": True,
        "api_key_file": api_key_file,
        "action": "install",
        "changed": changed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "block": block,
    }


def uninstall_codex_config(*, config_path: str | None = None, dry_run: bool = False) -> dict:
    path = codex_config_path(config_path)
    if not path.exists():
        return {
            "ok": True,
            "action": "uninstall",
            "changed": False,
            "dry_run": dry_run,
            "path": str(path),
            "backup": None,
        }
    before = read_config(path)
    after, removed = _remove_toml_table(before, "[mcp_servers.mnemotrace]")
    backup = None
    if removed and not dry_run:
        backup = _backup_path(path)
        private_write(backup, path.read_bytes())
        private_write(path, after.encode("utf-8"))
    return {
        "ok": True,
        "action": "uninstall",
        "changed": removed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
    }


def _load_json_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(read_config(path))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _credential_reference(path: Path, kind: str, api_url: str | None, api_key: str | None,
                          api_key_file: str | None, dry_run: bool, server_name: str) -> str | None:
    if not api_url:
        return None
    if api_key_file:
        value = read_key(api_key_file)
        if api_key is not None and value != api_key:
            raise CredentialError("Credential file does not match the requested identity.")
        return str(credential_path(api_key_file))
    key = resolve_key(key=api_key)
    if dry_run:
        return "<private-credential-file>"
    previous = None
    if path.exists():
        # Only inspect the current target, never discover/migrate historical client configs.
        try:
            if kind == "codex":
                server = load_codex_server(path)
            else:
                server = (_load_json_config(path).get("mcpServers") or {}).get(server_name, {})
        except (KeyError, ConfigReadError):
            # Reuse is optional; recognized file references are still checked by store_key.
            server = {}
        args = server.get("args", []) if isinstance(server, dict) else []
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            args = []

        def argument(flag):
            index = args.index(flag) + 1 if flag in args else len(args)
            return args[index] if index < len(args) else None

        if argument("--api-url") == api_url:
            previous = argument("--api-key-file")
    return store_key(key, previous=previous)


def install_mcp_json_config(
    *,
    config_path: str | None = None,
    api_url: str | None = "http://localhost:8000",
    api_key: str | None = None,
    api_key_file: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    python_cmd: str = "python",
    server_name: str = "mnemotrace",
    dry_run: bool = False,
) -> dict:
    path = mcp_json_path(config_path)
    before = read_config(path)
    data = _load_json_config(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise ValueError(f"{path} field `mcpServers` must be a JSON object")
    data["mcpServers"] = servers
    api_key_file = _credential_reference(path, "json", api_url, api_key, api_key_file, dry_run, server_name)
    server = _stdio_server(
        mcp_args(api_url=api_url, space_id=space_id, api_key_file=api_key_file, user_id=user_id),
        python_cmd,
    )
    servers[server_name] = server
    after = _json(data) + "\n"
    changed = after != before
    backup = None
    if changed and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = _backup_path(path)
            private_write(backup, path.read_bytes())
        private_write(path, after.encode("utf-8"))
    return {
        "ok": True,
        "api_key_file": api_key_file,
        "action": "install",
        "changed": changed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "server_name": server_name,
        "server": server,
        "config": data,
    }


def uninstall_mcp_json_config(
    *,
    config_path: str | None = None,
    server_name: str = "mnemotrace",
    dry_run: bool = False,
) -> dict:
    path = mcp_json_path(config_path)
    if not path.exists():
        return {
            "ok": True,
            "action": "uninstall",
            "changed": False,
            "dry_run": dry_run,
            "path": str(path),
            "backup": None,
            "server_name": server_name,
        }
    data = _load_json_config(path)
    servers = data.get("mcpServers")
    removed = isinstance(servers, dict) and server_name in servers
    if removed:
        del servers[server_name]
    after = _json(data) + "\n"
    backup = None
    if removed and not dry_run:
        backup = _backup_path(path)
        private_write(backup, path.read_bytes())
        private_write(path, after.encode("utf-8"))
    return {
        "ok": True,
        "action": "uninstall",
        "changed": removed,
        "dry_run": dry_run,
        "path": str(path),
        "backup": str(backup) if backup else None,
        "server_name": server_name,
        "config": data,
    }


def run_codex_doctor(
    *,
    python_cmd: str = "python",
    api_url: str | None = None,
    api_key: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    codex_config: str | None = None,
    mcp_json: str | None = None,
    mcp_server_name: str = "mnemotrace",
) -> dict:
    from .agent_doctor import diagnose

    return diagnose(
        client="codex",
        python_cmd=python_cmd,
        api_url=api_url,
        api_key=api_key,
        codex_config=codex_config,
        mcp_json=mcp_json,
        mcp_server_name=mcp_server_name,
        space_id=space_id,
        user_id=user_id,
    )


def run_mcp_doctor(
    *,
    client: str = "none",
    python_cmd: str = "python",
    api_url: str | None = None,
    api_key: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    mcp_json: str | None = None,
    mcp_server_name: str = "mnemotrace",
) -> dict:
    from .agent_doctor import diagnose

    return diagnose(
        client=client,
        python_cmd=python_cmd,
        api_url=api_url,
        api_key=api_key,
        mcp_json=mcp_json,
        mcp_server_name=mcp_server_name,
        space_id=space_id,
        user_id=user_id,
    )


def _doctor_verify_command(
    *,
    client: str,
    python_cmd: str,
    api_url: str | None,
    space_id: str | None,
    user_id: str | None = None,
    api_key_file: str | None = None,
) -> list[str]:
    verify = ["mnemotrace-agent-doctor", "--client", client, "--python", python_cmd]
    if api_url:
        verify += ["--api-url", api_url]
        verify += ["--api-key-file", _credential_file_argument(api_key_file)]
    elif user_id:
        verify += ["--user-id", user_id]
    if space_id:
        verify += ["--space-id", space_id]
    return verify


def render_codex_config_result(
    result: dict,
    *,
    python_cmd: str = "python",
    api_url: str | None = None,
    api_key: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    doctor_report: dict | None = None,
    doctor_skipped: str | None = None,
) -> str:
    api_key_file = result.get("api_key_file")
    action = "install" if result.get("action") == "install" else "uninstall"
    verb = {
        ("install", True): "Would install",
        ("install", False): "Installed",
        ("uninstall", True): "Would uninstall",
        ("uninstall", False): "Uninstalled",
    }[(action, bool(result.get("dry_run")))]
    if not result.get("changed"):
        verb = "No changes needed"
    lines = [
        "# MnemoTrace Codex Config",
        "",
        f"{verb}: `{result.get('path')}`",
    ]
    if result.get("backup"):
        lines.append(f"Backup: `{result['backup']}`")
    if action == "install" and result.get("block"):
        lines += ["", "Configured block:", "", "```toml", result["block"].rstrip(), "```"]
    if action == "install":
        verify = _doctor_verify_command(
            client="codex",
            python_cmd=python_cmd,
            api_url=api_url,
            space_id=space_id,
            user_id=user_id,
            api_key_file=api_key_file,
        )
        lines += [
            "",
            "Verify with:",
            "",
            "```bash",
            _shell(verify),
            "```",
        ]
    if doctor_skipped:
        lines += ["", f"Doctor skipped: {redact(doctor_skipped, api_key if api_url else None)}"]
    if doctor_report is not None:
        from .agent_doctor import render_report

        lines += ["", "Doctor result:", "", render_report(doctor_report, api_key=api_key if api_url else None).rstrip()]
    return "\n".join(lines).rstrip() + "\n"


def render_mcp_json_config_result(
    result: dict,
    *,
    python_cmd: str = "python",
    doctor_client: str = "none",
    api_url: str | None = None,
    api_key: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    doctor_report: dict | None = None,
    doctor_skipped: str | None = None,
) -> str:
    api_key_file = result.get("api_key_file")
    action = "install" if result.get("action") == "install" else "uninstall"
    verb = {
        ("install", True): "Would install",
        ("install", False): "Installed",
        ("uninstall", True): "Would uninstall",
        ("uninstall", False): "Uninstalled",
    }[(action, bool(result.get("dry_run")))]
    if not result.get("changed"):
        verb = "No changes needed"
    lines = [
        "# MnemoTrace MCP JSON Config",
        "",
        f"{verb}: `{result.get('path')}`",
    ]
    if result.get("backup"):
        lines.append(f"Backup: `{result['backup']}`")
    if action == "install" and result.get("server"):
        lines += [
            "",
            f"Configured server `{result.get('server_name', 'mnemotrace')}`:",
            "",
            "```json",
            _json(result["server"]),
            "```",
        ]
        verify = _doctor_verify_command(
            client=doctor_client,
            python_cmd=python_cmd,
            api_url=api_url,
            space_id=space_id,
            user_id=user_id,
            api_key_file=api_key_file,
        )
        lines += ["", "Verify with:", "", "```bash", _shell(verify), "```"]
    if doctor_skipped:
        lines += ["", f"Doctor skipped: {redact(doctor_skipped, api_key if api_url else None)}"]
    if doctor_report is not None:
        from .agent_doctor import render_report

        lines += ["", "Doctor result:", "", render_report(doctor_report, api_key=api_key if api_url else None).rstrip()]
    return "\n".join(lines).rstrip() + "\n"


def render_agents_policy_result(result: dict) -> str:
    action = "install" if result.get("action") == "install" else "uninstall"
    verb = {
        ("install", True): "Would install",
        ("install", False): "Installed",
        ("uninstall", True): "Would uninstall",
        ("uninstall", False): "Uninstalled",
    }[(action, bool(result.get("dry_run")))]
    if not result.get("changed"):
        verb = "No changes needed"
    lines = [
        "# MnemoTrace AGENTS.md Policy",
        "",
        f"{verb}: `{result.get('path')}`",
    ]
    if result.get("backup"):
        lines.append(f"Backup: `{result['backup']}`")
    if action == "install" and result.get("block"):
        lines += ["", "Managed policy block:", "", "```text", result["block"].rstrip(), "```"]
    return "\n".join(lines).rstrip() + "\n"


def _policy(agent: str, session_id: str) -> str:
    return f"""Use MnemoTrace as the user's cross-agent long-term memory.
At session start or when debugging memory wiring, call mnemotrace_agent_status with session_id `{session_id}`.
Before tasks that may depend on prior user/project context, call mnemotrace_recall with session_id `{session_id}`.
When the user states a durable preference, project rule, decision, or reusable fact, call mnemotrace_remember with the same session_id and scope `long` or `auto`.
Keep task progress and temporary execution state in the agent’s task tools or files.
When the user asks to correct or delete a memory, call mnemotrace_list_facts to find the fact id, then call mnemotrace_update_fact or mnemotrace_delete_fact with confirm=true.
When the user asks to export their memory, call mnemotrace_export(response_format="json"); set include_sensitive=true only for an explicit private export.
When the user asks you to pay more attention to or suppress a class of memories, call mnemotrace_set_focus; use mnemotrace_get_focus to inspect the current policy.
When the user asks what was saved from a thread, call mnemotrace_session_report for that session.
When the thread ends or you switch tasks, call mnemotrace_flush_processing with the same session_id.
Do not store secrets, credentials, large logs, or short-lived state as durable memory.
For this client, use session IDs shaped like `{agent}:<project>:<thread>`."""


def _bootstrap_policy(session_id: str) -> str:
    return f"""Use MnemoTrace as the user's cross-agent long-term memory.
At session start or when debugging memory wiring, call mnemotrace_agent_status with a session_id like `{session_id}`.
Before tasks that may depend on prior user/project context, call mnemotrace_recall with a session_id like `{session_id}`.
When the user states a durable preference, project rule, decision, or reusable fact, call mnemotrace_remember with the same session_id and scope `long` or `auto`.
Keep task progress and temporary execution state in the agent’s task tools or files.
When the user asks to correct or delete a memory, call mnemotrace_list_facts to find the fact id, then call mnemotrace_update_fact or mnemotrace_delete_fact with confirm=true.
When the user asks to export their memory, call mnemotrace_export(response_format="json"); set include_sensitive=true only for an explicit private export.
When the user asks you to pay more attention to or suppress a class of memories, call mnemotrace_set_focus; use mnemotrace_get_focus to inspect the current policy.
When the user asks what was saved from a thread, call mnemotrace_session_report for that session.
When the thread ends or you switch tasks, call mnemotrace_flush_processing with the same session_id.
Do not store secrets, credentials, large logs, or short-lived state as durable memory.
Use session IDs shaped like `<agent>:<project>:<thread>`, for example `codex:<repo>:<thread>` or `claude-code:<repo>:<thread>`."""


def parse_bootstrap_targets(value: str) -> list[str]:
    targets = [p.strip() for p in value.split(",") if p.strip()]
    if not targets:
        raise ValueError("bootstrap targets cannot be empty")
    invalid = [t for t in targets if t not in BOOTSTRAP_TARGETS]
    if invalid:
        raise ValueError(f"unknown bootstrap target(s): {', '.join(invalid)}")
    # 按 BOOTSTRAP_TARGETS 的固定顺序去重，重复传参不会导致重复执行。
    return [t for t in BOOTSTRAP_TARGETS if t in set(targets)]


def _action_verb(result: dict) -> str:
    action = "install" if result.get("action") == "install" else "uninstall"
    verb = {
        ("install", True): "would install",
        ("install", False): "installed",
        ("uninstall", True): "would uninstall",
        ("uninstall", False): "uninstalled",
    }[(action, bool(result.get("dry_run")))]
    return "no changes needed" if not result.get("changed") else verb


def render_bootstrap_result(
    *,
    results: list[tuple[str, dict]],
    python_cmd: str,
    api_url: str | None,
    api_key: str | None,
    session_id: str,
    doctor_reports: list[dict] | None = None,
    doctor_skipped: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
) -> str:
    api_key_file = next((r.get("api_key_file") for _, r in results if r.get("api_key_file")), None)
    lines = ["# MnemoTrace Agent Bootstrap", ""]
    policy_result = None
    for label, result in results:
        line = f"- {label}: {_action_verb(result)} `{result.get('path')}`"
        if result.get("backup"):
            line += f" (backup `{result['backup']}`)"
        lines.append(line)
        if label == "AGENTS.md policy":
            policy_result = result
    if doctor_skipped:
        lines += ["", f"Doctor skipped: {redact(doctor_skipped, api_key if api_url else None)}"]
    for report in doctor_reports or []:
        from .agent_doctor import render_report

        lines += ["", "Doctor result:", "", render_report(report, api_key=api_key if api_url else None).rstrip()]
    verify = _doctor_verify_command(
        client="codex" if any(label == "Codex" for label, _ in results) else "none",
        python_cmd=python_cmd,
        api_url=api_url,
        space_id=space_id,
        user_id=user_id,
        api_key_file=api_key_file,
    )
    lines += ["", "Verify later with:", "", "```bash", _shell(verify), "```", ""]
    if policy_result is not None:
        lines += [
            "Managed AGENTS.md policy block:",
            "",
            "```text",
            policy_result.get("block", agents_policy_block(session_id)).rstrip(),
            "```",
        ]
    else:
        lines += [
            "Suggested AGENTS.md note:",
            "",
            "```text",
            _bootstrap_policy(session_id),
            "```",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(
    client: str = "all",
    *,
    api_url: str | None = "http://localhost:8000",
    api_key_file: str | None = None,
    space_id: str | None = None,
    user_id: str | None = None,
    session_id: str = "codex:super-memory:thread-123",
    python_cmd: str = "python",
) -> str:
    args = mcp_args(api_url=api_url, space_id=space_id, api_key_file=api_key_file, user_id=user_id)
    server = _stdio_server(args, python_cmd)
    out: list[str] = [
        "# MnemoTrace Agent Setup",
        "",
        "Shared lifecycle:",
        "",
        "```text",
        "recall before work -> remember durable information -> flush session processing",
        "```",
        "",
    ]

    if client in {"all", "claude-code"}:
        add_json = _json(server)
        out += [
            "## Claude Code",
            "",
            "CLI:",
            "",
            "```bash",
            f"claude mcp add-json mnemotrace {_shell([add_json])}",
            "```",
            "",
            "Project `.mcp.json`:",
            "",
            "```json",
            _json({"mcpServers": {"mnemotrace": server}}),
            "```",
            "",
            "Suggested instruction:",
            "",
            "```text",
            _policy("claude-code", session_id.replace("codex:", "claude-code:", 1)),
            "```",
            "",
        ]

    if client in {"all", "codex"}:
        cli = ["codex", "mcp", "add", "mnemotrace", "--", python_cmd, *args]
        out += [
            "## Codex",
            "",
            "CLI:",
            "",
            "```bash",
            _shell(cli),
            "```",
            "",
            "`~/.codex/config.toml`:",
            "",
            "```toml",
            codex_toml_block(
                api_url=api_url,
                api_key_file=api_key_file,
                space_id=space_id,
                user_id=user_id,
                python_cmd=python_cmd,
            ).rstrip(),
            "```",
            "",
            "Suggested AGENTS.md note:",
            "",
            "```text",
            _policy("codex", session_id),
            "```",
            "",
        ]

    if client in {"all", "cursor"}:
        out += [
            "## Cursor / Generic MCP Client",
            "",
            "MCP JSON:",
            "",
            "```json",
            _json({"mcpServers": {"mnemotrace": server}}),
            "```",
            "",
            "Suggested instruction:",
            "",
            "```text",
            _policy("cursor", session_id.replace("codex:", "cursor:", 1)),
            "```",
            "",
        ]

    if client in {"all", "openai"}:
        key = "<API_KEY>"
        url = (api_url or "http://localhost:8000").rstrip("/") + "/v1"
        out += [
            "## OpenAI-Compatible Apps",
            "",
            "Python:",
            "",
            "```python",
            "from openai import OpenAI",
            "",
            f"session_id = {session_id!r}",
            f"client = OpenAI(base_url={url!r}, api_key={key!r})",
            "client.chat.completions.create(",
            "    model='mnemotrace',",
            "    messages=[{'role': 'user', 'content': 'Continue the work.'}],",
            "    extra_body={'memory': {",
            f"        'space_id': {space_id or '<SPACE_ID>'!r},",
            "        'session_id': session_id,",
            "        'recall': True,",
            "        'remember': True,",
            "        'scope': 'auto',",
            "    }},",
            ")",
            "```",
            "",
            "Finish core processing:",
            "",
            "```bash",
            _shell([
                "curl", "-s", "-X", "POST", (api_url or "http://localhost:8000").rstrip("/") + "/v1/processing/flush",
                "-H", f"Authorization: Bearer {key}",
                "-H", "Content-Type: application/json",
                "-d", json.dumps({"session_id": session_id, "space_id": space_id or "<SPACE_ID>"}),
            ]),
            "```",
            "",
        ]
    return "\n".join(out).rstrip() + "\n"


def _main(argv: list[str] | None = None, *, prog: str = "mnemotrace-agent-setup") -> None:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Print or explicitly install config snippets for using MnemoTrace as cross-agent memory.",
    )
    ap.add_argument("--client", choices=CLIENTS, default="all", help="which adapter recipe to print")
    ap.add_argument("--api-url", default="http://localhost:8000",
                    help="MnemoTrace HTTP server for remote MCP/OpenAI-compatible mode")
    credentials = ap.add_mutually_exclusive_group()
    credentials.add_argument("--api-key", help="Explicit Bearer key (prefer hidden input or private file)")
    credentials.add_argument("--api-key-file", help="Private credential file")
    credentials.add_argument("--prompt-api-key", action="store_true", help="Read API key without echo")
    ap.add_argument("--local", action="store_true", help="use local MCP storage instead of an HTTP server")
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--user-id", default=os.environ.get("MNEMOTRACE_USER_ID"))
    ap.add_argument("--session-id", default="codex:super-memory:thread-123",
                    help="example session id to show in generated snippets")
    ap.add_argument("--python", default="python",
                    help="Python executable that has mnemotrace-memory[mcp] installed")
    ap.add_argument("--bootstrap", action="store_true",
                    help="install Codex + .mcp.json configs, run doctor, and print AGENTS.md guidance")
    ap.add_argument("--bootstrap-targets", default="codex,mcp-json",
                    help="comma-separated bootstrap targets: codex,mcp-json (default both)")
    ap.add_argument("--install-codex", action="store_true",
                    help="write/update [mcp_servers.mnemotrace] in Codex config.toml (backs up first)")
    ap.add_argument("--uninstall-codex", action="store_true",
                    help="remove [mcp_servers.mnemotrace] from Codex config.toml (backs up first)")
    ap.add_argument("--codex-config", default=None,
                    help="Codex config path (default ~/.codex/config.toml)")
    ap.add_argument("--install-mcp-json", action="store_true",
                    help="write/update an `mnemotrace` server in a project .mcp.json (backs up first)")
    ap.add_argument("--uninstall-mcp-json", action="store_true",
                    help="remove the `mnemotrace` server from a project .mcp.json (backs up first)")
    ap.add_argument("--mcp-json", default=None,
                    help="MCP JSON path (default ./.mcp.json)")
    ap.add_argument("--mcp-server-name", default="mnemotrace",
                    help="server name to manage inside mcpServers (default mnemotrace)")
    ap.add_argument("--install-policy", action="store_true",
                    help="write/update a managed MnemoTrace memory block in AGENTS.md (backs up first)")
    ap.add_argument("--uninstall-policy", action="store_true",
                    help="remove only the managed MnemoTrace memory block from AGENTS.md (backs up first)")
    ap.add_argument("--agents-file", default=None,
                    help="AGENTS.md path for --install-policy/--uninstall-policy (default ./AGENTS.md)")
    ap.add_argument("--doctor-client", choices=DOCTOR_CLIENTS, default="none",
                    help="client to pass to --doctor for --install-mcp-json (e.g. claude-code or cursor)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change without writing files")
    ap.add_argument("--doctor", action="store_true",
                    help="after installing config, run mnemotrace-agent-doctor against the same Python")
    ap.add_argument("--no-doctor", action="store_true",
                    help="with --bootstrap, skip the automatic doctor check")
    args = ap.parse_args(argv)
    actions = [
        args.bootstrap,
        args.install_codex,
        args.uninstall_codex,
        args.install_mcp_json,
        args.uninstall_mcp_json,
        args.uninstall_policy,
    ]
    if sum(bool(a) for a in actions) > 1:
        ap.error("choose only one install/uninstall action")
    if args.install_policy and args.uninstall_policy:
        ap.error("choose only one of --install-policy or --uninstall-policy")
    if args.install_policy and not args.bootstrap and any([
        args.install_codex,
        args.uninstall_codex,
        args.install_mcp_json,
        args.uninstall_mcp_json,
    ]):
        ap.error("--install-policy can be used by itself or with --bootstrap")
    if args.doctor and not (args.bootstrap or args.install_codex or args.install_mcp_json):
        ap.error("--doctor requires --bootstrap, --install-codex, or --install-mcp-json")
    if args.no_doctor and not args.bootstrap:
        ap.error("--no-doctor requires --bootstrap")

    api_url = None if args.local else args.api_url
    needs_key = bool(args.bootstrap or args.install_codex or args.install_mcp_json)
    if args.local and (args.api_key is not None or args.api_key_file or args.prompt_api_key):
        ap.error("--local cannot be combined with remote credentials")
    try:
        explicit_input = args.api_key is not None or args.api_key_file is not None or args.prompt_api_key
        api_key = None
        if not args.local and (needs_key or explicit_input):
            api_key = resolve_key(
                key=args.api_key, key_file=args.api_key_file,
                prompt=args.prompt_api_key, required=needs_key,
            )
    except CredentialError as exc:
        ap.error(str(exc))
    space_id = args.space_id
    user_id = args.user_id if args.local else None
    if (args.bootstrap or args.install_codex or args.install_mcp_json) and not space_id:
        ap.error('Select --space-id before installing an agent configuration')
    if args.local and (args.bootstrap or args.install_codex or args.install_mcp_json) and not user_id:
        ap.error('Local agent configuration requires --user-id')
    if args.bootstrap:
        try:
            targets = parse_bootstrap_targets(args.bootstrap_targets)
        except ValueError as exc:
            ap.error(str(exc))
        results: list[tuple[str, dict]] = []
        if "codex" in targets:
            results.append((
                "Codex",
                install_codex_config(
                    config_path=args.codex_config,
                    api_url=api_url,
                    api_key=api_key,
                    api_key_file=args.api_key_file,
                    space_id=space_id,
                    user_id=user_id,
                    python_cmd=args.python,
                    dry_run=args.dry_run,
                ),
            ))
        if "mcp-json" in targets:
            results.append((
                "MCP JSON",
                install_mcp_json_config(
                    config_path=args.mcp_json,
                    api_url=api_url,
                    api_key=api_key,
                    api_key_file=args.api_key_file,
                    space_id=space_id,
                    user_id=user_id,
                    python_cmd=args.python,
                    server_name=args.mcp_server_name,
                    dry_run=args.dry_run,
                ),
            ))
        if args.install_policy:
            results.append((
                "AGENTS.md policy",
                install_agents_policy(
                    agents_file=args.agents_file,
                    session_id=args.session_id,
                    dry_run=args.dry_run,
                ),
            ))
        doctor_reports: list[dict] = []
        doctor_skipped = None
        if args.dry_run:
            doctor_skipped = "dry-run does not claim installed agent configs are ready"
        elif args.no_doctor:
            doctor_skipped = "--no-doctor was set"
        elif "codex" in targets:
            codex_path = next((r["path"] for label, r in results if label == "Codex"), None)
            mcp_json_path_ = next((r["path"] for label, r in results if label == "MCP JSON"), None)
            doctor_reports.append(run_codex_doctor(
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                codex_config=codex_path,
                mcp_json=mcp_json_path_,
                mcp_server_name=args.mcp_server_name,
            ))
            if args.doctor_client not in {"none", "codex"}:
                doctor_reports.append(run_mcp_doctor(
                    client=args.doctor_client,
                    python_cmd=args.python,
                    api_url=api_url,
                    api_key=api_key,
                    space_id=space_id,
                    user_id=user_id,
                    mcp_json=mcp_json_path_,
                    mcp_server_name=args.mcp_server_name,
                ))
        elif "mcp-json" in targets:
            mcp_json_path_ = next((r["path"] for label, r in results if label == "MCP JSON"), None)
            doctor_reports.append(run_mcp_doctor(
                client=args.doctor_client,
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                mcp_json=mcp_json_path_,
                mcp_server_name=args.mcp_server_name,
            ))
        print(
            render_bootstrap_result(
                results=results,
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                session_id=args.session_id,
                doctor_reports=doctor_reports,
                doctor_skipped=doctor_skipped,
            ),
            end="",
        )
        if any(not r.get("ok") for r in doctor_reports):
            raise SystemExit(1)
        return
    if args.install_codex:
        result = install_codex_config(
            config_path=args.codex_config,
            api_url=api_url,
            api_key=api_key,
            api_key_file=args.api_key_file,
            space_id=space_id,
            user_id=user_id,
            python_cmd=args.python,
            dry_run=args.dry_run,
        )
        doctor_report = None
        doctor_skipped = None
        if args.doctor and args.dry_run:
            doctor_skipped = "dry-run does not claim the installed Codex config is ready"
        elif args.doctor:
            doctor_report = run_codex_doctor(
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                codex_config=result["path"],
            )
        print(
            render_codex_config_result(
                result,
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                doctor_report=doctor_report,
                doctor_skipped=doctor_skipped,
            ),
            end="",
        )
        if doctor_report is not None and not doctor_report.get("ok"):
            raise SystemExit(1)
        return
    if args.uninstall_codex:
        result = uninstall_codex_config(config_path=args.codex_config, dry_run=args.dry_run)
        print(render_codex_config_result(result, python_cmd=args.python), end="")
        return
    if args.install_mcp_json:
        result = install_mcp_json_config(
            config_path=args.mcp_json,
            api_url=api_url,
            api_key=api_key,
            api_key_file=args.api_key_file,
            space_id=space_id,
            user_id=user_id,
            python_cmd=args.python,
            server_name=args.mcp_server_name,
            dry_run=args.dry_run,
        )
        doctor_report = None
        doctor_skipped = None
        if args.doctor and args.dry_run:
            doctor_skipped = "dry-run does not claim the installed MCP JSON config is ready"
        elif args.doctor:
            doctor_report = run_mcp_doctor(
                client=args.doctor_client,
                python_cmd=args.python,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                mcp_json=result["path"],
                mcp_server_name=args.mcp_server_name,
            )
        print(
            render_mcp_json_config_result(
                result,
                python_cmd=args.python,
                doctor_client=args.doctor_client,
                api_url=api_url,
                api_key=api_key,
                space_id=space_id,
                user_id=user_id,
                doctor_report=doctor_report,
                doctor_skipped=doctor_skipped,
            ),
            end="",
        )
        if doctor_report is not None and not doctor_report.get("ok"):
            raise SystemExit(1)
        return
    if args.uninstall_mcp_json:
        result = uninstall_mcp_json_config(
            config_path=args.mcp_json,
            server_name=args.mcp_server_name,
            dry_run=args.dry_run,
        )
        print(render_mcp_json_config_result(result, python_cmd=args.python), end="")
        return
    if args.install_policy:
        result = install_agents_policy(
            agents_file=args.agents_file,
            session_id=args.session_id,
            dry_run=args.dry_run,
        )
        print(render_agents_policy_result(result), end="")
        return
    if args.uninstall_policy:
        result = uninstall_agents_policy(agents_file=args.agents_file, dry_run=args.dry_run)
        print(render_agents_policy_result(result), end="")
        return
    print(render_markdown(
        args.client,
        api_url=api_url,
        api_key_file=args.api_key_file,
        space_id=space_id,
        user_id=user_id,
        session_id=args.session_id,
        python_cmd=args.python,
    ), end="")


def main(argv: list[str] | None = None, *, prog: str = "mnemotrace-agent-setup") -> None:
    try:
        _main(argv, prog=prog)
    except CredentialError as exc:
        raise SystemExit(str(exc)) from None


def bootstrap_main(argv: list[str] | None = None) -> None:
    import sys

    main(["--bootstrap", *(sys.argv[1:] if argv is None else argv)], prog="mnemotrace-agent-bootstrap")


if __name__ == "__main__":
    main()
