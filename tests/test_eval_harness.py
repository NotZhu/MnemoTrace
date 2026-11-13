from __future__ import annotations

import json
import sys
from types import ModuleType

from eval.harness import evaluate, run_mnemotrace, score_search, write_jsonl
from eval.report import format_synthetic_report, load
from eval.synthetic import ITEMS
from mnemotrace.retrieve.answers import SearchResult
from mnemotrace.types import Fact
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


def test_evidence_scoring_requires_the_correct_value_at_rank_one():
    correct = Fact('Ana', 'favorite_color', 'blue', text='Ana prefers blue.')
    wrong = Fact('Ana', 'favorite_color', 'blueberry', text='Ana prefers blueberry.')
    result = SearchResult('What color?', [wrong, correct])
    assert result.status == 'evidence_only' and result.answer() == ''
    assert not score_search('blue', result)
    assert not score_search(None, result)
    assert score_search('blue', SearchResult('What color?', [correct, wrong]))


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
    assert (first['status'], first['answer'], first['top_fact_object']) == ('evidence_only', '', 'Spotify')
    paths = {row['id']: (row['answer'], len(row['fact_ids'])) for row in records[:-1]
             if row['category'] == 'multi_hop'}
    assert paths == {'mh1': ('Netflix', 2), 'mh2': ('Seattle', 2)}


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


def test_window_extraction_smoke_uses_current_fact_schema():
    from eval.window_extraction import compare

    result = compare(events=1, runs=1)
    assert [(row['mode'], row['facts_found'], row['wrong_sources']) for row in result['results']] == [
        ('immediate', 1, 0), ('window', 1, 0),
    ]
