"""Verify the Python/MCP runtime and access to an explicitly selected space."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import os
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from .agent_config import ConfigReadError, load_codex_server
from .credentials import CredentialError, read_key, redact, resolve_key

CLIENTS = ("none", "codex", "claude-code", "cursor", "openai")


_PROBE = r"""
import asyncio
import json
from mnemotrace import Memory
from mnemotrace.mcp.server import mcp

async def main():
    tools = {tool.name for tool in await mcp.list_tools()}
    required = {"mnemotrace_remember_messages", "mnemotrace_recall",
                "mnemotrace_get_operation", "mnemotrace_retry_operation", "mnemotrace_flush_processing"}
    print(json.dumps(dict(missing_tools=sorted(required - tools), memory_entry=Memory.__name__)))

asyncio.run(main())
"""

_STDIO_PROBE = r"""
import asyncio
import json
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemotrace.mcp"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("mnemotrace_stats", {"response_format": "json"})
            payload = json.loads(result.content[0].text)
            configured = payload.get("llm_configured")
            print(json.dumps(dict(space_id=payload["space_id"], storage=payload["storage"],
                                  llm_configured=configured if isinstance(configured, bool) else None)))

asyncio.run(main())
"""


def _run(parts: list[str], timeout: float = 30.0, env=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(parts, capture_output=True, text=True, timeout=timeout, env=env, check=False)


def _check(
    checks: list[dict[str, Any]],
    name: str,
    status: str,
    detail: str,
    *,
    command: list[str] | None = None,
) -> None:
    checks.append({
        "name": name,
        "status": status,
        "detail": detail,
        "command": command or [],
    })


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _first_line(text: str) -> str:
    return ((text or "").strip().splitlines() or [""])[0]


def _arg_value(args: list[Any], flag: str) -> Any:
    try:
        i = args.index(flag)
    except ValueError:
        return None
    if i + 1 >= len(args):
        return None
    return args[i + 1]


def _check_mcp_server_config(
    name: str,
    server: Any,
    *,
    python_cmd: str,
    api_url: str | None,
    api_key: str | None,
    space_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    data = {
        "command_ok": False,
        "args_prefix_ok": False,
        "api_url_ok": api_url is None,
        "api_key_ok": api_url is None or api_key is None,
        "space_id_ok": False,
        "user_id_ok": api_url is not None,
    }
    if not isinstance(server, dict):
        failures.append("server entry is not an object")
        server = {}
    command = server.get("command")
    args = server.get("args")
    if command == python_cmd:
        data["command_ok"] = True
    else:
        failures.append("command does not match the requested Python executable")
    if isinstance(args, list) and args[:2] == ["-m", "mnemotrace.mcp"]:
        data["args_prefix_ok"] = True
    else:
        failures.append("args must start with ['-m', 'mnemotrace.mcp']")
    if api_url is not None:
        if isinstance(args, list) and _arg_value(args, "--api-url") == api_url:
            data["api_url_ok"] = True
        else:
            failures.append("args do not point at the requested MnemoTrace API URL")
        if api_key is not None:
            try:
                reference = _arg_value(args, "--api-key-file") if isinstance(args, list) else None
                data["api_key_ok"] = bool(reference and read_key(reference) == api_key)
            except CredentialError:
                data["api_key_ok"] = False
            if not data["api_key_ok"]:
                failures.append("private credential reference is missing, unsafe, or has a different identity")
    else:
        data['user_id_ok'] = isinstance(args, list) and _arg_value(args, '--user-id') == user_id and bool(user_id)
        if not data['user_id_ok']:
            failures.append('args do not include the requested local user ID')
    data['space_id_ok'] = isinstance(args, list) and _arg_value(args, '--space-id') == space_id and bool(space_id)
    if not data['space_id_ok']:
        failures.append('args do not include the requested space ID')
    ok = not failures
    return {
        "name": name,
        "status": "ok" if ok else "fail",
        "detail": (
            "MnemoTrace MCP config points at the requested Python/runtime."
            if ok else "MnemoTrace MCP config mismatch: " + "; ".join(failures)
        ),
        "command": [],
        "data": data,
    }


def _codex_config_probe(
    config_path: str,
    *,
    python_cmd: str,
    api_url: str | None,
    api_key: str | None,
    space_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    path = Path(config_path).expanduser()
    try:
        server = load_codex_server(path)
    except FileNotFoundError:
        return {
            "name": "Codex config",
            "status": "fail",
            "detail": f"Codex config not found: {path}",
            "command": [],
        }
    except ConfigReadError as exc:
        return {
            "name": "Codex config",
            "status": "fail",
            "detail": str(exc),
            "command": [],
        }
    except Exception:  # noqa: BLE001
        return {
            "name": "Codex config",
            "status": "fail",
            "detail": f"Could not read [mcp_servers.mnemotrace] from {path}.",
            "command": [],
        }
    check = _check_mcp_server_config(
        "Codex config",
        server,
        python_cmd=python_cmd,
        api_url=api_url,
        api_key=api_key,
        space_id=space_id,
        user_id=user_id,
    )
    check["data"] = {**check.get("data", {}), "path": str(path)}
    return check


def _mcp_json_config_probe(
    config_path: str,
    *,
    server_name: str,
    python_cmd: str,
    api_url: str | None,
    api_key: str | None,
    space_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    path = Path(config_path).expanduser()
    try:
        data = json.loads(path.read_text())
        server = (data.get("mcpServers") or {})[server_name]
    except FileNotFoundError:
        return {
            "name": "MCP JSON config",
            "status": "fail",
            "detail": f"MCP JSON config not found: {path}",
            "command": [],
        }
    except Exception:  # noqa: BLE001
        return {
            "name": "MCP JSON config",
            "status": "fail",
            "detail": f"Could not read mcpServers.{server_name} from {path}.",
            "command": [],
        }
    check = _check_mcp_server_config(
        "MCP JSON config",
        server,
        python_cmd=python_cmd,
        api_url=api_url,
        api_key=api_key,
        space_id=space_id,
        user_id=user_id,
    )
    check["data"] = {
        **check.get("data", {}),
        "path": str(path),
        "server_name": server_name,
    }
    return check


def _http_get_json(
    api_url: str,
    api_key: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    url = api_url.rstrip("/") + path + (f"?{query}" if query else "")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or "{}")


def _probe_result(name, status, detail, *, command=None, data=None):
    result = dict(name=name, status=status, detail=detail, command=command or [])
    if data is not None:
        result['data'] = data
    return result


def _probe_python(python_cmd, script, label, *, env=None):
    command = [python_cmd, '-c', f'<{label} probe>']
    try:
        process = _run([python_cmd, '-c', script], timeout=60, env=env)
        if process.returncode:
            return _probe_result(label, 'fail', f'Probe exited with status {process.returncode}', command=command)
        data = json.loads(process.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return _probe_result(label, 'fail', 'Probe could not run or returned invalid output', command=command)
    return _probe_result(label, 'ok', 'Probe completed', command=command, data=data)


def _python_probe(python_cmd):
    result = _probe_python(python_cmd, _PROBE, 'Python MCP runtime')
    if result.get('data', {}).get('missing_tools'):
        result.update(status='fail', detail='Required MCP operation tools are missing')
    return result


def _stdio_probe(python_cmd, *, space_id, user_id, api_url, api_key):
    env = {key: value for key, value in os.environ.items()
           if key not in {'MNEMOTRACE_API_URL', 'MNEMOTRACE_API_KEY', 'MNEMOTRACE_USER_ID'}}
    env['MNEMOTRACE_SPACE_ID'] = space_id
    if api_url:
        env.update(MNEMOTRACE_API_URL=api_url, MNEMOTRACE_API_KEY=api_key)
    else:
        env['MNEMOTRACE_USER_ID'] = user_id
    result = _probe_python(python_cmd, _STDIO_PROBE, 'MCP stdio space access', env=env)
    data = result.get('data', {})
    if result['status'] == 'ok' and (data.get('space_id'), data.get('storage')) != (space_id, 'postgres'):
        result.update(status='fail', detail='MCP did not reach the requested Postgres space')
    return result


def _remote_http_probe(api_url, api_key, space_id):
    label = 'Remote HTTP space access'
    try:
        result = _http_get_json(api_url.rstrip('/'), api_key, '/v1/stats', {'space_id': space_id})
    except urllib.error.HTTPError as exc:
        return _probe_result(label, 'fail', f'HTTP {exc.code}')
    except (OSError, ValueError):
        return _probe_result(label, 'fail', 'The server could not return space statistics')
    if not isinstance(result, dict) or (result.get('space_id'), result.get('storage')) != (space_id, 'postgres'):
        return _probe_result(label, 'fail', 'Unexpected space response')
    configured = result.get('llm_configured')
    return _probe_result(label, 'ok', 'Postgres space statistics',
                         data={'llm_configured': configured if isinstance(configured, bool) else None})


def _client_probe(client: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if client == "none":
        return checks
    if client == "codex":
        exe = shutil.which("codex")
        if not exe:
            _check(checks, "Codex CLI", "fail", "`codex` not found on PATH.")
            return checks
        version = _run([exe, "--version"], timeout=10.0)
        version_text = _first_line(version.stdout or version.stderr)
        _check(checks, "Codex CLI", "ok", f"{exe} {version_text}".strip(), command=[exe, "--version"])
        proc = _run([exe, "mcp", "--help"], timeout=10.0)
        status = "ok" if proc.returncode == 0 and "codex mcp" in (proc.stdout + proc.stderr).lower() else "fail"
        detail = "codex mcp help is available" if status == "ok" else _tail(proc.stdout or proc.stderr)
        _check(checks, "Codex MCP command", status, detail, command=[exe, "mcp", "--help"])
        return checks
    if client == "claude-code":
        exe = shutil.which("claude")
        if not exe:
            _check(checks, "Claude Code CLI", "fail", "`claude` not found on PATH.")
            return checks
        version = _run([exe, "--version"], timeout=10.0)
        version_text = _first_line(version.stdout or version.stderr)
        _check(checks, "Claude Code CLI", "ok", f"{exe} {version_text}".strip(), command=[exe, "--version"])
        proc = _run([exe, "mcp", "--help"], timeout=10.0)
        status = "ok" if proc.returncode == 0 and "mcp" in (proc.stdout + proc.stderr).lower() else "fail"
        detail = "claude mcp help is available" if status == "ok" else _tail(proc.stdout or proc.stderr)
        _check(checks, "Claude Code MCP command", status, detail,
               command=[exe, "mcp", "--help"])
        return checks
    if client == "cursor":
        _check(
            checks,
            "Cursor MCP config",
            "warn",
            "Cursor has no portable CLI smoke check here; use the generated MCP JSON and restart Cursor.",
        )
        return checks
    if client == "openai":
        _check(
            checks,
            "OpenAI-compatible client",
            "warn",
            "No local CLI required; pass --api-url to verify the HTTP server lifecycle.",
        )
    return checks


def _redact_report(report: dict[str, Any], key: str | None) -> dict[str, Any]:
    """Keep control fields intact even when a valid key equals a field or enum."""
    result = deepcopy(report)
    for field in ("python", "api_url"):
        if field in result:
            result[field] = redact(result[field], key)
    for check in result["checks"]:
        for field in ("detail", "command", "data"):
            if field in check:
                check[field] = redact(check[field], key)
    return result


def diagnose(
    client: str = "none",
    python_cmd: str = "python",
    api_url: str | None = None,
    api_key: str | None = None,
    api_key_file: str | None = None,
    codex_config: str | None = None,
    mcp_json: str | None = None,
    mcp_server_name: str = "mnemotrace",
    space_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    space_id = space_id or os.environ.get('MNEMOTRACE_SPACE_ID')
    user_id = None if api_url else user_id or os.environ.get('MNEMOTRACE_USER_ID')
    if api_url:
        api_key = resolve_key(key=api_key, key_file=api_key_file, required=True)
    python_check = _python_probe(python_cmd)
    checks = [python_check]
    model_probe = None
    if python_check['status'] == 'ok':
        if space_id and (api_url or user_id):
            model_probe = _stdio_probe(python_cmd, space_id=space_id, user_id=user_id,
                                       api_url=api_url, api_key=api_key)
            checks.append(model_probe)
            if api_url:
                model_probe = _remote_http_probe(api_url, api_key, space_id)
                checks.append(model_probe)
        else:
            checks.append(_probe_result('Memory space access', 'warn', 'Provide a space ID and local user ID or remote API'))
    if codex_config:
        checks.append(_codex_config_probe(
            codex_config,
            python_cmd=python_cmd,
            api_url=api_url,
            api_key=api_key,
            space_id=space_id,
            user_id=user_id,
        ))
    if mcp_json:
        checks.append(_mcp_json_config_probe(
            mcp_json,
            server_name=mcp_server_name,
            python_cmd=python_cmd,
            api_url=api_url,
            api_key=api_key,
            space_id=space_id,
            user_id=user_id,
        ))
    checks.extend(_client_probe(client))
    configured = None
    if model_probe is not None and model_probe['status'] == 'ok':
        value = model_probe.get('data', {}).get('llm_configured')
        if isinstance(value, bool):
            configured = value
    checks.append(_probe_result('LLM extraction', 'warn', {
        True: 'LLM configuration detected. Live extraction was not tested.',
        False: 'Automatic extraction requires MNEMOTRACE_LLM and provider credentials.',
        None: 'LLM configuration could not be determined from the target space.',
    }[configured], data={'llm_configured': configured, 'model_call_performed': False}))
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    return _redact_report({
        "ok": not failed,
        "client": client,
        "python": python_cmd,
        "api_url": api_url,
        "checks": checks,
        "summary": {
            "ok": len([c for c in checks if c["status"] == "ok"]),
            "warn": len(warned),
            "fail": len(failed),
        },
    }, api_key if api_url else None)


def render_report(report: dict[str, Any], *, api_key: str | None = None) -> str:
    if api_key is not None:
        report = _redact_report(report, api_key)
    lines = [
        "# MnemoTrace Agent Doctor",
        "",
        f"client: {report.get('client')}",
        f"python: {report.get('python')}",
    ]
    if report.get("api_url"):
        lines.append(f"api_url: {report.get('api_url')}")
    lines.append("")
    for check in report.get("checks", []):
        icon = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}.get(check.get("status"), "INFO")
        lines += [f"- {icon} {check.get('name')}: {check.get('detail', '')}"]
    summary = report.get("summary", {})
    lines += [
        "",
        f"summary: {summary.get('ok', 0)} passed, {summary.get('warn', 0)} warning(s), "
        f"{summary.get('fail', 0)} failure(s)",
    ]
    if report.get("ok"):
        lines += ["", "Cross-agent memory runtime is ready for this client."]
    else:
        lines += ["", "Fix the failing check(s), then run this doctor again before wiring the agent."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="mnemotrace-agent-doctor",
        description="Verify the Python/MCP runtime an agent will use for MnemoTrace memory.",
    )
    ap.add_argument("--client", choices=CLIENTS, default="none",
                    help="optional agent CLI to check in addition to the MCP lifecycle")
    ap.add_argument("--python", default="python",
                    help="Python executable that the agent will launch for `python -m mnemotrace.mcp`")
    ap.add_argument("--api-url", default=None,
                    help="optional MnemoTrace HTTP server to verify with a remote lifecycle smoke test")
    ap.add_argument("--space-id")
    ap.add_argument("--user-id")
    credentials = ap.add_mutually_exclusive_group()
    credentials.add_argument("--api-key", help="Explicit remote Bearer key")
    credentials.add_argument("--api-key-file", help="Private credential file for remote access")
    ap.add_argument("--codex-config", default=None,
                    help="optional Codex config.toml path to verify [mcp_servers.mnemotrace]")
    ap.add_argument("--mcp-json", default=None,
                    help="optional .mcp.json path to verify mcpServers.mnemotrace")
    ap.add_argument("--mcp-server-name", default="mnemotrace",
                    help="server name inside --mcp-json (default mnemotrace)")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        api_key = None
        if args.api_url or args.api_key is not None or args.api_key_file:
            api_key = resolve_key(key=args.api_key, key_file=args.api_key_file, required=bool(args.api_url))
    except CredentialError as exc:
        ap.error(str(exc))

    report = diagnose(
        client=args.client,
        python_cmd=args.python,
        api_url=args.api_url,
        api_key=api_key,
        codex_config=args.codex_config,
        mcp_json=args.mcp_json,
        mcp_server_name=args.mcp_server_name,
        space_id=args.space_id,
        user_id=args.user_id,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report), end="")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
