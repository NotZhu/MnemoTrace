"""Isolated service measurement child. Load the selected implementation before its dependencies."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict
import functools
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

from extraction_budget import Budget, BudgetStopped
from extraction_quality import load_suite, score_case, suite_hash

PHASE = ContextVar('extraction_eval_phase', default='unknown')


def require_credentials(extractor):
    if (extractor in {'deepseek', 'deepseek-chat', 'deepseek-reasoner'} or extractor.startswith('deepseek/')):
        if not os.environ.get('DEEPSEEK_API_KEY'):
            raise ValueError('Configure DEEPSEEK_API_KEY before starting DeepSeek evaluation')


def tagged(function, phase):
    @functools.wraps(function)
    def call(*args, **kwargs):
        token = PHASE.set(phase)
        try:
            return function(*args, **kwargs)
        finally:
            PHASE.reset(token)
    return call


def run_case(case, args):
    require_credentials(args.extractor)
    from mnemotrace import ExtractionError
    from mnemotrace.consolidate.planning import WINDOW_PREFIX, validate_response
    from mnemotrace.consolidate.fact_rules import _NAME_PREDICATES
    from mnemotrace.llm.base import LLM, complete_result
    from mnemotrace.llm import providers
    from mnemotrace.service import MemoryService
    from mnemotrace.ingest.pipeline import Ingestor
    import mnemotrace.memory as memory_module

    case_dir = args.out / f'{args.revision}-{args.mode}-{case["id"]}-{args.run}'
    case_dir.mkdir()
    calls, episodes, direct_working, ack_times = [], {}, {}, []
    current = [None]
    ledger = Budget(args.budget)
    entered = threading.Event()
    original_ingest, original_memory_now = Ingestor.ingest, memory_module.now
    original_factory = providers.make_llm
    model = original_factory(args.extractor, temperature=0, max_attempts=1, num_retries=0, timeout=45)
    # No change to source text/checkpoints after enqueue: stamp at the ingestion seam.
    def ingest(self, content, user_id='default', session_id='default', role='user', event_time=None, embedding=None):
        event = current[0]
        if event is None or content != event['text'] or user_id != event['user']:
            raise RuntimeError('Unexpected ingestion in evaluator')
        ep = original_ingest(self, content, user_id, session_id, role, event['at'], embedding)
        episodes[ep.id] = event
        return ep
    Ingestor.ingest = ingest
    memory_module.now = lambda: current[0]['at'] if current[0] else 1_700_000_000

    class Meter(LLM):
        def complete(self, prompt, **kwargs):
            return self.complete_result(prompt, **kwargs).text

        def complete_result(self, prompt, **kwargs):
            data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0] if WINDOW_PREFIX in prompt else {}
            phase = 'summary' if data.get('operation') == 'summary' or '本次仅补缺失摘要' in kwargs.get('system', '') else 'extraction'
            row = dict(call_id=len(calls), phase=phase, prompt=prompt, system=kwargs.get('system'), status='started')
            calls.append(row)
            claimed, result = False, None
            try:
                if case.get('inject_transient_once') and row['phase'] == 'extraction' and not any(
                        c.get('injected') for c in calls):
                    row['injected'] = True
                    raise TimeoutError('Synthetic pre-provider interruption')
                ledger.claim()
                claimed = True
                row['provider_request'] = True
                row['started_at'] = time.perf_counter()
                with (case_dir / 'calls.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                if row['phase'] == 'extraction':
                    entered.set()
                result = complete_result(model, prompt, **kwargs)
                row.update(text=result.text, usage=result.usage, finish_reason=result.finish_reason, status='completed')
                return result
            except BudgetStopped:
                row['status'] = 'budget_stopped'
                raise
            except Exception as exc:
                row.update(status='failed', error_type=type(exc).__name__)
                raise
            finally:
                row['ended_at'] = time.perf_counter()
                row['latency_ms'] = (row['ended_at'] - row.get('started_at', row['ended_at'])) * 1000
                if claimed:
                    ledger.finish(result.usage if result else None)
                # Keep each finished attempt even if a later window/process fails.
                with (case_dir / 'calls.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')

    meter = Meter()
    providers.make_llm = lambda *a, **kw: meter
    svc, failure, snapshots = None, None, {}
    started = time.perf_counter()
    try:
        svc = MemoryService(data_dir=str(case_dir / 'store'), embedder_name='hashing', llm_name=args.extractor)
        for manual in case.get('manual', []):
            svc.add_fact(manual['user'], manual['subject'], manual['predicate'], manual['object'])
        for index, event in enumerate(case['events'], 1):
            current[0] = event
            t0 = time.perf_counter()
            recovered = False
            try:
                response = svc.remember(event['user'], event['text'], session_id=event['session'],
                                        scope=event['scope'], processing=args.mode)
            except ExtractionError as exc:
                if not (case.get('inject_transient_once') and args.mode == 'immediate' and exc.stored_raw
                        and calls and calls[-1].get('injected')):
                    raise
                # Explicitly retry committed raw input; never upload/create a second event.
                if not svc.flush(event['user'], event['session'], retry_failed=True)['ok']:
                    raise RuntimeError('Explicit retry failed')
                svc.close_session(event['user'], event['session'], summarize=True,
                                  clear_working=False, outcomes=False)
                response = {'episode_id': exc.episode_ids[0]}
                recovered = True
            ack_times.append(dict(event=event['id'], ms=(time.perf_counter() - t0) * 1000,
                                  confirmed_at=time.perf_counter(), recovered_after_retry=recovered))
            if event['scope'] == 'working':
                direct_working[response['id']] = event['id']
            if args.mode == 'buffered':
                if index in case.get('flush_after', []):
                    flushed = svc.flush(event['user'], event['session'], timeout=180)
                    if not flushed['ok']:
                        raise RuntimeError('Buffered boundary failed')
                if case.get('append_during_model') and index == 2:
                    svc.flush(event['user'], event['session'], timeout=0)
                    if not entered.wait(10):
                        raise RuntimeError('Model did not start before append')
        if args.mode == 'buffered':
            if not svc.flush(timeout=180)['ok']:
                raise RuntimeError('Buffered extraction failed')
        for user in sorted({e['user'] for e in case['events']}):
            for session in sorted({e['session'] for e in case['events'] if e['user'] == user}):
                svc.close_session(user, session, summarize=True, clear_working=False, outcomes=False)
            with svc._read_scope(user) as mem:
                snapshots[user] = dict(facts=[asdict(f) for f in mem._all_facts()],
                    working=[asdict(w) for w in mem.working_mem.values()], identity=dict(mem._identity),
                    episodes=[asdict(e) for e in mem.episodes_doc.values()])
        metrics = svc.metrics_snapshot()
    except Exception as exc:
        failure = exc.code if isinstance(exc, ExtractionError) else type(exc).__name__
        metrics = svc.metrics_snapshot() if svc else {}
    finally:
        if svc:
            svc.close()
        Ingestor.ingest, memory_module.now = original_ingest, original_memory_now
        providers.make_llm = original_factory

    restored = False
    if snapshots:
        try:
            from mnemotrace import Memory
            for user, snapshot in snapshots.items():
                with Memory.open(svc._path(user)) as reopened:
                    for group, actual in (('facts', reopened._all_facts()), ('working', reopened.working_mem.values()),
                                          ('episodes', reopened.episodes_doc.values())):
                        if sorted((asdict(v) for v in actual), key=lambda v: v['id']) != sorted(
                                snapshot[group], key=lambda v: v['id']):
                            raise ValueError('Restored extraction state mismatch')
                    if dict(reopened._identity) != snapshot['identity']:
                        raise ValueError('Restored identity mismatch')
            restored = True
        except Exception as exc:
            failure = type(exc).__name__
    elapsed = (time.perf_counter() - started) * 1000
    def source_ids(ids):
        return [episodes.get(key, {}).get('id', 'UNKNOWN:' + key) for key in ids]
    final = dict(facts=[], working=[], identity={})
    for user, snapshot in snapshots.items():
        final['identity'].update(snapshot['identity'])
        for f in snapshot['facts']:
            final['facts'].append(dict(id=f['id'], subject=f['subject'], predicate=f['predicate'], object=f['object'],
                user=user, source=f['source'], sources=source_ids(f['provenance']), valid_at=f['valid_at'],
                invalid_at=f['invalid_at'], supersedes=f['supersedes']))
        for w in snapshot['working']:
            final['working'].append(dict(content=w['content'], kind=w['kind'], user=user, session=w['session_id'],
                event_time=w['event_time'], sources=([direct_working[w['id']]] if w['id'] in direct_working
                    else source_ids(w['metadata'].get('provenance', [])))))
    raw, invalid_outputs = dict(facts=[], working=[]), []
    for index, call in enumerate(calls):
        if call['phase'] != 'extraction' or call['status'] != 'completed':
            continue
        try:
            window = json.JSONDecoder().raw_decode(call['prompt'].split(WINDOW_PREFIX, 1)[1])[0]
            from mnemotrace import Config
            value = validate_response(call['text'], window, Config(), compensation='本次仅补缺失摘要' in call['system'])
            for span in (*window['pending'], *window['context']):
                span.update(episode_id=span['id'], start=span['ranges'][0][0], end=span['ranges'][-1][1])
            spans = {s['id']: s for s in (*window['pending'], *window['context'])}
            for group in ('facts', 'working'):
                for item in value[group]:
                    refs = item.get('source_ids', [s['id'] for s in window['pending']] if len(window['pending']) == 1 else [])
                    if not refs or any(ref not in spans for ref in refs):
                        raise ValueError('Invalid raw sources')
                    selected = [spans[ref] for ref in refs]
                    pending = [s for s in window['pending'] if s['id'] in refs]
                    if not pending:
                        raise ValueError('No pending source')
                    anchor = pending[-1]
                    owner = episodes[anchor['episode_id']]
                    row = dict(item, user=owner['user'], session=owner['session'],
                        sources=source_ids([s['episode_id'] for s in selected]),
                        spans=[dict(event=episodes[s['episode_id']]['id'], content=s['content'],
                                    start=s['start'], end=s['end']) for s in selected])
                    if group == 'facts':
                        # Resolve only explicit self references, not third-party names.
                        for field in ('subject', 'object'):
                            if row[field].lower() in {'user', 'i', 'me', '用户', '我'}:
                                row[field] = owner['user']
                        if row['predicate'].strip().lower() in _NAME_PREDICATES:
                            continue  # Final identity is scored separately from persisted Facts.
                        row['valid_at'] = anchor['event_time']
                    else:
                        row['event_time'] = anchor['event_time']
                    raw[group].append(row)
        except (ValueError, KeyError, TypeError, AttributeError, ExtractionError):
            invalid_outputs.append(index)
    status = ('budget_stopped' if any(c['status'] == 'budget_stopped' for c in calls) else
              'failed' if failure or any(c['status'] != 'completed' and not c.get('injected') for c in calls)
              else 'completed')
    result = dict(revision=args.revision, mode=args.mode, case_id=case['id'], run=args.run,
        suite_hash=suite_hash(args.suite), status=status, error_code=failure,
        score=score_case(case, final), raw_score=score_case(case, raw, raw=True),
        invalid_outputs=invalid_outputs, final=final, raw=raw, snapshots=snapshots,
        calls=calls, acknowledgements=ack_times, metrics=metrics, elapsed_ms=elapsed, restored=restored)
    if case.get('append_during_model') and args.mode == 'buffered':
        active = [c for c in calls if c['phase'] == 'extraction' and 'started_at' in c]
        result['append_inflight'] = bool(active and len(ack_times) == 3 and
            active[0]['started_at'] < ack_times[2]['confirmed_at'] < active[0]['ended_at'])
    (case_dir / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'suite', 'out', 'budget'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--revision', choices=('baseline', 'candidate'), required=True)
    parser.add_argument('--extractor', default='deepseek')
    parser.add_argument('--mode', choices=('immediate', 'buffered'), required=True)
    parser.add_argument('--cases', required=True)
    parser.add_argument('--run', type=int, default=0)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    os.chdir(args.root)
    logging.disable(logging.CRITICAL)
    for key in list(os.environ):
        if key.startswith('MNEMOTRACE_'):
            os.environ.pop(key)
    os.environ.update(MNEMOTRACE_BUFFERED_EXTRACTION='1', MNEMOTRACE_STORAGE='memory',
                      MNEMOTRACE_CONFLICT_DETECTION='0', MNEMOTRACE_SESSION_OUTCOMES='0',
                      LITELLM_LOCAL_MODEL_COST_MAP='True')
    from mnemotrace.llm import providers
    import mnemotrace
    if not Path(mnemotrace.__file__).resolve().is_relative_to(args.root.resolve()):
        raise RuntimeError('Selected implementation was not loaded')
    providers.load_dotenv = lambda *a, **kw: None  # Parent loads credentials; selected code has no .env.
    chosen = set(args.cases.split(','))
    cases = [c for c in load_suite(args.suite)['cases'] if c['id'] in chosen]
    if {c['id'] for c in cases} != chosen:
        parser.error('Unknown case id')
    for case in cases:
        result = run_case(case, args)
        with (args.out / 'results.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        print(json.dumps({k: result[k] for k in ('revision', 'mode', 'case_id', 'run', 'status')}), flush=True)
        if result['status'] == 'budget_stopped':
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
