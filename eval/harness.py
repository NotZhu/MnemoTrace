"""可复现的评测 harness。

    python eval/harness.py
    python eval/harness.py --out .artifacts/eval/synthetic.jsonl
    python eval/report.py .artifacts/eval/synthetic_durable.jsonl

让 MnemoTrace 跑每个 item 的历史并按类别打分，报告章程要求的三元组（准确率 + context token + 延迟）。
包含一个"naive recall"参照，充当 full-context 基线的替身：它能召回原始历史里的任何 fact，但无法
限定到某个时间点、无法分辨两个矛盾 fact 哪个是当前有效的、也无法拒答 —— 这正是一个 memory 系统
必须补上的能力面。（M1 会在同一 harness 之后换上真实 LOCOMO/LongMemEval + LLM judge + 真正的
full-context+LLM 基线。）
"""
from __future__ import annotations

import os
import sys
import time
import argparse
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace import MemoryEngine  # noqa: E402
from mnemotrace.util import DAY, stems  # noqa: E402
from eval.synthetic import ITEMS, EvalItem  # noqa: E402

BASE = 1_700_000_000.0


def _norm(s: str) -> str:
    return " ".join(stems(s))


def _contains(answer: str, gold: str) -> bool:
    return _norm(gold) in _norm(answer)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1))))
    return s[idx]


def _memory_for(storage: str, llm) -> MemoryEngine:
    if storage != "memory":
        raise ValueError("Use eval.storage_postgres for database evaluation")
    return MemoryEngine(llm=llm)


def run_mnemotrace(item: EvalItem, storage: str = "memory", *, llm) -> tuple[bool, int, float]:
    detail = run_mnemotrace_detail(item, storage=storage, llm=llm)
    return detail["ok"], detail["tokens"], detail["latency_ms"]


def run_mnemotrace_detail(item: EvalItem, storage: str = "memory", *, llm) -> dict:
    mem = _memory_for(storage, llm)
    for text, day in item.sessions:
        mem.add(text, user_id=item.id, event_time=BASE + day * DAY)
    mem.consolidate()
    when = None if item.as_of_day is None else BASE + item.as_of_day * DAY

    t0 = time.perf_counter()
    res = mem.search(item.question, user_id=item.id, as_of=when)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    if item.answer is None:
        correct = res.abstained
    else:
        correct = (not res.abstained) and _contains(res.answer(), item.answer)
    context_tokens = len(stems(" ".join(f.text for f in res.facts)))
    return {
        "id": item.id,
        "category": item.category,
        "question": item.question,
        "gold": item.answer,
        "storage": storage,
        "ok": correct,
        "answer": res.answer(),
        "abstained": res.abstained,
        "tokens": context_tokens,
        "latency_ms": latency_ms,
        "fact_ids": [f.id for f in res.facts],
    }


def run_naive_recall(item: EvalItem) -> tuple[bool, int]:
    history_text = " ".join(t for t, _ in item.sessions)
    tokens = len(stems(history_text))
    if item.answer is None:
        return False, tokens  # naive 读者无法拒答
    if item.as_of_day is not None:
        return False, tokens  # naive 读者没有时间范围限定
    return _contains(history_text, item.answer), tokens


def evaluate(storage: str = "memory", *, llm, run_item=None) -> tuple[list[dict], dict]:
    cats = sorted({it.category for it in ITEMS})
    rows: list[dict] = []
    eng_hits: dict[str, list[bool]] = defaultdict(list)
    base_hits: dict[str, list[bool]] = defaultdict(list)
    eng_tokens: list[int] = []
    base_tokens: list[int] = []
    latencies: list[float] = []

    runner = run_mnemotrace_detail if run_item is None else run_item
    for it in ITEMS:
        e = runner(it, storage=storage, llm=llm)
        b_ok, b_tok = run_naive_recall(it)
        row = {
            **e,
            "naive_ok": b_ok,
            "naive_tokens": b_tok,
        }
        rows.append(row)
        eng_hits[it.category].append(e["ok"])
        base_hits[it.category].append(b_ok)
        eng_tokens.append(e["tokens"])
        base_tokens.append(b_tok)
        latencies.append(e["latency_ms"])

    def acc(values: list[bool]) -> float:
        return 100.0 * sum(values) / len(values) if values else 0.0

    per_category = {
        c: {
            "engram_accuracy": acc(eng_hits[c]),
            "naive_accuracy": acc(base_hits[c]),
            "n": len(eng_hits[c]),
        }
        for c in cats
    }
    summary = {
        "type": "summary",
        "storage": storage,
        "n": len(rows),
        "accuracy": acc([r["ok"] for r in rows]),
        "naive_accuracy": acc([r["naive_ok"] for r in rows]),
        "avg_tokens": sum(eng_tokens) / len(eng_tokens) if eng_tokens else 0.0,
        "naive_avg_tokens": sum(base_tokens) / len(base_tokens) if base_tokens else 0.0,
        "token_ratio": sum(base_tokens) / max(1, sum(eng_tokens)),
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "per_category": per_category,
    }
    return rows, summary


def write_jsonl(path: str, rows: list[dict], summary: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        fh.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the synthetic MnemoTrace eval with an explicit LLM.")
    ap.add_argument("--storage", choices=["memory"], default="memory",
                    help="use eval.storage_postgres for the database path")
    ap.add_argument("--out", help="write per-item raw logs plus a final summary record as JSONL")
    ap.add_argument("--llm", help="model name, or set MNEMOTRACE_LLM")
    args = ap.parse_args()
    from mnemotrace.llm.providers import make_llm, load_dotenv
    load_dotenv()
    model = args.llm or os.environ.get("MNEMOTRACE_LLM", "")
    if not model:
        ap.error("configure --llm or MNEMOTRACE_LLM")

    rows, summary = evaluate(storage=args.storage, llm=make_llm(model))
    if args.out:
        write_jsonl(args.out, rows, summary)

    print(f"MnemoTrace eval harness -- {len(ITEMS)} items, LLM extraction with lexical retrieval")
    print(f"storage={args.storage} (RAM only)\n")
    print(f"  {'id':5} {'category':16} {'MnemoTrace':10} {'naive':6}  question")
    print("  " + "-" * 78)
    for row in rows:
        print(
            f"  {row['id']:5} {row['category']:16} "
            f"{'PASS' if row['ok'] else 'FAIL':7} {'ok' if row['naive_ok'] else '--':6}  {row['question']}"
        )

    print("\n  Per-category accuracy (MnemoTrace / naive-recall):")
    for c, vals in summary["per_category"].items():
        print(f"    {c:16} {vals['engram_accuracy']:6.1f}% / {vals['naive_accuracy']:5.1f}%")

    print("\n  Overall:")
    print(f"    accuracy        MnemoTrace {summary['accuracy']:5.1f}%   "
          f"naive-recall {summary['naive_accuracy']:5.1f}%")
    print(f"    context tokens  MnemoTrace {summary['avg_tokens']:5.1f}    "
          f"naive-recall {summary['naive_avg_tokens']:5.1f}   "
          f"({summary['token_ratio']:.1f}x leaner)")
    print(f"    latency (ms)    p50 {summary['latency_p50_ms']:.2f}   p95 {summary['latency_p95_ms']:.2f}")
    if args.out:
        print(f"\n  Raw log written to {args.out}")
    print("\n  Note: 'naive-recall' is a recall ceiling; it cannot abstain, scope by time, or resolve")
    print("  knowledge updates. Those columns are where a memory system earns its keep.")


if __name__ == "__main__":
    main()
