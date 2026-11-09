"""Extraction gold/scorer contracts; these tests do not measure model quality."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('quality', Path(__file__).resolve().parents[1] / 'eval/extraction_quality.py')
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


@pytest.fixture
def case():
    return deepcopy(next(c for c in quality.load_suite()['cases'] if c['id'] == 'reference-1'))


def predicted(case):
    return {'facts': [dict(id=g['id'], subject=g['variants'][0][0], predicate=g['variants'][0][1],
                          object=g['variants'][0][2], user='u', sources=g['sources'][0],
                          valid_at=next(e['at'] for e in case['events'] if e['id'] == g['at']),
                          invalid_at=None, supersedes=None) for g in case['facts']], }


def test_frozen_suite_has_all_categories_and_disjoint_splits():
    rows = quality.load_suite()['cases']
    assert len(rows) == len({c['id'] for c in rows}) == 40
    assert len({c['category'] for c in rows}) == 8
    assert sum(c['core'] for c in rows) == 8
    assert sum(c['split'] == 'development' for c in rows) == 24
    assert all(c['split'] == 'development' for c in rows if c['core'])


def test_correct_reference_and_equivalent_inverse_both_pass(case):
    result = predicted(case)
    assert quality.score_case(case, result)['ok']
    result['facts'][0].update(subject='林澄', predicate='older_sister_of', object='u')
    assert quality.score_case(case, result)['ok']


@pytest.mark.parametrize('mutation', ['missing_context', 'extra_source', 'wrong_direction', 'weaker', 'wrong_time', 'wrong_user'])
def test_quality_errors_cannot_be_hidden_by_valid_ids(case, mutation):
    result = predicted(case)
    row = result['facts'][1]
    if mutation == 'missing_context':
        row['sources'] = ['e2']
    elif mutation == 'extra_source':
        result['facts'][0]['sources'] = ['e1', 'e2']
    elif mutation == 'wrong_direction':
        row['subject'], row['object'] = row['object'], row['subject']
    elif mutation == 'weaker':
        result['facts'][0]['predicate'] = 'has_sister'
    elif mutation == 'wrong_user':
        row['user'] = 'v'
    else:
        row['valid_at'] += 1
    score = quality.score_case(case, result)
    assert not score['ok']
    if mutation == 'weaker':
        assert score['specificity_loss'] == [0]


def test_duplicate_cannot_satisfy_two_distinct_expected_facts(case):
    row = predicted(case)['facts'][0]
    score = quality.score_case(case, {'facts': [row, deepcopy(row)], })
    assert score['counts']['fact_tp'] == 1 and score['unexpected'] and score['omitted']


def test_empty_result_is_separate_from_missing_facts(case):
    assert not quality.score_case(case, {'facts': [], })['ok']
    case['facts'] = []
    score = quality.score_case(case, {'facts': [], })
    assert score['ok']
    digest = quality.aggregate([{'score': score, 'status': 'completed'}])
    assert digest['fact_precision'] is digest['fact_recall'] is None


def test_raw_evidence_must_be_in_the_actual_cited_fragment():
    case = deepcopy(next(c for c in quality.load_suite()['cases'] if c['id'] == 'long-2'))
    row = predicted(case)['facts'][0]
    row['spans'] = [{'event': 'e1', 'content': '无关的尾部文字'}]
    score = quality.score_case(case, {'facts': [row], }, raw=True)
    assert not score['ok'] and not score['facts'][0]['evidence_ok']


def test_raw_evidence_uses_character_coverage_not_concatenated_fragments():
    case = deepcopy(next(c for c in quality.load_suite()['cases'] if c['id'] == 'long-2'))
    case['events'][0]['text'] = '我喜欢绿茶。'
    row = predicted(case)['facts'][0]
    row['spans'] = [dict(event='e1', content='我喜', start=0, end=2),
                    dict(event='e1', content='绿茶。', start=3, end=6)]
    assert not quality.score_case(case, {'facts': [row], }, raw=True)['ok']
    row['spans'] = [dict(event='e1', content='我喜欢绿茶。', start=0, end=6)]
    assert quality.score_case(case, {'facts': [row], }, raw=True)['ok']


def test_identity_aliases_share_canonical_user_without_guessing_names():
    case = next(c for c in quality.load_suite()['cases'] if c['id'] == 'reference-2')
    result = predicted(case)
    result['facts'][0]['subject'] = 'u'
    result['identity'] = {'u': '林舟'}
    assert quality.score_case(case, result)['ok']
    result['facts'][0]['subject'] = '陌生人'
    assert not quality.score_case(case, result)['ok']


def test_raw_repetitions_have_precision_but_no_future_fact_recall(case):
    result = predicted(case)
    result['facts'] *= 2
    score = quality.score_case(case, result, raw=True)
    digest = quality.aggregate([dict(score=score, status='completed')])
    assert digest['fact_precision'] == 1 and digest['fact_recall'] is None


def test_manual_value_keeps_user_authority_without_transcript_sources():
    case = next(c for c in quality.load_suite()['cases'] if c['id'] == 'isolation-3')
    result = predicted(case)
    result['facts'][0]['source'] = 'user'
    assert quality.score_case(case, result)['ok']
    result['facts'][0]['source'] = 'extracted'
    assert not quality.score_case(case, result)['ok']


def test_corroboration_time_allows_original_or_joint_source_anchor_only():
    case = next(c for c in quality.load_suite()['cases'] if c['id'] == 'append-2')
    result = predicted(case)
    for event in case['events']:
        result['facts'][0]['valid_at'] = event['at']
        assert quality.score_case(case, result)['ok']
    result['facts'][0]['valid_at'] += 1
    assert not quality.score_case(case, result)['ok']


def test_history_chain_is_scored_not_just_live_fact_text():
    case = next(c for c in quality.load_suite()['cases'] if c['id'] == 'update-1')
    result = predicted(case)
    result['facts'][0]['invalid_at'] = case['events'][1]['at']
    result['facts'][1]['supersedes'] = 'f1'
    assert quality.score_case(case, result)['ok']
    result['facts'][1]['supersedes'] = None
    assert not quality.score_case(case, result)['ok']


def test_result_completeness_rejects_duplicates_missing_rows_and_wrong_suite():
    key = ('baseline', 'immediate', 'independent-1', 0)
    row = dict(zip(('revision', 'mode', 'case_id', 'run'), key), status='failed', suite_hash='fixed')
    assert not quality.validate_rows([row], [key], 'fixed')  # A visible failure is a terminal result.
    assert quality.validate_rows([row, row], [key], 'fixed')
    assert quality.validate_rows([], [key], 'fixed')
    assert quality.validate_rows([row], [key], 'other')
