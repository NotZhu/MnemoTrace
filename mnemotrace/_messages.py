"""One recoverable dialogue step, sharing the process worker and namespace lease."""
from __future__ import annotations

from copy import copy, deepcopy
import time
import uuid

from ._namespace_lock import namespace_lock
from .consolidate.messages import KINDS, MessageTasks, TERMINAL, config_identity, digest, reset_attempts, source
from .consolidate.message_windows import input_size, output_size, plan, request, system_prompt, validate_response
from .extraction import ExtractionError, ExtractionTruncatedError, LLMNotConfiguredError, require_llm
from .llm.base import complete_result
from .store._validation import validate_embedding
from .types import Fact, WorkingMemory


def clock(service):
    return service._buffered.clock() if service._buffered else time.time()


def _pending(tasks, session, after=0):
    return [r for r in tasks.rows(session['session_id']).values() if not r['cancelled']
            and (r['mode'], r['scope']) == (session['mode'], session['scope']) and r['sequence'] > after
            and r['window_id'] is None and r['core'] == 'pending']


def trigger_ready(mem, tasks, now):
    if tasks.state is None:
        return
    for session in tasks.state['sessions'].values():
        rows = _pending(tasks, session, session['cutoff'])
        if not rows:
            continue
        cutoff = max(r['sequence'] for r in rows)
        if now - session['last_activity'] >= 60:
            tasks.trigger(session, cutoff)
        else:
            # Budget triggers freeze only the next window. No oldest-message timeout.
            data = plan(mem, tasks, session, cutoff, after=session['cutoff'])
            if data and (len(data['pending']) < len(rows) or any(s['partial'] for s in data['pending'])
                    or input_size(data, mem.config) >= mem.config.extraction_max_input_bytes
                    or output_size(data, mem.config) + mem.config.joint_summary_output_tokens > mem.config.joint_max_output_tokens):
                tasks.trigger(session, data['pending'][-1]['sequence'])


def _advance(tasks, session):
    rows = [r for r in tasks.rows(session['session_id']).values()
            if (r['mode'], r['scope']) == (session['mode'], session['scope'])
            and r['sequence'] <= session['cutoff'] and not r['cancelled']]
    if any(r['window_id'] is None or tasks.state['windows'][r['window_id']]['state'] not in TERMINAL for r in rows):
        return
    session.update(cutoff=session['following'], following=0,
                   batch_id=uuid.uuid4().hex if session['following'] else '')


def _select(mem, tasks, now):
    trigger_ready(mem, tasks, now)
    for session_key, session in sorted(tasks.state['sessions'].items(), key=lambda item: item[1]['last_turn']):
        # Failed windows can be explicitly retried after their original batch has ended.
        windows = [(key, w) for key, w in tasks.state['windows'].items()
                   if w['route'] == session_key and w['state'] not in TERMINAL]
        ready = [(key, w) for key, w in windows if w['next_attempt_at'] <= now]
        if ready:
            key, win = min(ready, key=lambda item: item[1]['input']['pending'][0]['sequence'])
        else:
            if windows:  # Do not reorder this session across a retry boundary.
                continue
            if not session['cutoff']:
                continue
            data = plan(mem, tasks, session, session['cutoff'])
            if data is None:
                _advance(tasks, session)
                continue
            key = uuid.uuid4().hex[:24]
            win = dict(route=session_key, batch_id=session['batch_id'], input=data, input_hash=digest(data),
                config=config_identity(mem), state='pending', phase='generate', attempts=0,
                embed_attempts=0, next_attempt_at=0, result=None, vectors={}, core_saved=False,
                summary_saved=[], error_code=None, history=[], guard=tasks.state['generation'], result_hash=None,
                limits=dict(summary_min_chars=mem.config.summary_min_chars, summary_max_chars=mem.config.summary_max_chars))
            tasks.state['windows'][key] = win
            for item in data['pending']:
                tasks.state['messages'][item['id']]['window_id'] = key
        session['last_turn'] = 1 + max(s['last_turn'] for s in tasks.state['sessions'].values())
        return key, win
    return None


