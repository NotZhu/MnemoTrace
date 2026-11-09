"""Reports keep failures, unknown usage and unmatched pairs out of savings."""
import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'eval'))
    return (importlib.import_module('buffered_replay_report'),
            importlib.import_module('buffered_replay_comparison'))


def observation(case, mode, fingerprint, *, tokens=100):
    arrivals, pending, commits = [], [], []
    for i, event in enumerate(case['events']):
        arrivals.append(dict(kind='event', event=event['id'], episode_id=event['id'],
            planned_at=i*.01, generated_at=i*.01, entered_at=i*.01, saved_at=i*.01,
            returned_at=i*.01+.001, completed_at=3, status='completed'))
        pending.append(dict(id=event['id']+'@0', episode_id=event['id'], start=0, end=len(event['text']),
                            event_time=event['at'], ingested_at=event['at'], content=event['text']))
        commits.append(dict(event=event['id'], window_id='w', start=0, end=len(event['text']), saved_at=3))
    return dict(case_id=case['id'], run=0, mode=mode, status='completed', suite_hash=fingerprint,
        restored=True, final={}, raw={}, errors=[], inflight_observed=False,
        arrivals=arrivals, commits=commits, selections=[], budgets=dict(input_bytes=16384),
        calls=[dict(phase='extraction', window='w', status='completed', provider_request=True,
                    usage=dict(prompt_tokens=tokens-10, completion_tokens=10, total_tokens=tokens),
                    started_at=2, ended_at=2.5)],
        windows=[dict(window_id='w', pending=pending, context=[], validated=True, output=dict(facts=[],),
                      started_at=2, ended_at=2.5, input_bytes=8000, bytes=dict(body=6000, system=2000, total=8000))])


