"""自检跨 agent 的 忆痕 记忆配置。

setup 生成器只负责打印配置；本 doctor 验证的是 agent 实际会启动的运行时：
所选 Python 必须已安装 忆痕 + MCP，且 MCP 工具必须能在临时命名空间里跑通
status -> remember -> close -> report -> recall 全流程。它还会真实启动
Codex/Claude/Cursor 将要拉起的 `python -m mnemotrace.mcp` stdio server，
避免"配置看着没问题"与"运行时确实能用"出现偏差。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
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
import shutil
import sys
import tempfile

from mnemotrace.mcp import server as S
from mnemotrace.mcp.backends import LocalBackend
from mnemotrace.service import MemoryService

SOURCE_SESSION = "doctor:source:smoke"
TARGET_SESSION = "doctor:target:smoke"
USER = "doctor"
PHRASE = "cross-agent handoff through MCP lifecycle"

def text_of(res):
    seq = res[0] if isinstance(res, tuple) else res
    return seq[0].text

async def call(name, **args):
    return text_of(await S.mcp.call_tool(name, args))

async def main():
    data_dir = tempfile.mkdtemp(prefix="mnemotrace_agent_doctor_")
    svc = MemoryService(data_dir=data_dir, embedder_name="hashing", llm_name="")
    S.set_backend(LocalBackend(namespace=USER, service=svc))
    before = json.loads(await call(
        "mnemotrace_recall",
        query="MnemoTrace doctor smoke memory",
        max_chunks=0,
        session_id=TARGET_SESSION,
        response_format="json",
    ))
    remembered = await call(
        "mnemotrace_remember",
        content=f"Project rule: MnemoTrace doctor verifies {PHRASE}.",
        session_id=SOURCE_SESSION,
    )
    focus_set = json.loads(await call(
        "mnemotrace_set_focus",
        track=["MCP lifecycle"],
        mute=["temporary diagnostics"],
        response_format="json",
    ))
    focus = json.loads(await call("mnemotrace_get_focus", response_format="json"))
    status = json.loads(await call(
        "mnemotrace_agent_status",
        session_id=TARGET_SESSION,
        response_format="json",
    ))
    closed_source = json.loads(await call(
        "mnemotrace_close_session",
        session_id=SOURCE_SESSION,
        response_format="json",
    ))
    report = json.loads(await call(
        "mnemotrace_session_report",
        session_id=SOURCE_SESSION,
        response_format="json",
    ))
    sessions = json.loads(await call(
        "mnemotrace_list_sessions",
        q="doctor:source",
        response_format="json",
    ))
    exported = json.loads(await call(
        "mnemotrace_export",
        response_format="json",
    ))
    recalled = json.loads(await call(
        "mnemotrace_recall",
        query="What does MnemoTrace doctor verify across agents?",
        max_chunks=3,
        session_id=TARGET_SESSION,
        response_format="json",
    ))
    closed_target = json.loads(await call(
        "mnemotrace_close_session",
        session_id=TARGET_SESSION,
        response_format="json",
    ))
    context = recalled.get("context") or ""
    report_text = json.dumps(report, ensure_ascii=False)
    sessions_text = json.dumps(sessions, ensure_ascii=False)
    stats = svc.stats(USER).get("counts", {})
    S.set_backend(None)
    shutil.rmtree(data_dir, ignore_errors=True)
    print(json.dumps({
        "python": sys.executable,
        "before_empty": not bool((before.get("context") or "").strip()),
        "remembered": "Remembered" in remembered,
        "source_session": SOURCE_SESSION,
        "target_session": TARGET_SESSION,
        "focus_ok": (
            focus_set.get("ok") is True
            and focus.get("track") == ["MCP lifecycle"]
            and focus.get("mute") == ["temporary diagnostics"]
        ),
        "status_ok": (
            status.get("ok") is True
            and (status.get("session") or {}).get("id") == TARGET_SESSION
            and "facts_live" in (status.get("counts") or {})
        ),
        "report_ok": (
            report.get("ok") is True
            and report.get("session_id") == SOURCE_SESSION
            and int(report.get("facts_added") or 0) >= 1
            and PHRASE in report_text
        ),
        "sessions_ok": (
            sessions.get("ok") is True
            and any(
                row.get("id") == SOURCE_SESSION
                and int(row.get("facts_added") or 0) >= 1
                for row in (sessions.get("sessions") or [])
            )
            and PHRASE not in sessions_text
        ),
        "export_ok": (
            exported.get("mnemotrace_export_version") == 1
            and exported.get("include_sensitive") is False
            and len(exported.get("facts") or []) >= 1
        ),
        "recalled": PHRASE in context and SOURCE_SESSION in context,
        "closed": bool(closed_source.get("ok")) and bool(closed_target.get("ok")),
        "recalled_phrase": PHRASE in context,
        "recalled_source_session": SOURCE_SESSION in context,
        "facts_live": stats.get("facts_live", 0),
        "summaries": stats.get("summaries", 0),
    }, ensure_ascii=False))

asyncio.run(main())
"""


