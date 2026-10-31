"""Observation wraps real service commits, never substitutes its scheduler."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'eval'))
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1')
    monkeypatch.setenv('MNEMOTRACE_CONFLICT_DETECTION', '0')
    monkeypatch.setenv('MNEMOTRACE_SESSION_OUTCOMES', '0')
    return importlib.import_module('buffered_replay_runner')


def rapid_replay(case, call):
    from buffered_replay import replay
    immediate = deepcopy(case)
    for event in immediate['events']:
        event['delay'] = 0
    for control in immediate.get('controls', []):
        control['delay'] = 0
    return replay(immediate, call)


@pytest.mark.parametrize('mode', ['immediate', 'buffered'])
@pytest.mark.parametrize('case_id', [f'{family}-{size}' for family in
    ('burst', 'continuous', 'idle', 'longinput', 'inflight', 'isolation') for size in ('short', 'long')])
def test_exact_responses_trace_committed_ranges_and_restore(runner, tmp_path, case_id, mode):
    from buffered_replay import SUITE, load_suite
    from buffered_replay_report import validate_observation
    case = next(c for c in load_suite()['cases'] if c['id'] == case_id)
    args = SimpleNamespace(out=tmp_path, extractor=None, budget=tmp_path/'unused',
                           mode=mode, run=0, suite=SUITE)
    row = runner.run_case(case, args, replay_fn=rapid_replay)
    assert row['status'] == 'completed', (row['errors'], [(r['event'], r['status']) for r in row['arrivals'] if r['kind']=='event'])
    assert row['restored']
    assert not validate_observation(row, case, row['suite_hash'])
    assert row['score']['ok'], row['score']
    assert row['raw_score']['ok'], row['raw_score']
    eligible = sum(len(event['text']) >= 2000 for event in case['events'])
    summary_calls = sum(c['phase'] == 'summary' for c in row['calls'])
    assert summary_calls >= eligible if eligible else summary_calls == 0
    assert all(not c['provider_request'] for c in row['calls'])
    if case_id == 'burst-short' and mode == 'buffered':
        assert sum(c['phase'] == 'extraction' for c in row['calls']) == 1
    assert all(r['completed_at'] >= r['saved_at'] for r in row['arrivals'] if r['kind'] == 'event')
    assert (tmp_path/f'{case_id}-{mode}-0/observations.jsonl').exists()


def test_wire_byte_parts_are_exact_with_unicode_escaping(runner):
    window = dict(pending=[dict(id='a', content='中文\n"quote"')], context=[dict(id='b', content='前文\\')])
    prompt = 'prefix'+json.dumps(window, ensure_ascii=False)+'suffix'
    parts = runner.byte_parts(prompt, '系统', window)
    assert parts['total'] == sum(v for k, v in parts.items() if k != 'total')
    assert parts['metadata'] > 0 and parts['context'] > 0 and parts['template'] == len('prefixsuffix')


def test_credential_failure_occurs_before_store_or_budget(runner, monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    args = SimpleNamespace(out=tmp_path, extractor='deepseek-chat')
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        runner.run_case({}, args)
    assert list(tmp_path.iterdir()) == []


def test_inflight_window_does_not_absorb_later_saved_input(runner, tmp_path):
    from buffered_replay import SUITE, load_suite
    from mnemotrace.llm import Completion, FakeLLM
    case = next(c for c in load_suite()['cases'] if c['id'] == 'burst-short')
    entered, release = threading.Event(), threading.Event()
    def respond(prompt, system):
        if 'MNEMOTRACE_WINDOW' in prompt and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return Completion('{"facts":[],"working":[]}' if 'MNEMOTRACE_WINDOW' in prompt else '摘要', 'stop')
    def controlled(case, call):
        origin, rows = time.perf_counter(), []
        for index, event in enumerate(case['events']):
            row = dict(kind='event',event=event['id'],planned_at=0,generated_at=time.perf_counter()-origin,
                       entered_at=time.perf_counter()-origin)
            if index == 2:
                command = dict(kind='flush',user='u',session='s')
                control = dict(kind='flush',user='u',planned_at=0,generated_at=time.perf_counter()-origin,
                               entered_at=time.perf_counter()-origin)
                call(command, control)
                assert entered.wait(5)
            call(dict(kind='event',event=event), row)
            row.update(returned_at=time.perf_counter()-origin,status='completed')
            rows.append(row)
            if index == 2:
                release.set()
        return rows
    args = SimpleNamespace(out=tmp_path, extractor=None,budget=None,mode='buffered',run=0,suite=SUITE)
    row = runner.run_case(case, args, replay_fn=controlled, model_override=FakeLLM(handler=respond))
    assert row['status'] == 'completed', row['errors']
    assert row['inflight_observed']
    assert len(row['windows'][0]['pending']) == 2
    assert any(s['event_time'] == case['events'][2]['at'] for w in row['windows'][1:] for s in w['pending'])
