"""固定时钟与记录 ID，复用 eval 的摄入/上下文路径比较未关联用户的行为。

真实数据由 --data 显式提供；这里只验证读取等价性，不运行 answerer 或 judge。
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import io
import itertools
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def capture(source: Path, data: Path, limit: int, phase: str) -> list[dict]:
    sys.path.insert(0, str(source))
    from eval.ablate_readpath import context_for_item
    from eval.longmemeval import all_text, load_data
    from mnemotrace.embed import HashingEmbedder

    rows = []
    for item in load_data(str(data))[:limit]:
        ids = itertools.count(1)
        started = time.perf_counter()
        with patch("time.time", return_value=1_800_000_000.0), patch(
            "uuid.uuid4", side_effect=lambda ids=ids: UUID(int=next(ids) << 80),
        ):
            context, tokens = context_for_item(item, HashingEmbedder(), None, (), 10, 5, 8)
        rows.append({
            "kind": "context", "status": "exploratory", "phase": phase,
            "qid": item["question_id"], "context": context,
            "context_tokens_estimate": tokens,
            "full_context_tokens_estimate": len(all_text(item)) // 4,
            "ingest_and_context_ms": round((time.perf_counter() - started) * 1000, 3),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--baseline-ref", default="HEAD")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source", type=Path, help=argparse.SUPPRESS)
    ap.add_argument("--phase", default="after", help=argparse.SUPPRESS)
    args = ap.parse_args()
    data = args.data.resolve()
    if args.limit <= 0:
        ap.error("--limit must be positive")
    if args.source:
        rows = capture(args.source, data, args.limit, args.phase)
        args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return 0

    ref = subprocess.check_output(["git", "rev-parse", args.baseline_ref], cwd=ROOT, text=True).strip()
    script = Path(__file__).resolve()
    relative_script = script.relative_to(ROOT).as_posix()
    runtime_diff = subprocess.check_output(
        ["git", "diff", "--binary", ref, "--", "mnemotrace", "eval", "scripts"], cwd=ROOT,
    )
    if subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_script], cwd=ROOT, capture_output=True, check=False,
    ).returncode:
        runtime_diff += "".join(difflib.unified_diff(
            [], script.read_text(encoding="utf-8").splitlines(keepends=True),
            fromfile="/dev/null", tofile=f"b/{relative_script}",
        )).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="scope-equivalence-") as directory:
        temp = Path(directory)
        baseline = temp / "baseline"
        baseline.mkdir()
        archive = subprocess.check_output(["git", "archive", ref, "mnemotrace", "eval"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            # archive 仅来自固定 commit 下的两个源码目录；拒绝链接和任何越界路径。
            for member in tar.getmembers():
                if member.issym() or member.islnk() or not (baseline / member.name).resolve().is_relative_to(baseline):
                    raise ValueError("unsafe source archive entry")
            kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(baseline, **kwargs)
        captures = []
        for phase, source in (("before", baseline), ("after", ROOT)):
            out = temp / f"{phase}.jsonl"
            subprocess.run([
                sys.executable, str(Path(__file__).resolve()), "--source", str(source),
                "--data", str(data), "--limit", str(args.limit), "--phase", phase, "--out", str(out),
            ], cwd=ROOT, check=True)
            captures.append([json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()])
    before, after = captures
    equal = len(before) == len(after) == args.limit and all(
        a["qid"] == b["qid"] and a["context"] == b["context"]
        and a["context_tokens_estimate"] == b["context_tokens_estimate"]
        for a, b in zip(before, after)
    )
    runtime_artifact = gzip.compress(runtime_diff, mtime=0)
    meta = {
        "kind": "equivalence_meta", "status": "exploratory", "equal": equal,
        "baseline_commit": ref, "rows_per_phase": len(after),
        "implementation_base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip(),
        "implementation_uncommitted": bool(runtime_diff),
        "runtime_patch": args.out.with_suffix(".runtime.patch.gz").name,
        "runtime_patch_sha256": hashlib.sha256(runtime_artifact).hexdigest(),
        "data_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
        "extractor": "rules", "embedder": "hashing", "answerer": None, "judge": None,
        "token_metric": "characters // 4 estimate; not tokenizer counts",
        "latency_metric": "ingest + selected consolidation + context; not retrieval-only or QA latency",
        "command": subprocess.list2cmdline([sys.executable, *sys.argv]),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".runtime.patch.gz").write_bytes(runtime_artifact)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [meta, *before, *after]), encoding="utf-8")
    print(json.dumps({"equal": equal, "rows_per_phase": len(after), "out": str(args.out)}))
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