def _fail(tasks, win, code, now, *, retryable=True, embedding=False):
    attempts = win['embed_attempts'] if embedding else win['attempts']
    retry = retryable and attempts < 3
    win.update(state='retry_wait' if retry else 'failed', error_code=code,
               next_attempt_at=now + (30 if attempts == 1 else 120) if retry else 0)
    if not retry:
        for item in win['input']['pending']:
            row = tasks.state['messages'][item['id']]
            if not win['core_saved']:
                row['core'] = 'failed'
            if row['summary'] == 'pending':
                row['summary'] = 'failed'


def _retryable(exc):
    cause = exc.__cause__ or exc
    return not (isinstance(cause, (ImportError, TypeError, LLMNotConfiguredError)) or any(
        name in type(cause).__name__.lower() for name in ('authentication', 'permission', 'badrequest', 'notfound')))


def _texts(win):
    result = win['result']
    texts = []
    if not win['core_saved']:
        texts.extend(f['text'] for f in result['facts'])
        texts.extend(w['content'] for w in result['working'])
    texts.extend(text for key, text in result['summaries'].items() if key not in win['summary_saved'])
    return list(dict.fromkeys(texts))


def _current(mem, tasks, win, token, model):
    if (tasks.state['generation'] != token or config_identity(mem) != win['config']
            or mem.llm is not model):
        return False
    for item in (*win['input']['pending'], *win['input']['context']):
        ep = mem.episodes_doc.get(item['id'])
        if ep is None or source(ep) != item['source']:
            return False
        if item['representation'] == 'summary' and ep.summary != item['content']:
            return False
    return True


def _commit(mem, tasks, win, now):
    data, result = win['input'], win['result']
    sources = {s['id']: s for s in (*data['context'], *data['pending'])}
    pending = {s['id'] for s in data['pending']}
    def anchor(item):
        return max((sources[k] for k in item['source_ids'] if k in pending and
                    sources[k]['role'] == KINDS[item['assertion_kind']]), key=lambda s: s['sequence'])
    def vector(text):
        return win['vectors'][digest(text)]
    stats = mem.engine.empty_stats()
    if not win['core_saved']:
        conflict = mem.engine.conflict
        mem.engine.conflict = copy(conflict)
        # Joint dialogue generation has a single bounded model-call ledger.
        # Commit uses deterministic reconciliation and cannot call another model under the lock.
        mem.engine.conflict.llm = None
        try:
            for item in sorted(result['facts'], key=lambda item: anchor(item)['sequence']):
                origin = anchor(item)
                from .consolidate.topic_rules import resolve_topic
                from .consolidate.classify import classify_fact
                topic, topic_source = resolve_topic(item['predicate'], item['object'],
                    item.get('topic') if mem.config.llm_topics else None)
                fact = Fact(subject=item['subject'], predicate=item['predicate'], object=item['object'],
                    text=item['text'], display=item['text'], user_id=tasks.namespace,
                    topic=topic, topic_source=topic_source,
                    valid_at=origin['event_time'], created_at=now, last_access=now,
                    provenance=item['source_ids'], assertion_kind=item['assertion_kind'],
                    source_roles=list(dict.fromkeys(sources[k]['role'] for k in item['source_ids'])),
                    id='ft_' + digest([win['input_hash'], item])[:24], embedding=vector(item['text']))
                classify_fact(fact)
                if mem.engine._accepted(fact, '\n'.join(sources[k]['content'] for k in item['source_ids'])):
                    mem.engine._write_fact(fact, stats)
                from .consolidate.llm_extractor import _NAME_PREDICATES
                if item['predicate'] in _NAME_PREDICATES and item['assertion_kind'] in {'user_statement', 'user_confirmation'}:
                    mem._identity.setdefault(tasks.namespace, item['object'])
                    mem._aliases.setdefault(tasks.namespace, set()).add(item['object'].lower())
                    mem.engine.extractor.self_name.update(mem._identity)
                    mem.engine.extractor.aliases.update(mem._aliases)
            for item in result['working']:
                origin = anchor(item)
                key = 'wm_' + digest([win['input_hash'], item])[:24]
                mem.working_mem.setdefault(key, WorkingMemory(id=key, content=item['content'], kind=item['kind'],
                    user_id=tasks.namespace, session_id=tasks.state['messages'][origin['id']]['session_id'],
                    event_time=origin['event_time'], created_at=now, embedding=vector(item['content']),
                    metadata=dict(provenance=item['source_ids'], assertion_kind=item['assertion_kind'],
                                  episode_id=origin['id'])))
        finally:
            mem.engine.conflict = conflict
        for item in data['pending']:
            row = tasks.state['messages'][item['id']]
            ep = mem.episodes_doc.get(item['id'])
            ep.consolidated = True
            ep.coverage = dict(unit='unicode_code_points', ranges=item['ranges'], partial=item['partial'],
                policy='head_tail_v1' if item['partial'] else 'full_message_v1', source=item['source'],
                original_length=item['source_length'], input_hash=win['input_hash'], strategy_completed=True)
            row['core'] = 'completed'
            mem._sync_episode(ep)
        win['core_saved'] = True
        mem._persona_cache.clear()
        mem._enforce_hot_limit()
    for item in data['pending']:
        mid = item['message_id']
        if mid in result['summaries'] and mid not in win['summary_saved']:
            ep = mem.episodes_doc.get(item['id'])
            ep.summary = result['summaries'][mid]
            ep.summary_embedding = vector(ep.summary)
            mem._sync_episode(ep)
            tasks.state['messages'][ep.id]['summary'] = 'completed'
            win['summary_saved'].append(mid)
    missing = [s for s in data['pending'] if tasks.state['messages'][s['id']]['summary'] == 'pending']
    if missing:
        win.update(phase='compensate', vectors={})
        reset_attempts(win, 'embed_attempts', 'summary_pending', now)
        _fail(tasks, win, 'summary_invalid_response', now)
    else:
        win.update(phase='done', state='completed', error_code=None, next_attempt_at=0, vectors={})
    _advance(tasks, tasks.state['sessions'][win['route']])


