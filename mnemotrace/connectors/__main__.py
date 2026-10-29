"""CLI：把外部聊天/历史记录导出导入 忆痕。

  # 本地文件存储（事实抽取需要 --llm 配置）：
  python -m mnemotrace.connectors --file conversations.json --namespace me

  # 更高质量的本地导入（真实嵌入 / LLM 提取器；需要可选依赖 + key）：
  python -m mnemotrace.connectors -f conversations.json -n me --embedder bge-small --llm deepseek

  # 推送到运行中的 忆痕 服务器，而不是本地存储：
  python -m mnemotrace.connectors -f conversations.json --api-url http://localhost:8000 --key sk-alice-123

  # 从 stdin 读取并显式指定格式：
  cat log.jsonl | python -m mnemotrace.connectors --format jsonl -n me

本模块是纯标准库实现；远程 POST 使用 urllib（不需要 httpx），因此在零配置安装下也能完成导入。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import urllib.request
import urllib.error

from . import parse, sniff


def _read_input(path: str | None) -> str:
    if path and path != "-":
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def _post_remote(api_url: str, key: str, sessions: list, consolidate: bool, summarize: bool) -> dict:
    body = json.dumps({
        "sessions": [dataclasses.asdict(s) for s in sessions],
        "consolidate": consolidate,
        "summarize": summarize,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(api_url.rstrip("/") + "/v1/import", data=body,
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 (用户提供的 URL 是有意为之)
        return json.load(resp)


def _import_local(args, sessions) -> dict:
    from ..service import MemoryService

    # 与 HTTP/MCP 共用摘要路径和完整读-改-存锁；不能另行选择旧账号名目录。
    # 默认使用 hashing 嵌入；事实抽取需要显式选择模型。
    service = MemoryService(data_dir=args.data_dir, embedder_name=args.embedder or "hashing",
                            llm_name=args.llm or "")
    try:
        stats = service.import_(args.namespace, sessions=sessions,
                                consolidate=not args.no_consolidate,
                                summarize=not args.no_summarize)
        stats["store"] = service._path(args.namespace)
        return stats
    finally:
        service.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m mnemotrace.connectors",
                                 description="Import a chat/history export into MnemoTrace memory.")
    ap.add_argument("--file", "-f", help="export file (default: stdin)")
    ap.add_argument("--format", default="auto",
                    help="chatgpt | messages | records | jsonl | transcript | auto (default)")
    ap.add_argument("--namespace", "-n", default="me", help="memory namespace / user id (default: me)")
    ap.add_argument("--data-dir", default=os.environ.get("MNEMOTRACE_DATA_DIR", "~/.mnemotrace/data"),
                    help="local store dir (local mode; default ~/.mnemotrace/data)")
    ap.add_argument("--api-url", help="push to a running MnemoTrace server instead of a local store")
    ap.add_argument("--key", default=os.environ.get("MNEMOTRACE_API_KEY", ""), help="Bearer key for --api-url")
    ap.add_argument("--embedder", help="local mode: real embedder (e.g. bge-small); default hashing")
    ap.add_argument("--llm", help="local mode: LLM for fact extraction (e.g. deepseek); required unless --no-consolidate is used")
    ap.add_argument("--no-consolidate", action="store_true", help="skip fact extraction (raw episodes only)")
    ap.add_argument("--no-summarize", action="store_true", help="skip L2 session summaries")
    ap.add_argument("--dry-run", action="store_true", help="parse + report only; don't ingest")
    args = ap.parse_args()

    raw = _read_input(args.file)
    fmt = sniff(raw) if args.format == "auto" else args.format
    sessions = parse(raw, format=args.format)
    n_msgs = sum(len(s.messages) for s in sessions)
    print(f"parsed {len(sessions)} session(s), {n_msgs} message(s) [format: {fmt}]", file=sys.stderr)

    if args.dry_run:
        for s in sessions[:3]:
            preview = s.to_text()[:200].replace("\n", " ")
            print(f"  - {s.session_id}: {preview}…", file=sys.stderr)
        return
    if not sessions:
        print("nothing to import.", file=sys.stderr)
        return

    from ..extraction import ExtractionError

    try:
        if args.api_url:
            stats = _post_remote(args.api_url, args.key, sessions,
                                 not args.no_consolidate, not args.no_summarize)
        else:
            stats = _import_local(args, sessions)
    except ExtractionError as exc:
        ap.exit(1, json.dumps(exc.detail(), ensure_ascii=False) + "\n")
    except ImportError:
        ap.exit(1, 'Install mnemotrace-memory[llm] for model extraction.\n')
    except urllib.error.HTTPError as exc:
        from ..extraction import LLMNotConfiguredError, ExtractionResponseError
        errors = {e.code: e for e in (ExtractionError, LLMNotConfiguredError, ExtractionResponseError)}
        try:
            detail = json.load(exc).get("detail", {})
            error_type = errors.get(detail.get("code")) if isinstance(detail, dict) else None
            if error_type is not None:
                ids = detail.get("episode_ids", [])
                error = error_type(stored_raw=detail.get("stored_raw") is True,
                                   episode_ids=ids if isinstance(ids, list) and all(isinstance(i, str) for i in ids) else [])
                ap.exit(1, json.dumps(error.detail(), ensure_ascii=False) + "\n")
        except (ValueError, AttributeError):
            pass
        ap.exit(1, f'Import failed: HTTP {exc.code}. Check server extraction configuration and session status.\n')
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
