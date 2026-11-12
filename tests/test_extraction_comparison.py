"""Reports re-score observations and reject incomplete/forged success without model calls."""
from copy import deepcopy
import importlib
import itertools
import json
from pathlib import Path

import pytest


@pytest.fixture
def comparison(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'eval'))
    module = importlib.import_module('extraction_comparison')
    case = importlib.import_module('extraction_quality').load_suite()['cases'][0]
    suite = tmp_path/'suite.json'
    suite.write_text(json.dumps(dict(version=1, cases=[case])), encoding='utf-8')
    fingerprint = module.suite_hash(suite)
    expected = [(rev, mode, case['id'], run) for rev, mode, run in itertools.product(
        ('baseline', 'candidate'), ('immediate', 'buffered'), range(3))]
    (tmp_path/'manifest.json').write_text(json.dumps(dict(expected=expected, suite=str(suite),
        suite_hash=fingerprint)), encoding='utf-8')
    facts = [dict(id=g['id'], subject=g['variants'][0][0], predicate=g['variants'][0][1],
        object=g['variants'][0][2], user='u', sources=g['sources'][0], valid_at=case['events'][i]['at'],
        invalid_at=None, supersedes=[]) for i, g in enumerate(case['facts'])]
    final = dict(facts=facts, identity={})
    rows = [dict(revision=r, mode=m, case_id=c, run=n, suite_hash=fingerprint, status='completed',
        final=deepcopy(final), raw=deepcopy(final), restored=True, elapsed_ms=10,
        calls=[dict(phase='extraction', provider_request=True, status='completed',
                    usage=dict(total_tokens=5, prompt_tokens=3, completion_tokens=2), latency_ms=5)])
        for r, m, c, n in expected]
    def write():
        (tmp_path/'results.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
    write()
    return module, tmp_path, rows, write


def test_complete_same_observations_pass_and_phase_totals_are_recomputed(comparison):
    module, root, rows, write = comparison
    report = module.summarize(root)
    assert report['runtime_accepted'] and report['execution_complete']
    assert report['groups']['candidate/immediate']['phases']['extraction']['total_tokens'] == 15
    assert report['groups']['candidate/immediate']['raw']['fact_recall'] is None


def test_cached_success_cannot_cover_changed_observations(comparison):
    module, root, rows, write = comparison
    row = next(r for r in rows if r['revision'] == 'candidate')
    row['score'] = dict(ok=True)
    row['final']['facts'][0]['object'] = 'unsupported synthetic value'
    write()
    report = module.summarize(root)
    assert not report['runtime_accepted'] and report['severe_regressions']


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'not_run'])
def test_incomplete_or_duplicate_grid_cannot_report_acceptance(comparison, mutation):
    module, root, rows, write = comparison
    if mutation == 'duplicate':
        rows.append(deepcopy(rows[0]))
    elif mutation == 'missing':
        rows.pop()
    else:
        rows[0]['status'] = 'not_run'
    write()
    report = module.summarize(root)
    assert not report['runtime_accepted'] and not report['execution_complete']


def test_changed_gold_is_rejected_instead_of_reusing_old_scores(comparison):
    module, root, rows, write = comparison
    suite = root/'suite.json'
    suite.write_text(suite.read_text(encoding='utf-8')+' ', encoding='utf-8')
    with pytest.raises(ValueError, match='Frozen suite'):
        module.summarize(root)
