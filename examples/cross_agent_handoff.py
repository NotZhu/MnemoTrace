"""模拟两个 agent 共享同一个用户所有的 忆痕 记忆命名空间。

先配置 MNEMOTRACE_LLM 与对应凭据，再运行无服务端的本地示例：

    python examples/cross_agent_handoff.py --local

先启动 忆痕 服务：

    MNEMOTRACE_OPEN=1 MNEMOTRACE_EMBEDDER=hashing uvicorn mnemotrace.server.app:app --port 8000

然后运行：

    python examples/cross_agent_handoff.py \
        --base http://localhost:8000 \
        --key me \
        --project super-memory

脚本扮演 Codex 写入一条持久的项目决策，再由 Claude Code 用另一个 session_id 把它召回。
相同的 Bearer key = 相同的用户记忆命名空间。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
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


def http_call(base: str, key: str) -> Call:
    base = base.rstrip("/")

    def call(path: str, body: dict | None = None, method: str = "POST") -> dict:
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


def local_call(key: str, data_dir: str) -> Call:
    from mnemotrace.service import MemoryService

    svc = MemoryService(data_dir=data_dir, embedder_name="hashing")

    def call(path: str, body: dict | None = None, method: str = "POST") -> dict:
        body = body or {}
        if method != "POST":
            raise ValueError(f"local handoff smoke only supports POST, got {method}")
        if path == "/v1/remember":
            return svc.remember(
                key,
                body["content"],
                session_id=body.get("session_id", "default"),
                scope=body.get("scope", "auto"),
            )
        if path == "/v1/recall":
            return svc.recall(
                key,
                body["query"],
                session_id=body.get("session_id"),
                n_chunks=body.get("n_chunks", 3),
            )
        if path == "/v1/sessions/close":
            return svc.close_session(
                key,
                body.get("session_id", "default"),
                summarize=body.get("summarize", True),
                clear_working=body.get("clear_working", True),
            )
        raise ValueError(f"local handoff smoke does not implement {path}")

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
    no_close: bool = False,
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
        "scope": "long",
    }, "POST")
    output(json.dumps(remembered, ensure_ascii=False, indent=2))

    closed_source = None
    if not no_close:
        output("\n== 2. Source agent closes its session ==")
        closed_source = call("/v1/sessions/close", {
            "session_id": source_session,
            "summarize": True,
            "clear_working": True,
        }, "POST")
        output(json.dumps(closed_source, ensure_ascii=False, indent=2))

    output("\n== 3. Target agent recalls from the same user namespace ==")
    recalled = call("/v1/recall", {
        "query": QUERY,
        "session_id": target_session,
        "n_chunks": 3,
    }, "POST")
    context = (recalled.get("context") or "").strip()
    output(context[:1000] if context else "(no relevant memory found)")

    found = VERIFY_PHRASE in context
    if found:
        output("\nPASS: target agent recalled the source agent's durable memory.")

    closed_target = None
    if not no_close:
        output("\n== 4. Target agent closes its session ==")
        closed_target = call("/v1/sessions/close", {
            "session_id": target_session,
            "summarize": True,
            "clear_working": True,
        }, "POST")
        output(json.dumps(closed_target, ensure_ascii=False, indent=2))

    return {
        "source_session": source_session,
        "target_session": target_session,
        "remembered": remembered,
        "closed_source": closed_source,
        "recalled": recalled,
        "context": context,
        "found": found,
        "closed_target": closed_target,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Exercise cross-agent MnemoTrace memory handoff.")
    ap.add_argument("--local", action="store_true",
                    help="use local MemoryService storage instead of an HTTP server")
    ap.add_argument("--data-dir", default=None,
                    help="local MnemoTrace data dir for --local (default: temporary smoke-test dir)")
    ap.add_argument("--base", default="http://localhost:8000", help="MnemoTrace HTTP base URL")
    ap.add_argument("--key", default="me", help="Bearer key = user memory namespace")
    ap.add_argument("--project", default="super-memory", help="project/workspace name")
    ap.add_argument("--source-agent", default="codex", help="agent that writes the memory")
    ap.add_argument("--target-agent", default="claude-code", help="agent that recalls the memory")
    ap.add_argument("--source-thread", default="handoff-source", help="source thread id")
    ap.add_argument("--target-thread", default="handoff-target", help="target thread id")
    ap.add_argument("--no-close", action="store_true", help="skip session close calls")
    ap.add_argument("--no-verify", action="store_true", help="do not fail if recall misses the test phrase")
    args = ap.parse_args(argv)

    if args.local:
        data_dir = args.data_dir or tempfile.mkdtemp(prefix="mnemotrace-handoff.")
        print("mode: local")
        print(f"data_dir: {data_dir}")
        call = local_call(args.key, data_dir)
    else:
        base = args.base.rstrip("/")
        print("mode: http")
        print(f"base: {base}")
        call = http_call(base, args.key)

    result = exercise_handoff(
        call,
        namespace=args.key,
        project=args.project,
        source_agent=args.source_agent,
        target_agent=args.target_agent,
        source_thread=args.source_thread,
        target_thread=args.target_thread,
        no_close=args.no_close,
    )

    if not result["found"] and not args.no_verify:
        print("\nFAIL: target agent did not recall the source memory.", file=sys.stderr)
        raise SystemExit(1)

    if args.local:
        print("\nFor a real local agent setup, point MCP at the same namespace/data dir.")
    else:
        print(f"\nInspect in the console: {args.base.rstrip('/')}/ui/  (key: {args.key})")


if __name__ == "__main__":
    main()
