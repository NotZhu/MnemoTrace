from __future__ import annotations

import json
import sys
from types import ModuleType

from eval.harness import evaluate, run_mnemotrace, write_jsonl
from eval.report import format_synthetic_report, load
from eval.synthetic import ITEMS
from tests.model_fixtures import RecordedLLM


def test_mem0_baseline_loads_the_third_party_memory_class(monkeypatch):
    from eval.bench import SYSTEMS, Rig

    class Memory:
        @classmethod
        def from_config(cls, config):
            return cls()

        def search(self, **kwargs):
            return {"results": [{"memory": "Synthetic fact"}]}

    mem0 = ModuleType("mem0")
    mem0.Memory = Memory
    monkeypatch.setitem(sys.modules, "mem0", mem0)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")

    baseline = SYSTEMS["mem0"](Rig(None, None, None, None))
    assert baseline.context({"question_id": "synthetic", "question": "What was stored?"}) == "FACTS:\n- Synthetic fact"


def test_synthetic_harness_in_memory_smoke():
    item = next(it for it in ITEMS if it.id == "tmp2")
    ok, tokens, latency = run_mnemotrace(item, storage="memory", llm=RecordedLLM())
    assert ok is True
    assert tokens > 0
    assert latency >= 0.0


def test_synthetic_harness_jsonl_raw_log_contains_items_and_summary(tmp_path):
    rows, summary = evaluate(storage="memory", llm=RecordedLLM())
    path = tmp_path / "harness.jsonl"
    write_jsonl(str(path), rows, summary)

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(ITEMS) + 1
    assert records[-1]["type"] == "summary"
    assert records[-1]["accuracy"] == 100.0
    assert records[-1]["storage"] == "memory"
    first = records[0]
    assert {"id", "category", "question", "gold", "ok", "tokens", "latency_ms", "naive_ok"} <= first.keys()


def test_report_reads_synthetic_harness_jsonl(tmp_path):
    rows, summary = evaluate(storage="memory", llm=RecordedLLM())
    path = tmp_path / "harness.jsonl"
    write_jsonl(str(path), rows, summary)

    report = format_synthetic_report(str(path), load(str(path)))
    assert "Synthetic harness" in report
    assert "OVERALL" in report
    assert "100.0% (10)" in report
    assert "avg context tokens" in report
    assert "p50 latency ms" in report
    assert "p95 latency ms" in report
