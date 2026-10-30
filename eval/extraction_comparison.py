"""One runner, isolated committed implementations, and a bounded staged comparison."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from importlib.metadata import version
import platform
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

from extraction_budget import Budget
from extraction_quality import aggregate, load_suite, score_case, suite_hash, validate_rows

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent


def export(ref, target):
    revision = subprocess.check_output(['git', 'rev-parse', '--verify', ref + '^{commit}'], cwd=ROOT, text=True).strip()
    archive = target.with_suffix('.zip')
    subprocess.run(['git', 'archive', '--format=zip', '--output', str(archive), revision, 'mnemotrace'],
                   cwd=ROOT, check=True)
    target.mkdir()
    with ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if not (target / name).resolve().is_relative_to(target.resolve()):
                raise ValueError('Archive path escaped selected implementation')
        zipped.extractall(target)
    return revision


def summarize(directory):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    path = directory / 'results.jsonl'
    rows = [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    gold = {c['id']: c for c in load_suite(manifest['suite'])['cases']}
    if suite_hash(manifest['suite']) != manifest['suite_hash']:
        raise ValueError('Frozen suite content changed')
    for row in rows:
        stored = {f['id']: f for s in row.get('snapshots', {}).values() for f in s['facts']}
        for fact in row.get('final', {}).get('facts', []):
            if fact['id'] in stored:
                fact.setdefault('source', stored[fact['id']]['source'])
        # Earlier runners logged cited IDs/content but not copied character offsets.
        # Recover only from the actual recorded model input, never from inferred text matches.
        spans = {}
        for call in row.get('calls', []):
            if call['phase'] != 'extraction':
                continue
            try:
                payload = json.JSONDecoder().raw_decode(call['prompt'].split('MNEMOTRACE_WINDOW\n', 1)[1])[0]
                spans.update((s['id'], s) for s in (*payload['pending'], *payload['context']))
            except (ValueError, KeyError, IndexError):
                continue
        for item in (*row.get('raw', {}).get('facts', []), *row.get('raw', {}).get('working', [])):
            for cited, ref in zip(item.get('spans', []), item.get('source_ids', [])):
                if ref in spans:
                    cited.update(start=spans[ref]['start'], end=spans[ref]['end'])
        # Replay scoring from original observations, never trust previously computed scores.
        row['score'] = score_case(gold[row['case_id']], row.get('final', {}))
        row['raw_score'] = score_case(gold[row['case_id']], row.get('raw', {}), raw=True)
    errors = validate_rows(rows, [tuple(k) for k in manifest['expected']], manifest['suite_hash'])
    groups = defaultdict(list)
    for row in rows:
        if row['status'] != 'not_run':
            groups[(row['revision'], row['mode'])].append(row)
    summaries = {}
    for (revision, mode), group in groups.items():
        phases = defaultdict(list)
        for row in group:
            for call in row.get('calls', []):
                phases[call['phase']].append(call)
        phase_results = {}
        for phase, calls in phases.items():
            usages = [c.get('usage', {}).get('total_tokens') if c.get('usage') else None for c in calls
                      if c.get('provider_request')]
            latencies = sorted(c['latency_ms'] for c in calls if c.get('provider_request'))
            phase_results[phase] = dict(attempts=len(calls), provider_requests=len(usages),
                failures=sum(c['status'] != 'completed' for c in calls),
                total_tokens=sum(usages) if all(u is not None for u in usages) else None,
                usage_unreported=sum(u is None for u in usages),
                p50_ms=latencies[(len(latencies)-1)//2] if latencies else None,
                p95_ms=latencies[math.ceil(.95*len(latencies))-1] if latencies else None)
            for field in ('prompt_tokens', 'completion_tokens'):
                values = [c.get('usage', {}).get(field) if c.get('usage') else None for c in calls
                          if c.get('provider_request')]
                phase_results[phase][field] = sum(values) if all(v is not None for v in values) else None
        ack = sorted(a['ms'] for r in group for a in r.get('acknowledgements', []))
        elapsed = sorted(r['elapsed_ms'] for r in group)
        def percentiles(values):
            return dict(p50_ms=values[math.ceil(.5*len(values))-1],
                        p95_ms=values[math.ceil(.95*len(values))-1]) if values else None
        summaries[revision + '/' + mode] = dict(final=aggregate(group),
            raw=aggregate([dict(row, score=row['raw_score']) for row in group]), phases=phase_results,
            acknowledgement=percentiles(ack), scenario=percentiles(elapsed))
        waits = []
        for row in group:
            accepted = {a['event']: a['confirmed_at'] for a in row.get('acknowledgements', [])}
            event_ids = {e['at']: e['id'] for e in gold[row['case_id']]['events']}
            for call in row.get('calls', []):
                if call['phase'] != 'extraction' or 'started_at' not in call:
                    continue
                try:
                    payload = json.JSONDecoder().raw_decode(call['prompt'].split('MNEMOTRACE_WINDOW\n', 1)[1])[0]
                    times = [accepted[event_ids[s['event_time']]] for s in payload['pending']]
                except (ValueError, KeyError, IndexError):
                    continue
                waits.append(max(0, (call['started_at'] - min(times))*1000))
        summaries[revision + '/' + mode]['confirmed_to_window_start'] = percentiles(sorted(waits)) if mode == 'buffered' else None
    core = {c['id'] for c in load_suite(manifest['suite'])['cases'] if c['core']}
    candidate_core = [r for r in rows if r['revision'] == 'candidate' and r['case_id'] in core]
    core_passed = len(candidate_core) == len(core)*2*3 and all(passed(r) for r in candidate_core)
    nondecreasing = True
    severe_regressions = []
    def severe(row):
        score = row['score']
        return bool(score['unexpected'] or not score['identity_ok'] or any(
            not d['time_ok'] or not d['history_ok'] for d in score['facts']))
    reference = {(r['mode'], r['case_id'], r['run']): r for r in rows if r['revision'] == 'baseline'}
    for row in rows:
        before = reference.get((row['mode'], row['case_id'], row['run']))
        if row['revision'] == 'candidate' and row['status'] == 'completed' and before and severe(row) and not severe(before):
            severe_regressions.append((row['mode'], row['case_id'], row['run']))
    for mode in ('immediate', 'buffered'):
        old, new = summaries.get('baseline/'+mode), summaries.get('candidate/'+mode)
        if old is None or new is None:
            nondecreasing = False
            continue
        for field in ('fact_precision', 'fact_recall', 'source_precision', 'source_recall', 'joint_recall',
                      'working_precision', 'working_recall', 'working_joint_recall'):
            a, b = old['final'][field], new['final'][field]
            if (a is None) != (b is None) or (a is not None and b < a):
                nondecreasing = False
    report = dict(kind='extraction_service_quality', schema_errors=errors,
        scorer_hash=hashlib.sha256((HARNESS/'extraction_quality.py').read_bytes()).hexdigest(),
        measured_rows=sum(r['status'] != 'not_run' for r in rows), planned_rows=len(manifest['expected']),
        execution_complete=not errors and all(r['status'] == 'completed' for r in rows),
        core_three_runs_passed=core_passed, metrics_nondecreasing=nondecreasing,
        runtime_accepted=not errors and core_passed and nondecreasing and not severe_regressions
            and all(r['status'] == 'completed' and r.get('restored') and not r.get('invalid_outputs')
                    and r.get('append_inflight', True) for r in rows)
            and not any(r['score']['unreviewed_working'] for r in rows if r['revision'] == 'candidate'),
        severe_regressions=severe_regressions,
        groups=summaries, differences=[dict(revision=r['revision'], mode=r['mode'], case_id=r['case_id'],
            run=r['run'], status=r['status'], score=r['score'], raw_score=r['raw_score'])
            for r in rows if r['status'] != 'not_run' and not passed(r)])
    (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def passed(row):
    return (row['status'] == 'completed' and row['score']['ok'] and row['raw_score']['ok']
            and not row.get('invalid_outputs') and row.get('restored') and row.get('append_inflight', True))


def compare_quality(out, baseline, candidate, extractor, suite, *, max_calls=2000, max_tokens=3_000_000, seconds=3600,
                    budget_file=None):
    from mnemotrace.llm.providers import load_dotenv
    from extraction_runner import require_credentials
    load_dotenv()  # Never copy configuration files or credential values to output.
    require_credentials(extractor)
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    suite = Path(suite).resolve()
    cases = load_suite(suite)['cases']
    shutil.copyfile(suite, out / 'suite.json')
    suite = out / 'suite.json'
    sources = {}
    for label, ref in (('baseline', baseline), ('candidate', candidate)):
        sources[label] = export(ref, out / label)
    for name in ('extraction_runner.py', 'extraction_quality.py', 'extraction_budget.py', 'extraction_comparison.py'):
        shutil.copyfile(HARNESS / name, out / name)
    expected = [(label, mode, c['id'], run) for run in range(3) for c in cases if run == 0 or c['core']
                for label in sources for mode in ('immediate', 'buffered')]
    manifest = dict(kind='extraction_service_quality', sources=sources, suite=str(suite), suite_hash=suite_hash(suite),
        runner_hashes={name: hashlib.sha256((out/name).read_bytes()).hexdigest() for name in (
            'extraction_runner.py', 'extraction_quality.py', 'extraction_budget.py', 'extraction_comparison.py')},
        extractor=extractor, temperature=0, timeout=45, client_attempts=1,
        python=platform.python_version(), platform=platform.platform(), litellm_version=version('litellm'),
        environment=dict(storage='memory', embedder='hashing', buffered_enabled=True,
                         conflict_model=False, close_summarize=True, close_clear_working=False, close_outcomes=False),
        expected=expected, terminal_reason=None)
    def save_manifest():
        (out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save_manifest()
    budget_file = Path(budget_file).resolve() if budget_file else out/'budget.json'
    if not budget_file.exists():
        Budget.create(budget_file, calls=max_calls, tokens=max_tokens, seconds=seconds)
    manifest['budget_file'] = str(budget_file)
    save_manifest()
    stages = [(0, [c for c in cases if c['core']]), (0, [c for c in cases if not c['core']]),
              (1, [c for c in cases if c['core']]), (2, [c for c in cases if c['core']])]
    for stage, (run, chosen) in enumerate(stages):
        for label, mode in (('baseline', 'immediate'), ('candidate', 'immediate'),
                            ('candidate', 'buffered'), ('baseline', 'buffered')):
            command = [sys.executable, str(out/'extraction_runner.py'), '--root', str(out/label),
                '--suite', str(suite), '--out', str(out), '--budget', str(budget_file),
                '--revision', label, '--mode', mode, '--extractor', extractor,
                '--cases', ','.join(c['id'] for c in chosen), '--run', str(run)]
            with (out/f'child-{stage}-{label}-{mode}.log').open('w', encoding='utf-8') as log:
                try:
                    process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, timeout=seconds+60)
                except subprocess.TimeoutExpired:
                    manifest['terminal_reason'] = 'child_timeout'
                    break
            if process.returncode:
                manifest['terminal_reason'] = 'budget_or_child_failure'
                break
        rows = [json.loads(s) for s in (out/'results.jsonl').read_text(encoding='utf-8').splitlines()] if (out/'results.jsonl').exists() else []
        if manifest['terminal_reason']:
            break
        if stage == 0 and not all(passed(r) for r in rows if r['revision'] == 'candidate'):
            manifest['terminal_reason'] = 'core_gate_failed'
            break
    if manifest['terminal_reason'] is None:
        manifest['terminal_reason'] = 'finished'
    present = {(r['revision'], r['mode'], r['case_id'], r['run']) for r in rows}
    case_map = {c['id']: c for c in cases}
    with (out/'results.jsonl').open('a', encoding='utf-8') as stream:
        for key in expected:
            if key in present:
                continue
            label, mode, case_id, run = key
            row = dict(revision=label, mode=mode, case_id=case_id, run=run, suite_hash=manifest['suite_hash'],
                       status='not_run', error_code=manifest['terminal_reason'], restored=False,
                       score=score_case(case_map[case_id], {}), raw_score=score_case(case_map[case_id], {}, raw=True))
            stream.write(json.dumps(row, ensure_ascii=False)+'\n')
    save_manifest()
    return summarize(out)
