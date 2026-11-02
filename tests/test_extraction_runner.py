"""Native service measurement/restart and budget tests with recorded completions."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

EVAL = Path(__file__).resolve().parents[1] / 'eval'


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(EVAL))
    return importlib.import_module('extraction_runner')


@pytest.mark.parametrize('mode,case_id', [('immediate', 'independent-1'), ('buffered', 'independent-1'),
    ('immediate', 'lifecycle-1'), ('buffered', 'lifecycle-1'), ('immediate', 'isolation-5')])
def test_service_measurement_completes_same_summary_work_and_restores(harness, mode, case_id, monkeypatch, tmp_path):
    from mnemotrace.consolidate.planning import WINDOW_PREFIX
    from mnemotrace.llm import Completion, FakeLLM, providers
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1')
    monkeypatch.setenv('MNEMOTRACE_SESSION_OUTCOMES', '0')
    responses = {'我喜欢绿茶。': ('likes', '绿茶'), '我喜欢苹果。': ('likes', '苹果'),
                 '我有一辆自行车。': ('owns', '自行车')}
    def handler(prompt, system):
        if WINDOW_PREFIX not in prompt:
            return Completion('合成摘要', 'stop', {'total_tokens': 3})
        payload = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        if case_id == 'lifecycle-1':
            s, = payload['pending']
            return Completion(json.dumps(dict(summaries=[], facts=[], working=[dict(content=s['content'], kind='task',
                assertion_kind='unspecified', source_ids=[s['id']])])), 'stop', {'total_tokens': 7})
        facts = [dict(subject='user', predicate=responses[s['content']][0], object=responses[s['content']][1], text=s['content'], assertion_kind='unspecified', source_ids=[s['id']]) for s in payload['pending']]
        return Completion(json.dumps(dict(facts=facts, working=[], summaries=[])), 'stop', {'total_tokens': 7})
    monkeypatch.setattr(providers, 'make_llm', lambda *a, **kw: FakeLLM(handler=handler))
    budget_path = tmp_path / 'budget.json'
    harness.Budget.create(budget_path, calls=20)
    args = SimpleNamespace(out=tmp_path, revision='synthetic', mode=mode, run=0,
                           extractor='synthetic', budget=budget_path, suite=EVAL/'fixtures/extraction_quality.json')
    case = next(c for c in harness.load_suite(args.suite)['cases'] if c['id'] == case_id)
    row = harness.run_case(case, args)
    assert row['status'] == 'completed', row['error_code']
    assert row['score']['ok'] and row['raw_score']['ok'] and row['restored']
    originals = [ep for snapshot in row['snapshots'].values() for ep in snapshot['episodes']]
    assert all(ep['summary'] for ep in originals if len(ep['content']) >= 2000)
    assert sum(c['phase'] == 'extraction' for c in row['calls']) >= 1
    ledger = json.loads(budget_path.read_text())
    assert ledger['calls'] == sum(c.get('provider_request', False) for c in row['calls']) and ledger['inflight'] == 0
    if case_id == 'isolation-5':
        assert row['acknowledgements'][0]['recovered_after_retry']
        assert sum(len(s['episodes']) for s in row['snapshots'].values()) == len(case['events'])


def test_budget_stops_on_missing_usage_without_inventing_zero_cost(harness, tmp_path):
    budget = harness.Budget.create(tmp_path/'budget.json', calls=2)
    budget.claim()
    budget.finish(None)
    with pytest.raises(harness.BudgetStopped):
        budget.claim()


def test_budget_counts_failed_requests_and_enforces_call_limit(harness, tmp_path):
    budget = harness.Budget.create(tmp_path/'budget.json', calls=1)
    budget.claim()
    budget.finish({'total_tokens': 5})
    with pytest.raises(harness.BudgetStopped):
        budget.claim()


def test_missing_deepseek_credentials_fail_before_model_or_store(harness, monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    args = SimpleNamespace(extractor='deepseek', out=tmp_path)
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        harness.run_case({}, args)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('limits', [{'calls': 0}, {'seconds': -1}, {'tokens': True}])
def test_invalid_budget_does_not_create_ledger(harness, tmp_path, limits):
    target = tmp_path/'budget.json'
    with pytest.raises(ValueError):
        harness.Budget.create(target, **limits)
    assert not target.exists()