def directory(tmp_path):
    from buffered_replay import SUITE, load_suite
    from extraction_quality import suite_hash
    suite = load_suite()
    case = suite['cases'][0]
    (tmp_path/'suite.json').write_bytes(SUITE.read_bytes())
    fingerprint = suite_hash(SUITE)
    rows = [observation(case, mode, fingerprint, tokens=tokens)
            for mode, tokens in [('immediate', 100), ('buffered', 60)]]
    manifest = dict(kind='buffered_replay', suite_hash=fingerprint, runner_hashes={}, runs=1,
                    extractor='deepseek-chat', terminal_reason='finished',
                    expected=[[case['id'], m, 0] for m in ('immediate', 'buffered')])
    (tmp_path/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    return case, rows


def write_rows(path, rows):
    (path/'results.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')


def test_pair_savings_use_totals_and_report_recomputes_gold(modules, tmp_path):
    report, _ = modules
    _, rows = directory(tmp_path)
    rows[0]['score'] = dict(ok=True)  # Cannot trust cached scores.
    write_rows(tmp_path, rows)
    result = report.summarize(tmp_path)
    assert result['execution_complete']
    assert result['paired_observations'] == 1
    assert result['paired_savings']['total']['total_tokens'] == .4
    assert not result['rows'][0]['score']['ok']
    assert result['groups']['immediate']['latency']['metrics']['saved_to_complete_ms']['p95'] == 3000


@pytest.mark.parametrize('fault', ['missing_usage', 'failed', 'missing_pair', 'source', 'timing', 'ranges', 'unknown_phase'])
def test_invalid_or_unfinished_observations_cannot_count_as_savings(modules, tmp_path, fault):
    report, _ = modules
    _, rows = directory(tmp_path)
    if fault == 'missing_usage':
        rows[1]['calls'][0]['usage'] = None
    elif fault == 'failed':
        rows[1]['status'] = 'failed'
    elif fault == 'missing_pair':
        rows.pop()
    elif fault == 'source':
        rows[1]['windows'][0]['output']['facts'].append(dict(source_ids=['unknown']))
    elif fault == 'timing':
        rows[1]['arrivals'][0]['saved_at'] = -1
    elif fault == 'ranges':
        rows[1]['commits'][0]['start'] = 1
    else:
        rows[1]['calls'][0]['phase'] = 'unknown'
    write_rows(tmp_path, rows)
    result = report.summarize(tmp_path)
    assert result['paired_observations'] == 0
    assert result['paired_savings']['total']['total_tokens'] is None
    assert len(result['excluded_pairs']) == 1
    if fault == 'missing_usage':
        assert result['groups']['buffered']['cost']['total']['total_tokens'] is None


def test_late_producer_samples_are_visible_but_not_latency_evidence(modules):
    report, _ = modules
    row = dict(arrivals=[dict(planned_at=0, generated_at=.3, entered_at=.4, returned_at=.5)], calls=[])
    result = report.latency([row])
    assert result['scheduling_outliers'] == 1
    assert result['metrics']['response_ms'] is None
    assert result['metrics']['producer_lag_ms']['p50'] == 300


def test_protocol_rejection_is_visible_even_when_provider_succeeded(modules, tmp_path):
    report, _ = modules
    _, rows = directory(tmp_path)
    rows[0]['status'] = 'failed'
    rows[0]['windows'][0].update(validated=False,error_code='extraction_invalid_response')
    rows[0]['arrivals'][0].update(status='failed',error_type='ExtractionResponseError')
    write_rows(tmp_path, rows)
    result = report.summarize(tmp_path)
    assert result['groups']['immediate']['cost']['extraction']['failures'] == 0
    assert result['groups']['immediate']['cost']['extraction']['rejected_windows'] == 1
    assert 'extraction_invalid_response' in result['rows'][0]['errors']
    assert result['paired_observations'] == 0


def test_grid_has_two_rounds_and_reverses_pair_order(modules):
    _, comparison = modules
    from buffered_replay import load_suite
    grid = comparison.ordered_grid(load_suite()['cases'], 2)
    assert len(grid) == len(set(grid)) == 48
    assert grid[:2] == [('burst-short','immediate',0), ('burst-short','buffered',0)]
    assert grid[24:26] == [('burst-short','buffered',1), ('burst-short','immediate',1)]


def test_frozen_runner_and_duplicate_results_fail_closed(modules, tmp_path):
    report, _ = modules
    _, rows = directory(tmp_path)
    write_rows(tmp_path, rows+rows[:1])
    with pytest.raises(ValueError, match='Duplicate'):
        report.summarize(tmp_path)
    manifest_path = tmp_path/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['runner_hashes'] = {'runner.py': hashlib.sha256(b'original').hexdigest()}
    manifest_path.write_text(json.dumps(manifest))
    (tmp_path/'runner.py').write_text('changed')
    with pytest.raises(ValueError, match='runner changed'):
        report.summarize(tmp_path)


@pytest.mark.parametrize('name', ['buffered_replay_report.py', 'extraction_quality.py'])
def test_report_rejects_a_different_frozen_reporting_version(modules, tmp_path, name):
    report, _ = modules
    _, rows = directory(tmp_path)
    rows[0]['calls'][0]['phase'] = 'summary'
    write_rows(tmp_path, rows)
    manifest_path = tmp_path/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    archived = b'raise RuntimeError("archived source must never be executed")\n'
    (tmp_path/name).write_bytes(archived)
    manifest['runner_hashes'][name] = hashlib.sha256(archived).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Use the original reporting checkout'):
        report.summarize(tmp_path)


def test_interrupted_child_retains_last_attempt_usage(modules, tmp_path):
    _, comparison = modules
    folder = tmp_path/'burst-short-immediate-0'
    folder.mkdir()
    entries = [dict(kind='call_started', observation=dict(call_id=0,status='started',provider_request=True,started_at=1)),
               dict(kind='call_finished', observation=dict(call_id=0,status='completed',provider_request=True,
                    started_at=1,ended_at=2,usage=dict(total_tokens=10))),
               dict(kind='call_started', observation=dict(call_id=1,status='started',provider_request=True,started_at=3))]
    (folder/'observations.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in entries)+'{', encoding='utf-8')
    result = comparison.interrupted_row(tmp_path, 'burst-short','immediate',0,'hash','child_timeout')
    assert result['status'] == 'timed_out'
    assert result['calls'][0]['usage']['total_tokens'] == 10
    assert result['calls'][1]['status'] == 'interrupted'


def test_resume_does_not_reset_unknown_usage_or_change_model(modules, tmp_path):
    _, comparison = modules
    manifest = dict(extractor='deepseek-chat')
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='original model'):
        comparison.compare(tmp_path, resume=True)


def test_missing_usage_stops_new_children_and_resume_preserves_ledger(modules, monkeypatch, tmp_path):
    _, comparison = modules
    from buffered_replay import load_suite
    from extraction_budget import Budget
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-test-key')
    revision = comparison.subprocess.check_output(
        ['git', 'rev-parse', comparison.DEFAULT_RUNTIME], cwd=comparison.ROOT, text=True).strip()
    def export(ref, target):
        target.mkdir()
        return revision
    monkeypatch.setattr(comparison, 'export', export)
    launched = []
    original_run = comparison.subprocess.run
    def child(command, **kwargs):
        from types import SimpleNamespace
        if not isinstance(command, list) or not any('buffered_replay_runner.py' in str(v) for v in command):
            return original_run(command, **kwargs)
        launched.append(command)
        get = lambda name: command[command.index('--'+name)+1]
        out = Path(get('out'))
        case = next(c for c in load_suite()['cases'] if c['id'] == get('case'))
        fingerprint = comparison.suite_hash(out/'suite.json')
        row = observation(case,get('mode'),fingerprint)
        row['calls'][0]['usage'] = None
        budget = Budget(out/'budget.json')
        budget.claim()
        budget.finish(None)
        with (out/'results.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row)+'\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(comparison.subprocess, 'run', child)
    out = tmp_path/'run'
    first = comparison.compare(out,extractor='deepseek-chat',cases=['burst-short'],runs=1)
    assert first['terminal_reason'] == 'pilot_gate_failed'
    assert len(launched) == 1
    before = (out/'budget.json').read_bytes()
    comparison.compare(out,extractor='deepseek-chat',cases=['burst-short'],runs=1,resume=True)
    assert len(launched) == 1
    assert (out/'budget.json').read_bytes() == before


def test_journal_without_terminal_row_blocks_automatic_resume(modules, monkeypatch, tmp_path):
    _, comparison = modules
    out = tmp_path/'batch'
    original_run = comparison.subprocess.run
    launched = []
    def interrupted(command, **kwargs):
        if not isinstance(command, list) or not any('buffered_replay_runner.py' in str(v) for v in command):
            return original_run(command, **kwargs)
        launched.append(command)
        raise KeyboardInterrupt('synthetic controller interruption')
    monkeypatch.setattr(comparison.subprocess, 'run', interrupted)
    with pytest.raises(KeyboardInterrupt):
        comparison.compare(out,cases=['burst-short'],runs=1)
    result = comparison.compare(out,cases=['burst-short'],runs=1,resume=True)
    assert result['terminal_reason'] == 'orphan_observation'
    assert len(launched) == 1
    assert result['measured_rows'] == 0


@pytest.mark.parametrize('flags', [['--quality','--buffered-replay'],['--quality','--cases','burst-short']])
def test_cli_rejects_ambiguous_or_ignored_scope_flags(modules, monkeypatch, flags):
    import sys
    import window_extraction
    monkeypatch.setattr(sys,'argv',['window_extraction.py',*flags])
    with pytest.raises(SystemExit) as exc:
        window_extraction.main()
    assert exc.value.code == 2