def run_one(service, user):
    lease = (service.message_lease() if hasattr(service, 'message_lease') else
             namespace_lock(service._contained_child('@' + service._safe_user(user) + '.processing.lock')).try_file_lock())
    with lease as acquired:
        if not acquired or service._closed or service._buffered and service._buffered.abort.is_set():
            return False
        with service.write_lock(user):
            if service._store_version(user) is None:
                return False
            mem = service.get(user)
            tasks = MessageTasks(mem, user)
            if tasks.state is None:
                return False
            before = digest(tasks.state)
            tasks.cancel_missing()
            selected = _select(mem, tasks, clock(service))
            if selected is None:
                if digest(tasks.state) != before:
                    service._save(user, mem)
                return False
            key, win = selected
            if win['history'] and win['history'][-1]['outcome'] == 'in_flight':
                win['history'][-1]['outcome'] = 'interrupted'
            if config_identity(mem) != win['config']:
                _fail(tasks, win, 'configuration_changed', clock(service), retryable=False)
                service._save(user, mem)
                return True
            if tasks.state['generation'] != win['guard'] or not _current(mem, tasks, win, win['guard'], mem.llm):
                win.update(state='cancelled', error_code='derived_state_changed')
                service._save(user, mem)
                return True
            if win['phase'] == 'commit':
                _commit(mem, tasks, win, clock(service))
                service._save(user, mem)
                return True
            embedding = win['phase'] == 'embed'
            counter = 'embed_attempts' if embedding else 'attempts'
            if win[counter] >= 3:
                _fail(tasks, win, 'embedding_failed' if embedding else 'extraction_failed',
                      clock(service), retryable=False, embedding=embedding)
                _advance(tasks, tasks.state['sessions'][win['route']])
                service._save(user, mem)
                return True
            win[counter] += 1
            win['state'] = 'running'
            win['history'].append(dict(stage=win['phase'], attempt=win[counter], started_at=clock(service),
                finished_at=None, outcome='in_flight', finish_reason=None, usage=None, error_code=None))
            config, data, phase = deepcopy(mem.config), deepcopy(win['input']), win['phase']
            model, embedder = mem.llm, mem.embedder
            policy = mem.policy.get('extract_system', '') + mem.policy.get('extract_instruction', '')
            texts = _texts(win) if embedding else []
            saved_summaries = list(win['summary_saved'])
            token = tasks.state['generation']
            service._save(user, mem)
            mem = tasks = win = None
        failure, output, completion = None, None, None
        try:
            if embedding:
                vectors = embedder.embed_batch(texts) if texts else []
                if len(vectors) != len(texts):
                    raise ValueError('Embedding batch length mismatch')
                for v in vectors:
                    validate_embedding(v, 'message embedding', embedder.dim)
                output = {digest(text): vector for text, vector in zip(texts, vectors)}
            else:
                from ._buffered import ModelMeter, one_attempt
                require_llm(model)
                prompt = request(data, config)
                system = system_prompt(policy, compensation=phase == 'compensate', llm_topics=config.llm_topics)
                if phase == 'compensate':
                    data['pending'] = [s for s in data['pending'] if s['message_id'] not in saved_summaries]
                    prompt = request(data, config)
                completion = complete_result(ModelMeter(one_attempt(model), service.metrics, 'messages'), prompt,
                    system=system, max_tokens=config.joint_max_output_tokens)
                if completion.finish_reason not in {None, 'stop', 'end_turn'}:
                    raise ExtractionTruncatedError()
                output = validate_response(completion.text, data, config, compensation=phase == 'compensate')
        except Exception as exc:
            failure = exc
        if service._closed or service._buffered and service._buffered.abort.is_set():
            return False
        with service.write_lock(user):
            if service._store_version(user) is None:
                return False
            mem = service.get(user)
            tasks = MessageTasks(mem, user)
            win = tasks.state['windows'].get(key) if tasks.state else None
            if win is None or win['state'] != 'running':
                return False
            win['history'][-1].update(finished_at=clock(service),
                finish_reason=completion.finish_reason if completion is not None else None,
                usage=completion.usage if completion is not None else None,
                outcome='failed' if failure is not None else 'saved')
            if not _current(mem, tasks, win, token, model):
                win.update(state='cancelled', error_code='source_changed')
                win['history'][-1]['outcome'] = 'discarded'
            elif failure is not None:
                code = 'embedding_failed' if embedding else (
                    failure.code if isinstance(failure, ExtractionError) else 'extraction_failed')
                _fail(tasks, win, code, clock(service), retryable=_retryable(failure), embedding=embedding)
                win['history'][-1]['error_code'] = code
            elif embedding:
                win['vectors'] = output
                win.update(phase='commit', state='pending', next_attempt_at=0)
            else:
                if phase == 'compensate':
                    for mid, text in output['summaries'].items():
                        if mid not in win['summary_saved']:
                            win['result']['summaries'][mid] = text
                else:
                    from .consolidate.llm_extractor import _norm_predicate, _SELF_REFS
                    from .consolidate.topic_rules import log_topic_inputs
                    if mem.config.llm_topics:
                        log_topic_inputs(output['facts'])
                    accepted = []
                    for item in output['facts']:
                        subject = item['subject'].strip()
                        if item['assertion_kind'].startswith('assistant_') and subject.lower() in _SELF_REFS:
                            subject = 'assistant'
                        elif subject.lower() in _SELF_REFS or subject.lower() in mem._aliases.get(user, set()):
                            subject = mem._identity.get(user, user)
                        candidate = Fact(subject=subject, predicate=_norm_predicate(item['predicate']), object=item['object'], text=item['text'])
                        if mem.engine._accepted(candidate, '\n'.join(s['content'] for s in data['pending'])):
                            accepted.append({**item, 'subject': candidate.subject, 'predicate': candidate.predicate,
                                             'object': candidate.object, 'text': candidate.text})
                    output['facts'] = accepted
                    win['result'] = output
                win['result_hash'] = digest(win['result'])
                win.update(phase='embed', state='pending', next_attempt_at=0)
                reset_attempts(win, 'embed_attempts', 'generated_result', clock(service))
            _advance(tasks, tasks.state['sessions'][win['route']])
            service._save(user, mem)
        return True


