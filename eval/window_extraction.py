"""Extraction-only comparison; synthetic replay by default, real models only by explicit flag.

This is not an answer-quality benchmark and does not replace bench.py/full-context QA.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnemotrace import Config, ExtractionError, Memory  # noqa: E402
from mnemotrace.consolidate.windows import WINDOW_PREFIX  # noqa: E402
from mnemotrace.llm import Completion, LLM  # noqa: E402
from mnemotrace.llm.base import complete_result  # noqa: E402


class RecordedExtraction(LLM):
    """Exact synthetic input/output pairs; no natural-language extraction rules."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses

    def complete(self, prompt: str, **kwargs) -> str:
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        return json.dumps({"facts": [
            {"subject": "user", "predicate": "owns", "object": self.responses[s["content"]],
             "source_ids": [s["id"]]} for s in data["pending"]], "working": []})


class MeteredLLM(LLM):
    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def complete(self, prompt: str, **kwargs) -> str:
        return self.complete_result(prompt, **kwargs).text

    def complete_result(self, prompt: str, **kwargs) -> Completion:
        start = time.perf_counter()
        result = complete_result(self.inner, prompt, **kwargs)
        self.calls.append({"latency_ms": (time.perf_counter() - start) * 1000,
                           "input_bytes": len((prompt + kwargs.get("system", "")).encode("utf-8")),
                           "finish_reason": result.finish_reason, "usage": result.usage})
        return result


def compare(events: int, runs: int, extractor: str | None = None) -> dict:
    expected = {f"I own synthetic_asset_{i:04d}.": f"synthetic_asset_{i:04d}" for i in range(events)}
    results = []
    for run in range(runs):
        for mode in ("immediate", "window"):
            if extractor:
                from mnemotrace.llm.providers import make_llm
                inner = make_llm(extractor)
            else:
                inner = RecordedExtraction(expected)
            meter = MeteredLLM(inner)
            memory = Memory(llm=meter)
            sources = {}
            start = time.perf_counter()
            for index, (text, obj) in enumerate(expected.items()):
                ep = memory.add(text, session_id="synthetic-session", event_time=1_700_000_000 + index)
                sources[obj] = ep.id
                if mode == "immediate":
                    memory.consolidate()
            if mode == "window":
                memory.consolidate()
            elapsed = (time.perf_counter() - start) * 1000
            facts = memory.fact_store.values()
            found = {f.object for f in facts if f.object in sources}
            wrong_sources = sum(set(f.provenance) != {sources[f.object]} for f in facts if f.object in sources)
            latencies = sorted(c["latency_ms"] for c in meter.calls)
            totals = {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                values = [c["usage"].get(key) if c["usage"] is not None else None for c in meter.calls]
                totals[key] = sum(values) if all(v is not None for v in values) else None
            results.append({"run": run, "mode": mode, "model_calls": len(meter.calls), **totals,
                            "model_latency_p50_ms": statistics.median(latencies),
                            "model_latency_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1],
                            "ingest_and_extract_ms": elapsed, "facts_expected": events,
                            "facts_found": len(found), "omitted": sorted(set(sources) - found),
                            "wrong_sources": wrong_sources,
                            "unexpected_facts": sum(f.object not in sources for f in facts),
                            "calls": meter.calls})
            memory.close()
    root = Path(__file__).resolve().parents[1]
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True))
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = None, None
    return {"kind": "extraction_only", "code_revision": revision, "working_tree_dirty": dirty,
            "synthetic_replay": extractor is None,
            "extractor": extractor, "events": events, "runs": runs,
            "budgets": {k: getattr(Config(), k) for k in (
                "extraction_max_input_bytes", "extraction_context_bytes", "extraction_max_output_tokens")},
            "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=24)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--extractor", help="Explicitly opt into a configured real model; may incur charges")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.events < 1 or args.runs < 1:
        parser.error("events and runs must be positive")
    try:
        report = compare(args.events, args.runs, args.extractor)
    except ExtractionError as exc:
        report = {"kind": "extraction_only", "status": "failed", "error": exc.detail(),
                  "synthetic_replay": args.extractor is None, "events": args.events, "runs": args.runs,
                  "results": []}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "results": [{k: v for k, v in row.items() if k != "calls"}
                                           for row in report["results"]]}, ensure_ascii=False, indent=2))
    return int(report.get("status") == "failed" or any(
        r["omitted"] or r["wrong_sources"] or r["unexpected_facts"] for r in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
