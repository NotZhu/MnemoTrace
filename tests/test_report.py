"""Report CLI regressions use generated samples instead of archived benchmark runs."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def write_log(path, *, errored=False):
    rows = []
    for index, latency in enumerate((10, 20, 100)):
        result = {"ok": True, "tok": 100, "lat": latency, "err": None, "pred": "A", "gold": "A"}
        memory = dict(result)
        if errored and index == 2:
            memory.update(ok=None, err="synthetic timeout", lat=9999)
        rows.append({
            "qid": f"q{index}", "cat": "synthetic-category",
            "sys": {"memory": memory, "full_context": result},
        })
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def report(*paths):
    return subprocess.check_output(
        [sys.executable, str(ROOT / "eval/report.py"), *map(str, paths)],
        text=True, encoding="utf-8",
    )


def test_report_cli_prints_latency_percentiles(tmp_path):
    out = report(write_log(tmp_path / "latency.jsonl"))
    assert re.search(r"p50 latency ms\s+20\s+20", out)
    assert re.search(r"p95 latency ms\s+100\s+100", out)


def test_report_cli_excludes_failed_rows_from_scored_denominators(tmp_path):
    out = report(write_log(tmp_path / "errored.jsonl", errored=True))
    assert re.search(r"scored items\s+2/3\s+3/3", out)
    assert re.search(r"errors\s+1\s+0", out)
    assert re.search(r"p95 latency ms\s+20\s+100", out)


def test_report_cli_prints_every_requested_log(tmp_path):
    first = write_log(tmp_path / "first.jsonl")
    second = write_log(tmp_path / "second.jsonl")
    out = report(first, second)
    assert f"FILE: {first}" in out
    assert f"FILE: {second}" in out