def flush(service, user, session_id=None, timeout=30, retry_failed=False, boundary=None):
    if type(timeout) not in (int, float) or not 0 <= timeout <= 300:
        raise ValueError('timeout must be between 0 and 300 seconds')
    with service.write_lock(user):
        mem = service.get(user)
        tasks = MessageTasks(mem, user)
        ids = set(tasks.rows(session_id, boundary))
        generation = tasks.state['generation'] if tasks.state else None
        tasks.force(session_id, ids, retry_failed)
        service._save(user, mem)
    if service._buffered:
        service._buffered.notify(user)
    deadline = time.monotonic() + timeout
    while True:
        with service._read_scope(user) as mem:
            tasks = MessageTasks(mem, user)
            result = tasks.snapshot(session_id, ids)
        invalidated = result['generation'] != generation or len(result['jobs']) != len(ids)
        counts = result['counts']
        active = any(counts[s] for s in ('pending', 'running', 'retry_wait'))
        failed = invalidated or bool(counts['failed'] or counts['cancelled'])
        status = 'timed_out' if active else 'failed' if failed else 'completed'
        if not active or time.monotonic() >= deadline:
            return dict(ok=status == 'completed', status=status, **result)
        # Immediate mode also works when the background worker was explicitly disabled.
        if service._buffered is None or not service._buffered.thread.is_alive():
            if run_one(service, user):
                continue
        time.sleep(min(.02, max(0, deadline - time.monotonic())))


