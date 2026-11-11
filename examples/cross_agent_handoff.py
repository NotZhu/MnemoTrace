"""模拟两个 agent 共享同一个用户所有的 忆痕 记忆命名空间。

先配置 MNEMOTRACE_LLM 与对应凭据。按 docs/reference/configuration.md 的 Postgres 数据库基础章节初始化数据库、创建账号和空间，
设置 MNEMOTRACE_DATABASE_URL 为运行角色连接串，再启动服务：

    python -m uvicorn mnemotrace.server.app:app --port 8000

然后运行：

    python examples/cross_agent_handoff.py \
        --base http://localhost:8000 \
        --key "$MNEMOTRACE_API_KEY" --space-id "<SPACE_ID>" \
        --project super-memory

脚本扮演 Codex 写入一条持久的项目决策，再由 Claude Code 用另一个 session_id 把它召回。
参与交接的客户端使用授权凭据，并选择同一个 Postgres 空间。
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


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


Call = Callable[[str, Optional[dict], str], dict]
MEMORY = "Project decision: the launch checklist must include committed eval logs."
QUERY = "What launch checklist decision did the previous agent record?"
VERIFY_PHRASE = "committed eval logs"


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


def exercise_handoff(
    call: Call,
    *,
    namespace: str,
    project: str,
    source_agent: str = "codex",
    target_agent: str = "claude-code",
    source_thread: str = "handoff-source",
    target_thread: str = "handoff-target",
    no_flush: bool = False,
    output: Callable[[str], None] = print,
) -> dict:
    source_session = f"{source_agent}:{project}:{source_thread}"
    target_session = f"{target_agent}:{project}:{target_thread}"

    output(f"namespace: {namespace}")
    output(f"source_session: {source_session}")
    output(f"target_session: {target_session}")

    output("\n== 1. Source agent remembers a durable decision ==")
    remembered = call("/v1/remember", {
        "content": MEMORY,
        "session_id": source_session,
        "extract": True,
    }, "POST")
    output(json.dumps(remembered, ensure_ascii=False, indent=2))

    flushed_source = None
    if not no_flush:
        output("\n== 2. Source agent flushes its session ==")
        flushed_source = call("/v1/processing/flush", {
            "session_id": source_session,
            "include_digests": True,
        }, "POST")
        output(json.dumps(flushed_source, ensure_ascii=False, indent=2))

    output("\n== 3. Target agent recalls from the same memory space ==")
    recalled = call("/v1/recall", {
        "query": QUERY,

        "n_chunks": 3,
    }, "POST")
    context = (recalled.get("context") or "").strip()
    output(context[:1000] if context else "(no relevant memory found)")

    found = VERIFY_PHRASE in context
    if found:
        output("\nPASS: target agent recalled the source agent's durable memory.")

    flushed_target = None
    if not no_flush:
        output("\n== 4. Target agent flushes its session ==")
        flushed_target = call("/v1/processing/flush", {
            "session_id": target_session,
            "include_digests": True,
        }, "POST")
        output(json.dumps(flushed_target, ensure_ascii=False, indent=2))

    return {
        "source_session": source_session,
        "target_session": target_session,
        "remembered": remembered,
        "flushed_source": flushed_source,
        "recalled": recalled,
        "context": context,
        "found": found,
        "flushed_target": flushed_target,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Exercise cross-agent MnemoTrace memory handoff.")
    ap.add_argument("--base", default="http://localhost:8000", help="MnemoTrace HTTP base URL")
    ap.add_argument("--key", default=None, help="Bearer key for HTTP")
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--project", default="super-memory", help="project/workspace name")
    ap.add_argument("--source-agent", default="codex", help="agent that writes the memory")
    ap.add_argument("--target-agent", default="claude-code", help="agent that recalls the memory")
    ap.add_argument("--source-thread", default="handoff-source", help="source thread id")
    ap.add_argument("--target-thread", default="handoff-target", help="target thread id")
    ap.add_argument("--no-flush", action="store_true", help="skip session flush calls")
    ap.add_argument("--no-verify", action="store_true", help="do not fail if recall misses the test phrase")
    args = ap.parse_args(argv)

    base = args.base.rstrip("/")
    print("mode: http")
    print(f"base: {base}")
    key = args.key or os.environ.get("MNEMOTRACE_API_KEY")
    if not key or not args.space_id:
        ap.error("HTTP mode requires a Bearer key and --space-id")
    call = http_call(base, key, args.space_id)

    result = exercise_handoff(
        call,
        namespace=args.space_id,
        project=args.project,
        source_agent=args.source_agent,
        target_agent=args.target_agent,
        source_thread=args.source_thread,
        target_thread=args.target_thread,
        no_flush=args.no_flush,
    )

    if not result["found"] and not args.no_verify:
        print("\nFAIL: target agent did not recall the source memory.", file=sys.stderr)
        raise SystemExit(1)

    print(f"\nInspect in the console: {args.base.rstrip('/')}/ui/")


if __name__ == "__main__":
    main()
