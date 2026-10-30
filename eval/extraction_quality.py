"""Frozen extraction gold and deterministic scoring, independent of runtime/model adapters."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata

SUITE = Path(__file__).with_name('fixtures') / 'extraction_quality.json'


def load_suite(path=SUITE):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    # Revision 1 remains readable so its immutable measured evidence can be rescored.
    if data.get('version') not in {1, 2} or not isinstance(data.get('cases'), list):
        raise ValueError('Invalid extraction suite')
    ids = set()
    for case in data['cases']:
        if (not isinstance(case['id'], str) or re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', case['id']) is None
                or case['id'] in ids or case['split'] not in {'development', 'held_out'}):
            raise ValueError('Duplicate case or invalid split')
        ids.add(case['id'])
        events = {e['id'] for e in case['events']}
        if len(events) != len(case['events']):
            raise ValueError('Duplicate event')
        for group in ('facts', 'working'):
            for item in case[group]:
                if not item['sources'] or any(not set(s) <= events for s in item['sources']):
                    raise ValueError('Invalid gold sources')
                if item['at'] not in events:
                    raise ValueError('Invalid gold time')
                if any(key not in events for key in item.get('allowed_at', [])):
                    raise ValueError('Invalid alternative time')
    return data


def suite_hash(path=SUITE):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize(value):
    return ' '.join(unicodedata.normalize('NFC', value).strip().split())


def _triple(row, aliases):
    def entity(value):
        value = normalize(value)
        seen = set()
        while value in aliases and value not in seen:
            seen.add(value)
            value = aliases[value]
        return value
    return (entity(row['subject']), normalize(row['predicate']).lower(), entity(row['object']))


def _matching(predicted, expected, accepts):
    """Maximum one-to-one matching; order does not let a duplicate consume two golds."""
    owners = {}
    def visit(p, seen):
        for g in range(len(expected)):
            if g in seen or not accepts(predicted[p], expected[g]):
                continue
            seen.add(g)
            if g not in owners or visit(owners[g], seen):
                owners[g] = p
                return True
        return False
    for p in range(len(predicted)):
        visit(p, set())
    return sorted((p, g) for g, p in owners.items())


def _sources(actual, alternatives):
    actual = set(actual)
    best = min((set(s) for s in alternatives),
               key=lambda s: (len(actual ^ s), tuple(sorted(s))))
    return dict(source_tp=len(actual & best), source_predicted=len(actual), source_expected=len(best),
                source_missing=sorted(best - actual), source_extra=sorted(actual - best),
                source_exact=actual == best)


def score_case(case, result, *, raw=False):
    """Raw mode scores emitted items only; omissions are measured on final state."""
    aliases = {'user': 'u', '用户': 'u', '我': 'u', **case.get('aliases', {})}
    for user, names in case.get('identity', {}).items():
        for name in names:
            aliases[name] = user
    events = {e['id']: e for e in case['events']}
    gold = case.get('raw_facts', case['facts']) if raw else case['facts']
    facts = result.get('facts', [])
    accepts = lambda row, g: row.get('user', 'u') == events[g['at']]['user'] and _triple(row, aliases) in {
        _triple(dict(zip(('subject', 'predicate', 'object'), t)), aliases) for t in g['variants']}
    pairs = _matching(facts, gold, accepts)
    if raw:
        # The same fact can be legitimately repeated in separate pending windows.
        pairs = [(p, next(g for g in range(len(gold)) if accepts(row, gold[g])))
                 for p, row in enumerate(facts) if any(accepts(row, g) for g in gold)]
    details = []
    for p, g in pairs:
        row, expected = facts[p], gold[g]
        alternatives = expected.get('raw_sources', expected['sources']) if raw else expected['sources']
        detail = dict(predicted=p, gold=expected['id'], **_sources(row.get('sources', []), alternatives))
        detail['time_ok'] = expected.get('manual', False) or row.get('valid_at') in {
            events[key]['at'] for key in expected.get('allowed_at', [expected['at']])}
        if not raw:
            replacement = expected.get('replaced_by')
            detail['history_ok'] = (row.get('invalid_at') == events[gold[next(
                i for i, f in enumerate(gold) if f['id'] == replacement)]['at']]['at']
                if replacement else row.get('invalid_at') is None)
            if replacement:
                successor = next((r for r in facts if accepts(r, next(f for f in gold if f['id'] == replacement))), None)
                detail['history_ok'] &= successor is not None and successor.get('supersedes') == row.get('id')
        else:
            detail['history_ok'] = True
        if expected.get('manual') and not raw:
            detail['history_ok'] &= row.get('source') == 'user'
        evidence_ok = True
        if raw and 'spans' in row:
            for event, quote in expected.get('evidence', {}).items():
                cited = [s for s in row['spans'] if s['event'] == event and 'start' in s and 'end' in s]
                original = events[event]['text']
                starts = [i for i in range(len(original)) if original.startswith(quote, i)]
                evidence_ok &= any(all(any(s['start'] <= pos < s['end'] for s in cited)
                                       for pos in range(i, i + len(quote))) for i in starts)
        detail['evidence_ok'] = evidence_ok
        details.append(detail)
    matched_p = {p for p, _ in pairs}
    matched_g = {g for _, g in pairs}
    working = result.get('working', [])
    def accepts_work(row, g):
        event = events[g['at']]
        return (row.get('user', 'u') == event['user'] and row.get('session', 's') == event['session']
                and normalize(row['content']) in {normalize(c) for c in g['contents']})
    wpairs = _matching(working, case['working'], accepts_work)
    wd = []
    for p, g in wpairs:
        row, expected = working[p], case['working'][g]
        wd.append(dict(predicted=p, gold=expected['id'], kind_ok=row.get('kind') in expected['kinds'],
                       time_ok=row.get('event_time') == events[expected['at']]['at'],
                       **_sources(row.get('sources', []), expected['sources'])))
    identity_ok = all(result.get('identity', {}).get(user) in names
                      for user, names in case.get('identity', {}).items()) if not raw else True
    unexpected = [p for p in range(len(facts)) if p not in matched_p]
    weaker = [p for p in unexpected if any(_triple(facts[p], aliases) in {
        _triple(dict(zip(('subject', 'predicate', 'object'), t)), aliases)
        for t in g.get('weaker_variants', [])} for g in gold)]
    counts = dict(fact_tp=len(pairs), fact_predicted=len(facts), fact_expected=0 if raw else len(gold),
                  working_tp=len(wpairs), working_predicted=len(working), working_expected=0 if raw else len(case['working']),
                  source_tp=sum(d['source_tp'] for d in (*details, *wd)),
                  source_predicted=sum(d['source_predicted'] for d in (*details, *wd)),
                  source_expected=sum(d['source_expected'] for d in (*details, *wd)),
                  joint_correct=sum(d['source_exact'] and d['time_ok'] and d['history_ok'] and d['evidence_ok']
                                    for d in details),
                  working_joint_correct=sum(d['source_exact'] and d['kind_ok'] and d['time_ok'] for d in wd))
    ok = (not unexpected and (raw or len(matched_g) == len(gold)) and identity_ok
          and all(d['source_exact'] and d['time_ok'] and d['history_ok'] and d['evidence_ok'] for d in details)
          and len(wpairs) == len(working) and (raw or len(wpairs) == len(case['working']))
          and all(d['source_exact'] and d['kind_ok'] and d['time_ok'] for d in wd))
    return dict(ok=bool(ok), counts=counts, facts=details, working=wd, identity_ok=identity_ok,
                omitted=[] if raw else [g['id'] for i, g in enumerate(gold) if i not in matched_g],
                unexpected=unexpected, specificity_loss=weaker,
                unreviewed_working=[i for i in range(len(working)) if i not in {p for p, _ in wpairs}])


def aggregate(rows):
    counts = Counter()
    for row in rows:
        counts.update(row['score']['counts'])
    def ratio(a, b):
        return counts[a] / counts[b] if counts[b] else None
    return dict(cases=len(rows), passed=sum(row['status'] == 'completed' and row['score']['ok'] for row in rows),
                failures=sum(row['status'] != 'completed' for row in rows), counts=dict(counts),
                fact_precision=ratio('fact_tp', 'fact_predicted'), fact_recall=ratio('fact_tp', 'fact_expected'),
                source_precision=ratio('source_tp', 'source_predicted'), source_recall=ratio('source_tp', 'source_expected'),
                joint_recall=ratio('joint_correct', 'fact_expected'),
                working_precision=ratio('working_tp', 'working_predicted'),
                working_recall=ratio('working_tp', 'working_expected'),
                working_joint_recall=ratio('working_joint_correct', 'working_expected'))


def validate_rows(rows, expected_keys, fingerprint):
    """Completion validation is separate from semantic quality: failed rows stay visible."""
    errors, seen = [], set()
    for row in rows:
        key = (row.get('revision'), row.get('mode'), row.get('case_id'), row.get('run'))
        if key in seen:
            errors.append('duplicate result: ' + repr(key))
        seen.add(key)
        if row.get('suite_hash') != fingerprint:
            errors.append('suite mismatch: ' + repr(key))
        if row.get('status') not in {'completed', 'failed', 'budget_stopped', 'timed_out', 'not_run'}:
            errors.append('nonterminal result: ' + repr(key))
    if seen != set(expected_keys):
        errors.append('result grid mismatch')
    return errors
