"""Isolated continuous-session observation, with no runtime changes."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

from buffered_replay import load_suite, replay
from extraction_budget import Budget, BudgetStopped
from extraction_quality import score_case, suite_hash
from extraction_runner import PHASE, require_credentials, tagged, summary_call_target

EVENT = ContextVar('replay_event', default=None)
WINDOW = ContextVar('replay_window', default=None)


def payload(prompt):
    return json.JSONDecoder().raw_decode(prompt.split('MNEMOTRACE_WINDOW\n', 1)[1])[0]


def byte_parts(prompt, system, window):
    """Exact additive wire-byte decomposition, including JSON escaping overhead."""
    encoded = lambda text: len(json.dumps(text, ensure_ascii=False)[1:-1].encode('utf-8'))
    body = sum(encoded(s['content']) for s in window['pending'])
    context = sum(encoded(s['content']) for s in window['context'])
    empty = dict(pending=[dict(s, content='') for s in window['pending']],
                 context=[dict(s, content='') for s in window['context']])
    metadata = len(json.dumps(empty, ensure_ascii=False).encode('utf-8'))
    total = len(prompt.encode('utf-8')) + len((system or '').encode('utf-8'))
    return dict(system=len((system or '').encode('utf-8')), body=body, context=context,
                metadata=metadata, template=total-len((system or '').encode('utf-8'))-body-context-metadata,
                total=total)


def recorded_response(case, window, episodes):
    """Fixture oracle for offline tests; no natural-language extraction heuristics."""
    by_event = {}
    for span in (*window['context'], *window['pending']):
        by_event.setdefault(episodes[span['episode_id']]['id'], []).append(span)
    pending = {s['id'] for s in window['pending']}
    output = dict(facts=[], working=[])
    for group in ('facts', 'working'):
        for gold in case[group]:
            sources = gold['sources'][0]
            if not all(key in by_event for key in sources):
                continue
            refs = []
            for key in sources:
                event = next(e for e in case['events'] if e['id'] == key)
                quote = gold.get('evidence', {}).get(key, event['text'].split('。', 1)[0] + '。')
                spans = [s for s in by_event[key] if quote in s['content']]
                if not spans:
                    break
                refs.append(spans[-1]['id'])
            if len(refs) != len(sources) or not pending.intersection(refs):
                continue
            if group == 'facts':
                subject, predicate, obj = gold['variants'][0]
                output[group].append(dict(subject=subject, predicate=predicate, object=obj, source_ids=refs))
            else:
                output[group].append(dict(content=gold['contents'][0], kind=gold['kinds'][0], source_ids=refs))
    for user, names in case['identity'].items():
        for span in window['pending']:
            if episodes[span['episode_id']]['user'] == user and f'我叫{names[0]}' in span['content']:
                output['facts'].append(dict(subject=user, predicate='name', object=names[0], source_ids=[span['id']]))
    return json.dumps(output, ensure_ascii=False)


def final_state(snapshots, episodes):
    final = dict(facts=[], working=[], identity={})
    def sources(ids):
        return [episodes[key]['id'] if key in episodes else 'UNKNOWN:'+key for key in ids]
    for user, snapshot in snapshots.items():
        final['identity'].update(snapshot['identity'])
        for f in snapshot['facts']:
            final['facts'].append(dict(id=f['id'], subject=f['subject'], predicate=f['predicate'], object=f['object'],
                user=user, source=f['source'], sources=sources(f['provenance']), valid_at=f['valid_at'],
                invalid_at=f['invalid_at'], supersedes=f['supersedes']))
        for w in snapshot['working']:
            final['working'].append(dict(content=w['content'], kind=w['kind'], user=user, session=w['session_id'],
                event_time=w['event_time'], sources=sources(w['metadata'].get('provenance', []))))
    return final


def raw_state(windows, episodes):
    from mnemotrace.consolidate.llm_extractor import _NAME_PREDICATES
    raw = dict(facts=[], working=[], identity={})
    for window in windows:
        if not window.get('validated'):
            continue
        spans = {s['id']: s for s in (*window['context'], *window['pending'])}
        order = {s['id']: i for i, s in enumerate(window['pending'])}
        for group in ('facts', 'working'):
            for item in window['output'][group]:
                selected = [spans[key] for key in item['source_ids']]
                anchor = max((s for s in selected if s['id'] in order), key=lambda s: (
                    s['event_time'], s['ingested_at'], order[s['id']], s['start']))
                owner = episodes[anchor['episode_id']]
                row = dict(item, user=owner['user'], session=owner['session'],
                    sources=list(dict.fromkeys(episodes[s['episode_id']]['id'] for s in selected)),
                    spans=[dict(event=episodes[s['episode_id']]['id'], content=s['content'],
                                start=s['start'], end=s['end']) for s in selected])
                if group == 'facts':
                    if row['predicate'].lower() in _NAME_PREDICATES:
                        continue
                    for field in ('subject', 'object'):
                        if row[field].lower() in {'user', 'i', 'me', '用户', '我'}:
                            row[field] = owner['user']
                    row['valid_at'] = anchor['event_time']
                else:
                    row['event_time'] = anchor['event_time']
                raw[group].append(row)
    return raw


def run_case(case, args, *, replay_fn=replay, model_override=None):
    from mnemotrace import Memory
    from mnemotrace._buffered import BufferedExtraction
    from mnemotrace.consolidate.llm_extractor import LLMExtractor
    summary_type, summary_method = summary_call_target()
    from mnemotrace.ingest.pipeline import Ingestor
    from mnemotrace.llm import Completion, LLM, providers
    from mnemotrace.llm.base import complete_result
    from mnemotrace.service import MemoryService

    if args.extractor:
        require_credentials(args.extractor)
    directory = args.out / f'{case["id"]}-{args.mode}-{args.run}'
    directory.mkdir()
    started = time.perf_counter()
    stamp = lambda: time.perf_counter() - started
    episodes, times, calls, windows, selections, commits = {}, {}, [], [], [], []
    lock = threading.RLock()
    ledger = Budget(args.budget) if args.extractor else None
    model = model_override or (providers.make_llm(args.extractor, temperature=0, max_attempts=1,
                             num_retries=0, timeout=45) if args.extractor else None)
    def log(kind, row):
        with lock, (directory / 'observations.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(kind=kind, observation=row), ensure_ascii=False)+'\n')

    class Meter(LLM):
        def complete(self, prompt, **kwargs):
            return self.complete_result(prompt, **kwargs).text

        def complete_result(self, prompt, **kwargs):
            with lock:
                row = dict(call_id=len(calls), phase=PHASE.get(), window=WINDOW.get(), prompt=prompt,
                           system=kwargs.get('system'), status='started', started_at=stamp(), provider_request=False)
                calls.append(row)
            result, claimed = None, False
            try:
                if ledger:
                    ledger.claim()
                    claimed = row['provider_request'] = True
                log('call_started', row)
                row['provider_started_at'] = stamp()
                if model is not None:
                    result = complete_result(model, prompt, **kwargs)
                else:
                    text = recorded_response(case, payload(prompt), episodes) if row['phase'] == 'extraction' else '合成摘要'
                    result = Completion(text, 'stop')
                row['provider_ended_at'] = stamp()
                row.update(status='completed', text=result.text, finish_reason=result.finish_reason, usage=result.usage)
                return result
            except Exception as exc:
                row.update(status='budget_stopped' if isinstance(exc, BudgetStopped) else 'failed',
                           error_type=type(exc).__name__)
                raise
            finally:
                row['ended_at'] = stamp()
                if 'provider_started_at' in row:
                    row.setdefault('provider_ended_at', row['ended_at'])
                if claimed:
                    ledger.finish(result.usage if result else None)
                log('call_finished', row)

    original_ingest, original_save = Ingestor.ingest, MemoryService._save
    original_raw, original_select = LLMExtractor.raw_window, BufferedExtraction._select
    def ingest(self, content, user_id='default', session_id='default', speaker='user', event_time=None, embedding=None):
        event = EVENT.get()
        if event is None or content != event['text'] or user_id != event['user']:
            raise ValueError('Replay ingestion identity mismatch')
        ep = original_ingest(self, content, user_id, session_id, speaker, event['at'], embedding)
        with lock:
            episodes[ep.id] = event
            times[event['id']]['episode_id'] = ep.id
        return ep

    def save(self, user, mem):
        result = original_save(self, user, mem)
        timestamp = stamp()
        with lock:
            for ep in mem.episodes_doc.values():
                event = episodes.get(ep.id)
                if event is None:
                    continue
                t = times[event['id']]
                t.setdefault('saved_at', timestamp)
                progress = ep.metadata.get('extraction_progress', {})
                for part in progress.get('windows', []):
                    key = (ep.id, part['window_id'], part['start'], part['end'])
                    if key not in seen_commits:
                        seen_commits.add(key)
                        entry = dict(event=event['id'], window_id=part['window_id'], start=part['start'],
                                     end=part['end'], saved_at=timestamp)
                        commits.append(entry)
                        log('committed_range', entry)
                if ep.consolidated:
                    t.setdefault('completed_at', timestamp)
        return result

    def select(self, mem, tasks, ids=None):
        selected = original_select(self, mem, tasks, ids)
        if selected:
            prepared, keys = selected
            window = prepared.windows[0]
            group = [j for j in tasks.rows(ids=ids).values() if j['state'] in {'pending', 'running', 'retry_wait'}
                     and (j['session_id'], j['scope']) == (window.pending[0].episode.session_id, window.scope)]
            now = self.clock()
            # Record simultaneous eligibility, not an invented winning trigger.
            conditions = dict(shutdown=self.stopping, forced=any(tasks.rows()[k]['forced'] for k in keys),
                idle=now-max(j['queued_at'] for j in group) >= 5,
                max_wait=now-min(j['queued_at'] for j in group) >= 30,
                incomplete=len(keys) < len(group) or any(s.end < len(s.episode.content) for s in window.pending),
                budget=prepared.extractor.window_size(window) >= mem.config.extraction_max_input_bytes)
            entry = dict(window_id=window.id, selected_at=stamp(),
                         conditions=[k for k, satisfied in conditions.items() if satisfied])
            selections.append(entry)
            log('selected_window', entry)
        return selected

    def raw_window(self, window, max_tokens):
        prompt = self.template.format(speaker=window.pending[0].episode.speaker, content=window.content)
        parts = payload(prompt)
        row = dict(window_id=window.id, **parts, started_at=stamp(), validated=False,
                   input_bytes=self.window_size(window))
        windows.append(row)
        token = WINDOW.set(window.id)
        phase = PHASE.set('extraction')
        try:
            output = original_raw(self, window, max_tokens)
            row.update(validated=True, output=output.items)
            return output
        except Exception as exc:
            row['error_code'] = getattr(exc, 'code', type(exc).__name__)
            raise
        finally:
            row['ended_at'] = stamp()
            matching = [c for c in calls if c['window'] == window.id and c['started_at'] >= row['started_at']]
            if matching:
                row['bytes'] = byte_parts(matching[0]['prompt'], matching[0]['system'], parts)
            log('window_finished', row)
            WINDOW.reset(token)
            PHASE.reset(phase)

    seen_commits, snapshots, errors, service = set(), {}, [], None
    with ExitStack() as patches:
        patches.enter_context(patch.object(Ingestor, 'ingest', ingest))
        patches.enter_context(patch.object(MemoryService, '_save', save))
        patches.enter_context(patch.object(LLMExtractor, 'raw_window', raw_window))
        patches.enter_context(patch.object(BufferedExtraction, '_select', select))
        patches.enter_context(patch.object(summary_type, summary_method, tagged(getattr(summary_type, summary_method), 'summary')))
        patches.enter_context(patch.object(providers, 'make_llm', lambda *a, **kw: Meter()))
        try:
            service = MemoryService(data_dir=str(directory/'store'), embedder_name='hashing',
                                    llm_name=args.extractor or 'synthetic')
            def call(command, row):
                if command['kind'] == 'flush':
                    # Force the captured boundary without holding up later appends.
                    answer = service.flush(command['user'], command.get('session'), timeout=0)
                    row['flush_status'] = answer['status']
                    if answer['status'] == 'failed':
                        raise RuntimeError('Replay flush incomplete')
                    return
                event = command['event']
                times[event['id']] = row
                token = EVENT.set(event)
                try:
                    response = service.remember(event['user'], event['text'], session_id=event['session'],
                                                scope=event['scope'], processing=args.mode)
                    row['response'] = response
                finally:
                    EVENT.reset(token)
            # The producer's times are relative to its own origin; align all observations.
            replay_origin = stamp()
            arrivals = replay_fn(case, call)
            for row in arrivals:
                for field in ('planned_at', 'generated_at', 'entered_at', 'returned_at'):
                    row[field] += replay_origin
            if not service.flush(timeout=180)['ok']:
                errors.append('final_flush_incomplete')
            for user in sorted({e['user'] for e in case['events']}):
                for session in sorted({e['session'] for e in case['events'] if e['user'] == user}):
                    service.close_session(user, session, summarize=True, clear_working=False, outcomes=False)
                with service._read_scope(user) as mem:
                    snapshots[user] = dict(facts=[asdict(f) for f in mem._all_facts()],
                        working=[asdict(w) for w in mem.working_mem.values()], identity=dict(mem._identity),
                        episodes=[asdict(e) for e in mem.episodes_doc.values()], processing=mem._processing_state)
        except Exception as exc:
            errors.append(getattr(exc, 'code', type(exc).__name__))
            arrivals = list(times.values())
        finally:
            if service:
                service.close()
    restored = bool(snapshots)
    try:
        for user, snapshot in snapshots.items():
            with Memory.open(service._path(user)) as reopened:
                for group, values in (('facts', reopened._all_facts()), ('working', reopened.working_mem.values()),
                                      ('episodes', reopened.episodes_doc.values())):
                    if sorted((asdict(v) for v in values), key=lambda v: v['id']) != sorted(snapshot[group], key=lambda v: v['id']):
                        raise ValueError('Replay restore mismatch')
                if dict(reopened._identity) != snapshot['identity'] or reopened._processing_state != snapshot['processing']:
                    raise ValueError('Replay restore mismatch')
    except Exception as exc:
        restored = False
        errors.append(type(exc).__name__)
    final, raw = final_state(snapshots, episodes), raw_state(windows, episodes)
    complete = (restored and len(times) == len(case['events']) and all('completed_at' in t for t in times.values())
                and not errors and all(r['status'] == 'completed' for r in arrivals)
                and all(c['status'] == 'completed' for c in calls if c['phase'] != 'extraction')
                and not any(c['status'] == 'budget_stopped' for c in calls))
    result = dict(case_id=case['id'], run=args.run, mode=args.mode, suite_hash=suite_hash(args.suite),
        status='completed' if complete else 'failed', errors=errors, restored=restored, arrivals=arrivals,
        calls=calls, windows=windows, selections=selections, commits=commits, final=final, raw=raw,
        score=score_case(case, final), raw_score=score_case(case, raw, raw=True), snapshots=snapshots,
        elapsed_seconds=stamp(), synthetic=args.extractor is None,
        budgets=dict(input_bytes=service.config.extraction_max_input_bytes,
                     context_bytes=service.config.extraction_context_bytes,
                     output_tokens=service.config.extraction_max_output_tokens) if service else {},
        inflight_observed=any(w['started_at'] < t.get('saved_at', -1) < w['ended_at']
            and t.get('episode_id') not in {s['episode_id'] for s in w['pending']}
            for w in windows for t in times.values()))
    (directory/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'out', 'suite', 'budget'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--case', required=True)
    parser.add_argument('--mode', choices=('immediate', 'buffered'), required=True)
    parser.add_argument('--run', type=int, required=True)
    parser.add_argument('--extractor')
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    os.chdir(args.root)
    logging.disable(logging.CRITICAL)
    for key in list(os.environ):
        if key.startswith('MNEMOTRACE_'):
            os.environ.pop(key)
    os.environ.update(MNEMOTRACE_BUFFERED_EXTRACTION='1', MNEMOTRACE_STORAGE='memory',
                      MNEMOTRACE_CONFLICT_DETECTION='0', MNEMOTRACE_SESSION_OUTCOMES='0',
                      MNEMOTRACE_EXTRACT_WORKERS='1', LITELLM_LOCAL_MODEL_COST_MAP='True')
    from mnemotrace.llm import providers
    import mnemotrace
    if not Path(mnemotrace.__file__).resolve().is_relative_to(args.root.resolve()):
        raise ValueError('Wrong selected runtime')
    providers.load_dotenv = lambda *a, **kw: None
    case = next(c for c in load_suite(args.suite)['cases'] if c['id'] == args.case)
    result = run_case(case, args)
    with (args.out/'results.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False)+'\n')
    print(json.dumps({k: result[k] for k in ('case_id', 'mode', 'run', 'status', 'errors')}), flush=True)


if __name__ == '__main__':
    main()