_STDIO_PROBE = r"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SOURCE_SESSION = "doctor:source:stdio"
TARGET_SESSION = "doctor:target:stdio"
NAMESPACE = "doctor-stdio"
PHRASE = "cross-agent handoff through the launched MCP server"

def text_of(result):
    return "\n".join(getattr(item, "text", "") for item in result.content).strip()

async def call(session, name, **args):
    return text_of(await session.call_tool(name, args))

async def main():
    data_dir = tempfile.mkdtemp(prefix="mnemotrace_agent_doctor_stdio_")
    try:
        env = os.environ.copy()
        env["MNEMOTRACE_DATA_DIR"] = data_dir
        env["MNEMOTRACE_EMBEDDER"] = "hashing"
        env["MNEMOTRACE_LLM"] = ""

        target_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemotrace.mcp", "--namespace", NAMESPACE],
            env=env,
        )
        source_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mnemotrace.mcp", "--namespace", NAMESPACE],
            env=env,
        )
        async with stdio_client(target_params) as (target_read, target_write):
            async with ClientSession(target_read, target_write) as target:
                await target.initialize()
                tools = {tool.name for tool in (await target.list_tools()).tools}
                required = {
                    "mnemotrace_remember",
                    "mnemotrace_recall",
                    "mnemotrace_close_session",
                    "mnemotrace_session_report",
                    "mnemotrace_list_sessions",
                    "mnemotrace_export",
                    "mnemotrace_agent_status",
                    "mnemotrace_get_focus",
                    "mnemotrace_set_focus",
                }
                status_before = json.loads(await call(
                    target,
                    "mnemotrace_agent_status",
                    session_id=TARGET_SESSION,
                    response_format="json",
                ))
                async with stdio_client(source_params) as (source_read, source_write):
                    async with ClientSession(source_read, source_write) as source:
                        await source.initialize()
                        remembered = await call(
                            source,
                            "mnemotrace_remember",
                            content=f"Project rule: MnemoTrace stdio doctor verifies {PHRASE}.",
                            session_id=SOURCE_SESSION,
                        )
                        focus = json.loads(await call(
                            source,
                            "mnemotrace_set_focus",
                            track=["launched MCP server"],
                            response_format="json",
                        ))
                        closed_source = json.loads(await call(
                            source,
                            "mnemotrace_close_session",
                            session_id=SOURCE_SESSION,
                            response_format="json",
                        ))
                        report = json.loads(await call(
                            source,
                            "mnemotrace_session_report",
                            session_id=SOURCE_SESSION,
                            response_format="json",
                        ))
                        sessions = json.loads(await call(
                            source,
                            "mnemotrace_list_sessions",
                            q="doctor:source",
                            response_format="json",
                        ))
                        exported = json.loads(await call(
                            source,
                            "mnemotrace_export",
                            response_format="json",
                        ))
                recalled = json.loads(await call(
                    target,
                    "mnemotrace_recall",
                    query="What does the stdio doctor verify across agents?",
                    max_chunks=3,
                    session_id=TARGET_SESSION,
                    response_format="json",
                ))
                closed_target = json.loads(await call(
                    target,
                    "mnemotrace_close_session",
                    session_id=TARGET_SESSION,
                    response_format="json",
                ))
                context = recalled.get("context") or ""
                report_text = json.dumps(report, ensure_ascii=False)
                sessions_text = json.dumps(sessions, ensure_ascii=False)

        result = {
            "python": sys.executable,
            "isolated_processes": True,
            "required_tools": sorted(required),
            "missing_tools": sorted(required - tools),
            "remembered": "Remembered" in remembered,
            "focused": focus.get("ok") is True,
            "target_preloaded": (
                status_before.get("ok") is True
                and (status_before.get("session") or {}).get("id") == TARGET_SESSION
                and (status_before.get("session") or {}).get("episodes") == 0
            ),
            "status_ok": (
                status_before.get("ok") is True
                and (status_before.get("session") or {}).get("id") == TARGET_SESSION
            ),
            "report_ok": (
                report.get("ok") is True
                and report.get("session_id") == SOURCE_SESSION
                and int(report.get("facts_added") or 0) >= 1
                and PHRASE in report_text
            ),
            "sessions_ok": (
                sessions.get("ok") is True
                and any(
                    row.get("id") == SOURCE_SESSION
                    and int(row.get("facts_added") or 0) >= 1
                    for row in (sessions.get("sessions") or [])
                )
                and PHRASE not in sessions_text
            ),
            "export_ok": (
                exported.get("mnemotrace_export_version") == 1
                and exported.get("include_sensitive") is False
                and len(exported.get("facts") or []) >= 1
            ),
            "source_session": SOURCE_SESSION,
            "target_session": TARGET_SESSION,
            "recalled": PHRASE in context and SOURCE_SESSION in context,
            "closed": bool(closed_source.get("ok")) and bool(closed_target.get("ok")),
            "recalled_phrase": PHRASE in context,
            "recalled_source_session": SOURCE_SESSION in context,
        }
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)

    print(json.dumps(result, ensure_ascii=False))

