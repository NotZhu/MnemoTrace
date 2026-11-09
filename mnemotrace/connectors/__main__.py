"""Import a chat/history export into a PostgreSQL memory space."""
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


def _post_remote(api_url: str, key: str, space_id: str, sessions: list,
                 consolidate: bool, summarize: bool, agent_id: str | None) -> dict:
    body = json.dumps({
        "space_id": space_id,
        "agent_id": agent_id,
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
    from ..cli import local_space

    with local_space(args) as (service, context):
        return service.import_(context, sessions=sessions, consolidate=not args.no_consolidate,
                               summarize=not args.no_summarize, agent_id=args.agent_id)


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m mnemotrace.connectors",
                                 description="Import a chat/history export into MnemoTrace memory.")
    ap.add_argument("--file", "-f", help="export file (default: stdin)")
    ap.add_argument("--format", default="auto",
                    help="chatgpt | messages | records | jsonl | transcript | auto (default)")
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--user-id", default=os.environ.get("MNEMOTRACE_USER_ID"))
    ap.add_argument("--agent-id")
    ap.add_argument("--api-url", help="Push to a running MnemoTrace HTTP server")
    key = ap.add_mutually_exclusive_group()
    key.add_argument("--key")
    key.add_argument("--key-file")
    ap.add_argument("--embedder", help="local mode: lexical (default), fastembed, or another configured embedder")
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

    from ..credentials import CredentialError, resolve_key
    from ..extraction import ExtractionError
    from ..store.postgres.errors import DatabaseError

    if not args.space_id or (not args.api_url and not args.user_id):
        ap.error('Select --space-id and, for direct database access, --user-id')
    try:
        if args.api_url:
            if args.user_id:
                ap.error('Remote identity is determined by the API key')
            key = resolve_key(key=args.key, key_file=args.key_file, required=True)
            stats = _post_remote(args.api_url, key, args.space_id, sessions,
                                 not args.no_consolidate, not args.no_summarize, args.agent_id)
        else:
            stats = _import_local(args, sessions)
    except ExtractionError as exc:
        ap.exit(1, json.dumps(exc.detail(), ensure_ascii=False) + "\n")
    except DatabaseError as exc:
        ap.exit(1, f'Import failed ({exc.code}).\n')
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get('detail')
        except (ValueError, AttributeError):
            detail = None
        if isinstance(detail, dict) and 'code' in detail:
            ap.exit(1, json.dumps(detail, ensure_ascii=False) + '\n')
        ap.exit(1, f'Import failed: HTTP {exc.code}.\n')
    except (CredentialError, ImportError, urllib.error.URLError, ValueError):
        ap.exit(1, 'Invalid import configuration or request.\n')
    print(json.dumps(stats, ensure_ascii=False))
    if stats.get('ok') is False:
        ap.exit(1)


if __name__ == "__main__":
    main()
