"""Frozen same-runtime comparisons with an independently bounded model budget."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version

from buffered_replay import PILOT, SUITE, load_suite
from buffered_replay_report import summarize, validate_observation
from extraction_budget import Budget
from extraction_comparison import export
from extraction_quality import score_case, suite_hash
from extraction_runner import require_credentials

def _optional_version(name):
    # Optional eval dependency: record its version when present, never fail the
    # harness just because litellm is not installed in this environment.
    try:
        return version(name)
    except PackageNotFoundError:
        return None


HARNESS = Path(__file__).parent
ROOT = HARNESS.parent
# Pin a main commit that contains both the historical file service and the window kernel.
DEFAULT_RUNTIME = '9bdc903'
FILES = ('buffered_replay.py', 'buffered_replay_runner.py', 'buffered_replay_report.py',
         'buffered_replay_comparison.py', 'extraction_quality.py', 'extraction_budget.py',
         'extraction_runner.py', 'extraction_comparison.py')


def ordered_grid(cases, runs):
    ordered = sorted(cases, key=lambda c: (c['id'] not in PILOT, PILOT.index(c['id']) if c['id'] in PILOT else c['id']))
    return [(case['id'], mode, run) for run in range(runs) for i, case in enumerate(ordered)
            for mode in (('immediate', 'buffered') if (i+run) % 2 == 0 else ('buffered', 'immediate'))]


def pilot_errors(row, case, fingerprint):
    errors = validate_observation(row, case, fingerprint)
    if row['status'] != 'completed' or not row['restored']:
        errors.append('incomplete_execution')
    if any(c['phase'] not in {'extraction', 'summary'} or c.get('provider_request') and
           (c.get('usage') or {}).get('total_tokens') is None for c in row['calls']):
        errors.append('incomplete_metering')
    score = score_case(case, row['final'])
    if any(not d['source_exact'] for d in (*score['facts'], *score['working'])):
        errors.append('wrong_gold_source')
    return errors


def interrupted_row(out, case_id, mode, run, fingerprint, reason):
    """Retain paid attempts even when the child never reaches result.json."""
    directory = out/f'{case_id}-{mode}-{run}'
    observations = directory/'observations.jsonl'
    calls, windows, commits, selections = {}, [], [], []
    if observations.exists():
        for line in observations.read_text(encoding='utf-8').splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                break  # Only a crash-truncated final journal line may be incomplete.
            kind, row = entry['kind'], entry['observation']
            if kind in {'call_started', 'call_finished'}:
                calls[row['call_id']] = row
            elif kind == 'window_finished':
                windows.append(row)
            elif kind == 'committed_range':
                commits.append(row)
            elif kind == 'selected_window':
                selections.append(row)
    for call in calls.values():
        if call['status'] == 'started':
            call['status'] = 'interrupted'
            call['ended_at'] = call['started_at']  # Not a completed latency sample.
    return dict(case_id=case_id, mode=mode, run=run, suite_hash=fingerprint, status='timed_out' if reason == 'child_timeout' else 'failed',
        errors=[reason], restored=False, arrivals=[], calls=list(calls.values()), windows=windows,
        commits=commits, selections=selections, final={}, raw={}, budgets={}, inflight_observed=False)


def compare(out, *, runtime=DEFAULT_RUNTIME, extractor=None, cases=None, runs=2, pilot_only=False, resume=False,
            max_calls=2000, max_tokens=3_000_000, max_seconds=14400):
    if runs < 1:
        raise ValueError('runs must be positive')
    if extractor:
        from mnemotrace.llm.providers import load_dotenv
        load_dotenv()
        require_credentials(extractor)
    out = Path(out).resolve()
    if resume:
        manifest = json.loads((out/'manifest.json').read_text(encoding='utf-8'))
        if manifest['extractor'] != extractor:
            raise ValueError('Resume must use the original model setting')
        if manifest['runs'] != runs or cases is not None and set(cases) != {key[0] for key in manifest['expected']}:
            raise ValueError('Resume must use the original case grid')
        revision = subprocess.check_output(['git', 'rev-parse', '--verify', runtime+'^{commit}'], cwd=ROOT, text=True).strip()
        if revision != manifest['runtime_sha']:
            raise ValueError('Resume must use the original runtime')
        for name, fingerprint in manifest['runner_hashes'].items():
            if hashlib.sha256((HARNESS/name).read_bytes()).hexdigest() != fingerprint:
                raise ValueError('Cannot resume with a changed harness')
        summarize(out)  # Validate original evidence before adding anything.
    else:
        suite = load_suite()
        wanted = set(cases or [c['id'] for c in suite['cases']])
        if not wanted or not wanted <= {c['id'] for c in suite['cases']}:
            raise ValueError('Unknown replay case')
        selected = [c for c in suite['cases'] if c['id'] in wanted]
        out.mkdir(parents=True, exist_ok=False)
        revision = export(runtime, out/'runtime')
        shutil.copyfile(SUITE, out/'suite.json')
        for name in FILES:
            shutil.copyfile(HARNESS/name, out/name)
        manifest = dict(kind='buffered_replay', runtime_sha=revision, suite_hash=suite_hash(out/'suite.json'),
            runner_hashes={n: hashlib.sha256((out/n).read_bytes()).hexdigest() for n in FILES},
            harness_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            harness_diff=subprocess.check_output(['git', 'diff', '--', 'eval', 'tests/test_buffered_replay*'],
                                                 cwd=ROOT, text=True, encoding='utf-8'),
            python=platform.python_version(), platform=platform.platform(),
            litellm_version=_optional_version('litellm') if extractor else None, extractor=extractor, runs=runs,
            settings=dict(temperature=0, client_attempts=1, request_timeout=45, storage='memory', embedder='hashing',
                          close_summarize=True, close_clear_working=False, close_outcomes=False),
            expected=ordered_grid(selected, runs), terminal_reason='created')
        Budget.create(out/'budget.json', calls=max_calls, tokens=max_tokens, seconds=max_seconds)
    suite = {c['id']: c for c in load_suite(out/'suite.json')['cases']}
    manifest_path = out/'manifest.json'
    def save():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    result_path = out/'results.jsonl'
    rows = [json.loads(s) for s in result_path.read_text(encoding='utf-8').splitlines()] if result_path.exists() else []
    present = {(r['case_id'], r['mode'], r['run']) for r in rows}
    # An interrupted started pair cannot be rerun silently; logs may contain paid attempts.
    orphan = [p for p in out.glob('child-*.log')
              if not any(p.name == f'child-{r["case_id"]}-{r["mode"]}-{r["run"]}.log' for r in rows)]
    manifest['terminal_reason'] = 'orphan_observation' if orphan else 'running'
    for case_id, mode, run in manifest['expected']:
        if manifest['terminal_reason'] != 'running':
            break
        if (case_id, mode, run) in present:
            continue
        if pilot_only and (case_id not in PILOT or run != 0):
            manifest['terminal_reason'] = 'pilot_only'
            break
        pilot = [r for r in rows if r['case_id'] in PILOT and r['run'] == 0]
        if any(pilot_errors(r, suite[r['case_id']], manifest['suite_hash']) for r in pilot):
            manifest['terminal_reason'] = 'pilot_gate_failed'
            break
        budget = json.loads((out/'budget.json').read_text(encoding='utf-8'))
        if extractor and (budget['missing_usage'] or budget['inflight']):
            manifest['terminal_reason'] = 'unknown_usage_or_inflight'
            break
        if extractor:
            import time
            if (budget['calls'] >= budget['limits']['calls'] or budget['tokens'] >= budget['limits']['tokens']
                    or time.time()-budget['started'] >= budget['limits']['seconds']):
                manifest['terminal_reason'] = 'budget_stopped'
                break
        command = [sys.executable, str(out/'buffered_replay_runner.py'), '--root', str(out/'runtime'),
                   '--out', str(out), '--suite', str(out/'suite.json'), '--budget', str(out/'budget.json'),
                   '--case', case_id, '--mode', mode, '--run', str(run)]
        if extractor:
            command += ['--extractor', extractor]
        # The runner owns its directory; keep launch logs outside it until completion.
        log_path = out/f'child-{case_id}-{mode}-{run}.log'
        with log_path.open('x', encoding='utf-8') as log:
            try:
                process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, timeout=900)
            except subprocess.TimeoutExpired:
                manifest['terminal_reason'] = 'child_timeout'
                process = None
        if process is not None and process.returncode:
            manifest['terminal_reason'] = 'child_failure'
        if manifest['terminal_reason'] != 'running':
            existing = [json.loads(s) for s in result_path.read_text(encoding='utf-8').splitlines()] if result_path.exists() else []
            if not any((r['case_id'], r['mode'], r['run']) == (case_id, mode, run) for r in existing):
                row = interrupted_row(out, case_id, mode, run, manifest['suite_hash'], manifest['terminal_reason'])
                with result_path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False)+'\n')
            break
        rows = [json.loads(s) for s in result_path.read_text(encoding='utf-8').splitlines()]
        save()
        report = summarize(out)
        print(json.dumps(dict(case_id=case_id, mode=mode, run=run,
                              measured_rows=report['measured_rows'], status=rows[-1]['status'])), flush=True)
        if any(c['status'] == 'budget_stopped' for c in rows[-1]['calls']):
            manifest['terminal_reason'] = 'budget_stopped'
    if manifest['terminal_reason'] == 'running':
        pilot = [r for r in rows if r['case_id'] in PILOT and r['run'] == 0]
        manifest['terminal_reason'] = ('pilot_gate_failed' if any(pilot_errors(r, suite[r['case_id']], manifest['suite_hash'])
                                       for r in pilot) else 'finished')
    save()
    return summarize(out)