def flush_memory(memory, namespace, session_id=None, timeout=30, retry_failed=False):
    """Drive the same checkpoint machine for the standalone Python facade."""
    from contextlib import contextmanager
    from pathlib import Path
    from .metrics import Metrics
    from .store.persist import snapshot_version

    class LocalExecution:
        _buffered = None
        metrics = Metrics()

        @property
        def _closed(self):
            return memory._message_closed

        def _safe_user(self, user):
            return digest(user)

        def _contained_child(self, name):
            root = Path(memory._persist_path).parent if memory._persist_path else Path('.')
            return str(root / name)

        @contextmanager
        def message_lease(self):
            with memory._message_step_lock:
                if memory._persist_path:
                    # Match the service lease when opening its namespace directly.
                    root = Path(memory._persist_path)
                    from ._namespace_paths import namespace_directory
                    lock_path = (root.parent / ('@' + root.name + '.processing.lock')
                                 if root.name == namespace_directory(namespace)
                                 else Path(str(root) + '.processing.lock'))
                    with namespace_lock(str(lock_path)).try_file_lock() as acquired:
                        yield acquired
                else:
                    yield True

        def _store_version(self, user):
            return memory._snapshot_version or 'memory'

        def get(self, user):
            return memory

        @contextmanager
        def write_lock(self, user):
            with memory._message_lock:
                try:
                    yield
                except BaseException:
                    # A backend can fail inside commit before save is reached.
                    # The same dirty-state rule covers both kinds of failure.
                    memory._recover_message_checkpoint()
                    raise

        @contextmanager
        def _read_scope(self, user):
            with memory._message_lock:
                yield memory

        def _save(self, user, mem):
            if mem._persist_path:
                if snapshot_version(mem._persist_path) != mem._snapshot_version:
                    raise RuntimeError('Snapshot changed; reopen before processing')
                mem.save(check_version=True)

    return flush(LocalExecution(), namespace, session_id, timeout, retry_failed)
