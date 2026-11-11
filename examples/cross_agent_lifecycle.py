"""演示单个 agent 会话中面向用户的 忆痕 完整生命周期。

先配置 MNEMOTRACE_LLM 与对应凭据。按 docs/reference/configuration.md 的 Postgres 数据库基础章节初始化数据库、创建账号和空间，
设置 MNEMOTRACE_DATABASE_URL 为运行角色连接串，再启动服务：

    python -m uvicorn mnemotrace.server.app:app --port 8000

然后模拟一个 Codex 线程：

    python examples/cross_agent_lifecycle.py \
        --base http://localhost:8000 \
        --key "$MNEMOTRACE_API_KEY" --space-id "<SPACE_ID>" \
        --agent codex \
        --project super-memory \
        --thread demo

本示例展示 agent 客户端应遵循的产品闭环：
  1. agent_status: 不含内容的状态/接线检查
  2. recall: 开始工作前拉取相关的用户/项目记忆
  3. remember: 保存可跨会话复用的事实与决策
  4. 整理当前会话的核心处理
  5. session_report: 审计本次会话保存了什么
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


Call = Callable[[str, Optional[dict], str], dict]
DURABLE_MEMORIES = [
    "Project rule: benchmark claims require committed raw logs before public copy changes.",
    "Project preference: keep MnemoTrace memory integrations automatic; users should not manually decide every remember call.",
]
VERIFY_QUERY = "What project rule should I follow for benchmark claims?"
VERIFY_PHRASE = "committed raw logs"


def http_call(base: str, key: str, space_id: str) -> Call:
    from urllib.parse import urlencode

    base = base.rstrip("/")

    def call(path: str, body: dict | None = None, method: str = "POST") -> dict:
        if method == "GET":
            path += ("&" if "?" in path else "?") + urlencode({"space_id": space_id})
        else:
            body = {**(body or {}), "space_id": space_id}
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SystemExit(f"MnemoTrace returned HTTP {exc.code} for {path}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Could not reach MnemoTrace at {base}: {exc}") from exc

    return call


def _status_path(session_id: str) -> str:
    return "/v1/agent/status?" + urlencode({"session_id": session_id})


def _report_path(session_id: str, include_sensitive: bool = False) -> str:
    return "/v1/sessions/report?" + urlencode({
        "session_id": session_id,
        "include_sensitive": "true" if include_sensitive else "false",
    })


def exercise_lifecycle(
    call: Call,
    *,
    namespace: str,
    agent: str = "codex",
    project: str = "super-memory",
    thread: str = "demo",
    no_flush: bool = False,
    output: Callable[[str], None] = print,
) -> dict:
    session_id = f"{agent}:{project}:{thread}"

    output(f"namespace: {namespace}")
    output(f"session_id: {session_id}")

    output("\n== 1. Agent status before work ==")
    status_before = call(_status_path(session_id), None, "GET")
    session_before = status_before.get("session") or {}
    output(
        "agent_status: "
        f"user={status_before.get('user')} "
        f"episodes={session_before.get('episodes', 0)}"
    )

    output("\n== 2. Recall before work ==")
    recalled_before = call("/v1/recall", {
        "query": f"Current rules and preferences for project {project}",
        "n_chunks": 3,
    }, "POST")
    context_before = (recalled_before.get("context") or "").strip()
    output(context_before[:600] if context_before else "(no relevant memory yet)")

    output("\n== 3. Remember facts and decisions ==")
    remembered: list[dict] = []
    for text in DURABLE_MEMORIES:
        out = call("/v1/remember", {
            "content": text,
            "session_id": session_id,
            "scope": "long",
        }, "POST")
        remembered.append(out)
        output(f"- long ({out.get('extracted', 0)} extracted): {text}")


    output("\n== 4. Recall the saved durable memory ==")
    recalled_after = call("/v1/recall", {
        "query": VERIFY_QUERY,
        "n_chunks": 3,
    }, "POST")
    context_after = (recalled_after.get("context") or "").strip()
    output(recalled_after.get("answer") or context_after[:1000] or "(no relevant memory found)")

    status_after_write = call(_status_path(session_id), None, "GET")
    session_after_write = status_after_write.get("session") or {}
    output(
        "agent_status_after_write: "
        f"episodes={session_after_write.get('episodes', 0)}"
    )

    flushed = None
    if not no_flush:
        output("\n== 5. Flush processing for the agent session ==")
        flushed = call("/v1/processing/flush", {
            "session_id": session_id,
            "include_digests": True,
        }, "POST")
        output(json.dumps(flushed, ensure_ascii=False, indent=2))

    output("\n== 6. Session report: what this session saved ==")
    report = call(_report_path(session_id), None, "GET")
    facts = report.get("facts") or []
    output(
        "session_report: "
        f"episodes={report.get('episodes', 0)} "
        f"facts={len(facts)} "
        f"redacted={report.get('redacted', 0)}"
    )
    for fact in facts[:8]:
        output(f"- {fact['text']}")

    found = VERIFY_PHRASE in context_after or any(
        VERIFY_PHRASE in fact["text"]
        for fact in facts
    )
    if found:
        output("\nPASS: lifecycle saved durable memory and the session report audits it.")

    return {
        "session_id": session_id,
        "status_before": status_before,
        "recalled_before": recalled_before,
        "context_before": context_before,
        "remembered": remembered,
        "recalled_after": recalled_after,
        "context_after": context_after,
        "status_after_write": status_after_write,
        "flushed": flushed,
        "report": report,
        "found": found,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Exercise MnemoTrace's agent memory lifecycle.")
    ap.add_argument("--base", default="http://localhost:8000", help="MnemoTrace HTTP base URL")
    ap.add_argument("--key", default=None, help="Bearer key for HTTP")
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--agent", default="codex", help="agent name used in the session id")
    ap.add_argument("--project", default="super-memory", help="project/workspace name")
    ap.add_argument("--thread", default="demo", help="thread/conversation id")
    ap.add_argument("--no-flush", action="store_true", help="skip /v1/processing/flush")
    ap.add_argument("--no-verify", action="store_true", help="do not fail if recall/report misses the test phrase")
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    print("mode: http")
    print(f"base: {base}")
    key = args.key or os.environ.get("MNEMOTRACE_API_KEY")
    if not key or not args.space_id:
        ap.error("HTTP mode requires a Bearer key and --space-id")
    call = http_call(base, key, args.space_id)

    result = exercise_lifecycle(
        call,
        namespace=args.space_id,
        agent=args.agent,
        project=args.project,
        thread=args.thread,
        no_flush=args.no_flush,
    )

    if not result["found"] and not args.no_verify:
        print("\nFAIL: lifecycle did not recall or report the saved durable memory.", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nInspect in the console: {args.base.rstrip('/')}/ui/")


if __name__ == "__main__":
    main()
