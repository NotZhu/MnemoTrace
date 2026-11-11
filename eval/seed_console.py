"""Seed one Postgres memory space per selected LongMemEval item."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnemotrace.config import Config
from mnemotrace.memory import Memory
from mnemotrace.store.contracts import MessageInput
from mnemotrace.store.postgres import Database, DatabaseConfig, Spaces
from mnemotrace.util import DAY

BASE = 1_700_000_000.0


def sessions_of(item: dict):
    sessions = item.get("haystack_sessions", [])
    ids = item.get("haystack_session_ids") or [f"sess_{i}" for i in range(len(sessions))]
    return list(zip(ids, sessions))


def parse_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    tm = re.search(r"(\d{1,2}):(\d{2})", s)
    hh, mm = (int(tm.group(1)), int(tm.group(2))) if tm else (0, 0)
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=timezone.utc).timestamp(), f"{y:04d}-{mo:02d}-{d:02d}"
    except ValueError:
        return None


def ingest(memory, item):
    dates = item.get("haystack_dates") or []
    for index, (session_id, turns) in enumerate(sessions_of(item)):
        parsed = parse_date(dates[index] if index < len(dates) else None)
        event_time = parsed[0] if parsed else BASE + index * DAY
        messages = tuple(
            MessageInput(message_id=str(position), role=turn.get("role", "user"),
                         content=turn["content"], event_time=event_time)
            for position, turn in enumerate(turns) if turn.get("content", "").strip()
        )
        if messages:
            memory.remember_messages(session_id, messages)
    result = memory.flush_processing(timeout=300)
    if not result["ok"]:
        raise RuntimeError("Seed processing is incomplete; inspect the space's operation status")


def main(argv=None):
    from mnemotrace.llm.providers import configured_embedder, load_dotenv, make_llm

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New JSONL file mapping cases to space IDs")
    parser.add_argument("--user-id", default=os.environ.get("MNEMOTRACE_USER_ID"))
    parser.add_argument("--llm", default=os.environ.get("MNEMOTRACE_LLM"))
    parser.add_argument("--embedder", default=os.environ.get("MNEMOTRACE_EMBEDDER", "lexical"))
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="0 selects all remaining cases")
    args = parser.parse_args(argv)
    if not args.user_id or not args.llm or args.offset < 0 or args.limit < 0:
        parser.error("An existing user, explicit LLM, and nonnegative offset/limit are required")
    items = json.loads(args.data.expanduser().read_text(encoding="utf-8"))
    if not isinstance(items, list):
        parser.error("Expected a JSON list of cases")
    llm = make_llm(args.llm)
    embedder, model = configured_embedder(args.embedder)
    end = args.offset + args.limit if args.limit else None
    output = args.out.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    config = Config.from_env()
    with output.open("x", encoding="utf-8") as manifest:
        with Database(DatabaseConfig.from_env()) as database:
            spaces = Spaces(database)
            for position, item in enumerate(items[args.offset:end], start=args.offset + 1):
                space = spaces.create(args.user_id, f"LongMemEval {position}")
                manifest.write(json.dumps(dict(case=position, space_id=space.space_id)) + "\n")
                manifest.flush()
                scope = spaces.authorize(args.user_id, space.space_id)
                memory = Memory(database, scope, config=config, llm=llm,
                                        embedder=embedder, embedding_model=model)
                ingest(memory, item)


if __name__ == "__main__":
    main()