asyncio.run(main())
"""


def _run(parts: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(parts, capture_output=True, text=True, timeout=timeout, check=False)


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
) -> dict[str, Any]:
    failures: list[str] = []
    data = {
        "command_ok": False,
        "args_prefix_ok": False,
        "api_url_ok": api_url is None,
        "api_key_ok": api_url is None or api_key is None,
        "namespace_ok": api_url is not None or api_key is None,
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
    elif api_key is not None:
        if isinstance(args, list) and _arg_value(args, "--namespace") == api_key:
            data["namespace_ok"] = True
        else:
            failures.append("args do not include the requested local namespace")
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
    )
    check["data"] = {
        **check.get("data", {}),
        "path": str(path),
        "server_name": server_name,
    }
    return check


def _http_json(
    api_url: str,
    api_key: str,
    path: str,
    body: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    req = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or "{}")


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


def _python_probe(python_cmd: str) -> dict[str, Any]:
    cmd = [python_cmd, "-c", _PROBE]
    display_cmd = [python_cmd, "-c", "<mnemotrace-agent-doctor MCP lifecycle probe>"]
    try:
        proc = _run(cmd, timeout=60.0)
    except FileNotFoundError:
        return {
            "name": "Python MCP lifecycle",
            "status": "fail",
            "detail": f"Python executable not found: {python_cmd}",
            "command": display_cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": "Python MCP lifecycle",
            "status": "fail",
            "detail": "Timed out while running the local MCP lifecycle smoke test.",
            "command": display_cmd,
        }
    if proc.returncode != 0:
        detail = _tail(proc.stderr or proc.stdout)
        if "MCP server needs" in detail or "mcp" in detail.lower():
            detail += '\nInstall in that environment with: pip install "mnemotrace-memory[mcp]"'
        return {
            "name": "Python MCP lifecycle",
            "status": "fail",
            "detail": detail or f"Python exited with status {proc.returncode}.",
            "command": display_cmd,
        }
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "Python MCP lifecycle",
            "status": "fail",
            "detail": f"Could not parse probe output: {exc}: {_tail(proc.stdout)}",
            "command": display_cmd,
        }
    ok = (
        data.get("before_empty") is True
        and data.get("remembered") is True
        and data.get("focus_ok") is True
        and data.get("status_ok") is True
        and data.get("report_ok") is True
        and data.get("sessions_ok") is True
        and data.get("export_ok") is True
        and data.get("recalled") is True
        and data.get("closed") is True
        and int(data.get("facts_live") or 0) >= 1
    )
    return {
        "name": "Python MCP lifecycle",
        "status": "ok" if ok else "fail",
        "detail": (
            f"{data.get('python', python_cmd)} saved from `{data.get('source_session')}`, "
            f"reported saved facts, listed a content-free session index, exported a safe memory payload, recalled from "
            f"`{data.get('target_session')}`, checked agent status, focused, closed both sessions, "
            f"and persisted {data.get('facts_live', 0)} live fact(s), "
            f"{data.get('summaries', 0)} summary(ies)."
        ),
        "command": display_cmd,
        "data": data,
    }


def _stdio_probe(python_cmd: str) -> dict[str, Any]:
    cmd = [python_cmd, "-c", _STDIO_PROBE]
    display_cmd = [python_cmd, "-c", "<mnemotrace-agent-doctor stdio MCP probe>"]
    try:
        proc = _run(cmd, timeout=60.0)
    except FileNotFoundError:
        return {
            "name": "MCP stdio server",
            "status": "fail",
            "detail": f"Python executable not found: {python_cmd}",
            "command": display_cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "name": "MCP stdio server",
            "status": "fail",
            "detail": "Timed out while launching and calling `python -m mnemotrace.mcp` over stdio.",
            "command": display_cmd,
        }
    if proc.returncode != 0:
        detail = _tail((proc.stderr or "") + "\n" + (proc.stdout or ""))
        if "No module named 'mcp'" in detail or "mnemotrace-memory[mcp]" in detail:
            detail += '\nInstall in that environment with: pip install "mnemotrace-memory[mcp]"'
        return {
            "name": "MCP stdio server",
            "status": "fail",
            "detail": detail or f"stdio probe exited with status {proc.returncode}.",
            "command": display_cmd,
        }
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "MCP stdio server",
            "status": "fail",
            "detail": f"Could not parse stdio probe output: {exc}: {_tail(proc.stdout)}",
            "command": display_cmd,
        }
    ok = (
        data.get("isolated_processes") is True
        and data.get("missing_tools") == []
        and data.get("remembered") is True
        and data.get("focused") is True
        and data.get("target_preloaded") is True
        and data.get("status_ok") is True
        and data.get("report_ok") is True
        and data.get("sessions_ok") is True
        and data.get("export_ok") is True
        and data.get("recalled") is True
        and data.get("closed") is True
    )
    missing = data.get("missing_tools") or []
    detail = (
        f"{data.get('python', python_cmd)} launched two `python -m mnemotrace.mcp` stdio servers, "
        f"preloaded the target session, listed tools, saved from `{data.get('source_session')}`, "
        f"reported saved facts, listed a content-free session index, exported a safe memory payload, recalled from "
        f"`{data.get('target_session')}`, checked agent status, focused, and closed both sessions."
    ) if ok else (
        f"stdio server launched but did not complete the tool lifecycle "
        f"(isolated_processes={data.get('isolated_processes')}, missing_tools={missing}, "
        f"remembered={data.get('remembered')}, focused={data.get('focused')}, "
        f"target_preloaded={data.get('target_preloaded')}, status_ok={data.get('status_ok')}, "
        f"report_ok={data.get('report_ok')}, sessions_ok={data.get('sessions_ok')}, "
        f"export_ok={data.get('export_ok')}, "
        f"recalled={data.get('recalled')}, "
        f"closed={data.get('closed')})."
    )
    return {
        "name": "MCP stdio server",
        "status": "ok" if ok else "fail",
        "detail": detail,
        "command": display_cmd,
        "data": data,
    }


def _remote_http_probe(api_url: str, api_key: str) -> dict[str, Any]:
    base = api_url.rstrip("/")
    stamp = str(int(time.time() * 1000))
    source_session = f"doctor:source:{stamp}"
    target_session = f"doctor:target:{stamp}"
    phrase = "remote doctor verifies cross-agent handoff"
    command = [
        "HTTP",
        f"{base}/v1/remember",
        f"{base}/v1/recall",
        f"{base}/v1/sessions/close",
        f"{base}/v1/sessions/report",
        f"{base}/v1/sessions",
        f"{base}/v1/export",
    ]
    try:
        remembered = _http_json(base, api_key, "/v1/remember", {
            "content": f"Project decision: MnemoTrace {phrase}.",
            "session_id": source_session,
            "scope": "long",
        })
        closed_source = _http_json(base, api_key, "/v1/sessions/close", {
            "session_id": source_session,
            "summarize": True,
            "clear_working": True,
        })
        report = _http_get_json(base, api_key, "/v1/sessions/report", {
            "session_id": source_session,
        })
        sessions = _http_get_json(base, api_key, "/v1/sessions", {
            "q": source_session,
            "limit": 10,
            "offset": 0,
        })
        exported = _http_get_json(base, api_key, "/v1/export", {
            "include_sensitive": "false",
        })
        recalled = _http_json(base, api_key, "/v1/recall", {
            "query": "What does the MnemoTrace remote doctor verify?",
            "session_id": target_session,
            "n_chunks": 3,
        })
        closed_target = _http_json(base, api_key, "/v1/sessions/close", {
            "session_id": target_session,
            "summarize": True,
            "clear_working": True,
        })
    except urllib.error.HTTPError as exc:
        detail = "Request failed."
        if exc.code == 401:
            detail = "HTTP 401: the MnemoTrace server rejected this API key."
        else:
            detail = f"HTTP {exc.code}: {detail}"
        return {"name": "Remote HTTP lifecycle", "status": "fail", "detail": detail, "command": command}
    except urllib.error.URLError as exc:
        return {
            "name": "Remote HTTP lifecycle",
            "status": "fail",
            "detail": f"Could not reach MnemoTrace at {base}: {exc.reason}",
            "command": command,
        }
    except TimeoutError:
        return {
            "name": "Remote HTTP lifecycle",
            "status": "fail",
            "detail": f"Timed out reaching MnemoTrace at {base}.",
            "command": command,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "name": "Remote HTTP lifecycle",
            "status": "fail",
            "detail": f"{type(exc).__name__}: {exc}",
            "command": command,
        }

    context = recalled.get("context") or ""
    report_text = json.dumps(report, ensure_ascii=False)
    sessions_text = json.dumps(sessions, ensure_ascii=False)
    ok = (
        remembered.get("ok") is True
        and closed_source.get("ok") is True
        and closed_target.get("ok") is True
        and report.get("ok") is True
        and int(report.get("facts_added") or 0) >= 1
        and exported.get("mnemotrace_export_version") == 1
        and exported.get("include_sensitive") is False
        and len(exported.get("facts") or []) >= 1
        and phrase in report_text
        and sessions.get("ok") is True
        and any(
            row.get("id") == source_session
            and int(row.get("facts_added") or 0) >= 1
            for row in (sessions.get("sessions") or [])
        )
        and phrase not in sessions_text
        and phrase in context
        and source_session in context
    )
    return {
        "name": "Remote HTTP lifecycle",
        "status": "ok" if ok else "fail",
        "detail": (
            f"{base} saved from `{source_session}`, recalled from `{target_session}`, "
            f"reported saved facts, listed a content-free session index, exported a safe memory payload, "
            f"and preserved source-session provenance."
        ) if ok else (
            f"{base} responded, but recall did not prove cross-agent handoff "
            f"(remember_ok={remembered.get('ok')}, close_ok={closed_source.get('ok')}, "
            f"report_ok={report.get('ok')}, sessions_ok={sessions.get('ok')}, "
            f"export_version={exported.get('mnemotrace_export_version')}, "
            f"target_close_ok={closed_target.get('ok')})."
        ),
        "command": command,
        "data": {
            "source_session": source_session,
            "target_session": target_session,
            "remembered": remembered.get("ok") is True,
            "closed_source": closed_source.get("ok") is True,
            "closed_target": closed_target.get("ok") is True,
            "reported": report.get("ok") is True,
            "report_facts": int(report.get("facts_added") or 0),
            "sessions_ok": sessions.get("ok") is True,
            "sessions_count": len(sessions.get("sessions") or []),
            "export_ok": (
                exported.get("mnemotrace_export_version") == 1
                and exported.get("include_sensitive") is False
            ),
            "recalled_phrase": phrase in context,
            "recalled_source_session": source_session in context,
        },
    }


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
) -> dict[str, Any]:
    if api_url:
        api_key = resolve_key(key=api_key, key_file=api_key_file)
    else:
        api_key = api_key or "me"
    python_check = _python_probe(python_cmd)
    checks = [python_check]
    if python_check["status"] == "ok":
        checks.append(_stdio_probe(python_cmd))
    if api_url:
        checks.append(_remote_http_probe(api_url, api_key))
    if codex_config:
        checks.append(_codex_config_probe(
            codex_config,
            python_cmd=python_cmd,
            api_url=api_url,
            api_key=api_key,
        ))
    if mcp_json:
        checks.append(_mcp_json_config_probe(
            mcp_json,
            server_name=mcp_server_name,
            python_cmd=python_cmd,
            api_url=api_url,
            api_key=api_key,
        ))
    checks.extend(_client_probe(client))
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
    credentials = ap.add_mutually_exclusive_group()
    credentials.add_argument("--api-key", help="Explicit Bearer key or expected local namespace")
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
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report), end="")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
