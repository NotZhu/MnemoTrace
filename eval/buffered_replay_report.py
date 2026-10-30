"""Recompute replay quality, cost pairs and latency from immutable observations."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

from buffered_replay import load_suite
from extraction_quality import aggregate, score_case, suite_hash


def percentiles(values):
    values = sorted(values)
    return dict(n=len(values), p50=values[math.ceil(len(values)*.5)-1],
                p95=values[math.ceil(len(values)*.95)-1]) if values else None


def costs(rows):
    calls = [c for r in rows for c in r.get('calls', [])]
    result = {}
    for phase in ('extraction', 'summary', 'unknown', 'total'):
        chosen = [c for c in calls if phase == 'total' or c['phase'] == phase]
        provider = [c for c in chosen if c.get('provider_request')]
        group = dict(attempts=len(chosen), provider_requests=len(provider),
                     failures=sum(c['status'] not in {'completed', 'budget_stopped'} for c in chosen),
                     budget_stops=sum(c['status'] == 'budget_stopped' for c in chosen))
        repeats = Counter((r['case_id'], r['mode'], r['run'], c.get('window')) for r in rows
                          for c in r.get('calls', []) if c['phase'] == 'extraction' and phase in {'extraction', 'total'})
        group['repeated_window_attempts'] = sum(n-1 for n in repeats.values())
        for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            values = [(c.get('usage') or {}).get(field) for c in provider]
            group[field] = sum(values) if values and all(v is not None for v in values) else None
        group['missing_usage'] = sum((c.get('usage') or {}).get('total_tokens') is None for c in provider)
        result[phase] = group
    return result


def latency(rows):
    result = {k: [] for k in ('client_queue_ms', 'response_ms', 'saved_to_start_ms', 'model_ms',
                              'saved_to_complete_ms', 'offered_to_complete_ms', 'producer_lag_ms')}
    excluded = 0
    for row in rows:
        for arrival in row.get('arrivals', []):
            lag = (arrival['generated_at']-arrival['planned_at'])*1000
            result['producer_lag_ms'].append(lag)
            if lag > 250:
                excluded += 1
                continue
            result['client_queue_ms'].append((arrival['entered_at']-arrival['generated_at'])*1000)
            result['response_ms'].append((arrival['returned_at']-arrival['entered_at'])*1000)
            if 'saved_at' in arrival and 'completed_at' in arrival:
                result['saved_to_complete_ms'].append((arrival['completed_at']-arrival['saved_at'])*1000)
                result['offered_to_complete_ms'].append((arrival['completed_at']-arrival['planned_at'])*1000)
                starts = [w['started_at'] for w in row.get('windows', []) if any(
                    s['episode_id'] == arrival.get('episode_id') for s in w['pending'])]
                if starts:
                    result['saved_to_start_ms'].append((min(starts)-arrival['saved_at'])*1000)
        if not any((a['generated_at']-a['planned_at'])*1000 > 250 for a in row.get('arrivals', [])):
            result['model_ms'].extend((c.get('provider_ended_at', c['ended_at'])-
                                      c.get('provider_started_at', c['started_at']))*1000 for c in row.get('calls', [])
                                      if c.get('provider_request') and c['status'] not in {'budget_stopped', 'interrupted'})
    return dict(metrics={k: percentiles(v) for k, v in result.items()}, scheduling_outliers=excluded)


def validate_observation(row, case, fingerprint):
    errors = []
    if row.get('suite_hash') != fingerprint:
        errors.append('suite_hash')
    if row['status'] != 'completed':
        return errors  # Failures are visible, not malformed successes.
    arrivals = [a for a in row['arrivals'] if a['kind'] == 'event']
    if Counter(a['event'] for a in arrivals) != Counter(e['id'] for e in case['events']):
        errors.append('event_grid')
    if not row['restored']:
        errors.append('restore')
    by_episode = {a['episode_id']: a for a in arrivals if 'episode_id' in a}
    known = {e['id']: e for e in case['events']}
    for arrival in arrivals:
        values = [arrival.get(k) for k in ('planned_at', 'generated_at', 'entered_at', 'saved_at', 'returned_at')]
        if any(v is None or not math.isfinite(v) for v in values) or values != sorted(values):
            errors.append('event_timing')
        if 'completed_at' not in arrival or arrival['completed_at'] < arrival.get('saved_at', math.inf):
            errors.append('completion_timing')
        ranges = sorted((c['start'], c['end']) for c in row['commits'] if c['event'] == arrival['event'])
        expected, cursor = len(known[arrival['event']]['text']), 0
        for start, end in ranges:
            if start != cursor or end <= start:
                errors.append('range_gap_or_repeat')
            cursor = end
        if cursor != expected:
            errors.append('incomplete_range')
    for window in row['windows']:
        owners = [known[by_episode[s['episode_id']]['event']] for s in window['pending']
                  if s['episode_id'] in by_episode]
        if len(owners) != len(window['pending']) or len({(e['user'], e['session'], e['scope']) for e in owners}) != 1:
            errors.append('window_isolation')
        if owners and any(s['episode_id'] not in by_episode or
                (known[by_episode[s['episode_id']]['event']]['user'], known[by_episode[s['episode_id']]['event']]['session'])
                != (owners[0]['user'], owners[0]['session']) for s in window['context']):
            errors.append('context_isolation')
        if window['input_bytes'] > row['budgets']['input_bytes']:
            errors.append('input_budget')
        if not window.get('bytes'):
            errors.append('missing_byte_accounting')
        if window.get('bytes') and sum(v for k, v in window['bytes'].items() if k != 'total') != window['input_bytes']:
            errors.append('byte_accounting')
        allowed = {s['id'] for s in (*window['pending'], *window['context'])}
        pending = {s['id'] for s in window['pending']}
        for item in (*window.get('output', {}).get('facts', []), *window.get('output', {}).get('working', [])):
            if not set(item['source_ids']) <= allowed or not pending.intersection(item['source_ids']):
                errors.append('source_outside_window')
    for call in row['calls']:
        if call['phase'] not in {'extraction', 'summary'}:
            errors.append('unknown_model_phase')
    for span in [s for w in row['windows'] for s in w['context']]:
        if span['episode_id'] not in by_episode:
            errors.append('unknown_context')
    window_ids = {w['window_id'] for w in row['windows'] if w.get('validated')}
    if any(c['window_id'] not in window_ids for c in row['commits']):
        errors.append('unknown_commit_window')
    if any(c.get('window') not in {w['window_id'] for w in row['windows']} for c in row['calls']
           if c['phase'] == 'extraction'):
        errors.append('unknown_call_window')
    return sorted(set(errors))


def severe(row):
    score = row['score']
    return (not score['identity_ok'] or any(not d['time_ok'] or not d['history_ok'] or not d['source_exact']
                                          for d in score['facts'])
            or bool(row.get('observation_errors')))


def summarize(directory):
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    suite = directory/'suite.json'
    fingerprint = suite_hash(suite)
    if fingerprint != manifest['suite_hash']:
        raise ValueError('Frozen replay suite changed')
    for name, expected in manifest['runner_hashes'].items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != expected:
            raise ValueError('Frozen replay runner changed')
    cases = {c['id']: c for c in load_suite(suite)['cases']}
    path = directory/'results.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    keys = [(r['case_id'], r['mode'], r['run']) for r in rows]
    expected = [tuple(k) for k in manifest['expected']]
    if len(set(keys)) != len(keys) or not set(keys) <= set(expected):
        raise ValueError('Duplicate or unexpected replay result')
    for row in rows:
        case = cases[row['case_id']]
        row['score'] = score_case(case, row['final'])
        row['raw_score'] = score_case(case, row['raw'], raw=True)
        row['observation_errors'] = validate_observation(row, case, fingerprint)
    groups, round_groups, pairs, excluded, regressions = {}, {}, [], [], []
    for mode in ('immediate', 'buffered'):
        chosen = [r for r in rows if r['mode'] == mode]
        groups[mode] = dict(cost=costs(chosen), quality=aggregate(chosen), latency=latency(chosen))
        groups[mode]['windows'] = dict(
            count=sum(len(r['windows']) for r in chosen),
            utilization=percentiles([w['input_bytes']/r['budgets']['input_bytes'] for r in chosen
                                    for w in r['windows'] if r.get('budgets')]),
            events_per_window=percentiles([len({s['episode_id'] for s in w['pending']}) for r in chosen for w in r['windows']]),
            byte_parts=dict(Counter({key: sum(w.get('bytes', {}).get(key, 0) for r in chosen for w in r['windows'])
                                     for key in ('system', 'body', 'context', 'metadata', 'template')})),
            triggers=dict(Counter(c for r in chosen for s in r['selections'] for c in s['conditions'])))
        for run in range(manifest['runs']):
            subset = [r for r in chosen if r['run'] == run]
            round_groups[f'{mode}/{run}'] = dict(cost=costs(subset), quality=aggregate(subset), latency=latency(subset))
    index = {(r['case_id'], r['mode'], r['run']): r for r in rows}
    for case_id, run in dict.fromkeys((key[0], key[2]) for key in expected):
        old, new = index.get((case_id, 'immediate', run)), index.get((case_id, 'buffered', run))
        complete = all(r and r['status'] == 'completed' and not r['observation_errors']
                       and not costs([r])['total']['missing_usage'] for r in (old, new))
        if not complete:
            excluded.append(dict(case_id=case_id, run=run))
            continue
        pairs.append((old, new))
        if severe(new) and not severe(old):
            regressions.append(dict(case_id=case_id, run=run))
    paired = {mode: costs([pair[i] for pair in pairs]) for i, mode in enumerate(('immediate', 'buffered'))}
    savings = {}
    for phase in ('extraction', 'summary', 'total'):
        savings[phase] = {}
        for field in ('attempts', 'prompt_tokens', 'completion_tokens', 'total_tokens'):
            a, b = paired['immediate'][phase][field], paired['buffered'][phase][field]
            savings[phase][field] = (a-b)/a if a not in (None, 0) and b is not None else None
    window_rows = [w for r in rows for w in r['windows']]
    nondecreasing = True
    quality = {m: aggregate([p[i] for p in pairs]) for i, m in enumerate(('immediate', 'buffered'))}
    for field in ('fact_precision', 'fact_recall', 'source_precision', 'source_recall', 'joint_recall',
                  'working_precision', 'working_recall', 'working_joint_recall'):
        a, b = quality['immediate'][field], quality['buffered'][field]
        if (a is None) != (b is None) or a is not None and b < a:
            nondecreasing = False
    complete = len(rows) == len(expected) and all(r['status'] == 'completed' and not r['observation_errors'] for r in rows)
    report = dict(kind='buffered_replay', planned_rows=len(expected), measured_rows=len(rows), execution_complete=complete,
        analysis_scorer_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        terminal_reason=manifest['terminal_reason'], synthetic=manifest['extractor'] is None,
        observation_errors=[dict(case_id=r['case_id'], mode=r['mode'], run=r['run'], errors=r['observation_errors'])
                            for r in rows if r['observation_errors']], groups=groups, rounds=round_groups,
        paired_cost=paired, paired_savings=savings, paired_observations=len(pairs), excluded_pairs=excluded,
        quality_nondecreasing=nondecreasing, severe_regressions=regressions,
        quality_maintained=bool(pairs) and nondecreasing and not regressions and not any(
            r['score']['unreviewed_working'] or r['score']['unexpected'] for pair in pairs for r in pair),
        windows=dict(count=len(window_rows), events_per_window=percentiles([len({s['episode_id'] for s in w['pending']}) for w in window_rows]),
            utilization=percentiles([w['input_bytes']/r['budgets']['input_bytes'] for r in rows for w in r['windows'] if r.get('budgets')]),
            triggers=dict(Counter(c for r in rows for s in r['selections'] for c in s['conditions']))),
        pairs=[dict(case_id=p[0]['case_id'], run=p[0]['run'],
                    cost={m: costs([p[i]]) for i, m in enumerate(('immediate', 'buffered'))}) for p in pairs],
        rows=[dict(case_id=r['case_id'], mode=r['mode'], run=r['run'], status=r['status'], errors=r['errors'],
                   score=r['score'], raw_score=r['raw_score'], cost=costs([r]), latency=latency([r]),
                   inflight_observed=r['inflight_observed']) for r in rows])
    (directory/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
