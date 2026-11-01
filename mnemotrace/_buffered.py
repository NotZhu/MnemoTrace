"""Recoverable session scheduling. Durable work belongs to ProcessingTasks, not this wake list."""
from __future__ import annotations

from collections import OrderedDict
from copy import copy, deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import logging
import os
import threading
import time

from ._namespace_lock import namespace_lock
from .consolidate.tasks import ACTIVE, ProcessingTasks
from .extraction import ExtractionError, ExtractionResponseError, LLMNotConfiguredError
from .llm.base import LLM, complete_result


def fingerprint(mem, llm):
    options = {k: v for k, v in asdict(mem.config).items() if k.startswith('extraction_') or k in {
        'llm_topics', 'explicit_preference_extraction', 'preference_object_filter',
        'preference_object_normalization', 'preference_reversal_extraction', 'procedural_extraction',
        'conflict_detection', 'conflict_llm_escalation'}}
    provider = [getattr(llm, field, None) for field in ('model', 'temperature', 'max_tokens', 'kwargs')]
    return hashlib.sha256(json.dumps([mem.policy, options, id(llm), provider], sort_keys=True,
        default=lambda value: [type(value).__module__, type(value).__qualname__, id(value)]).encode()).hexdigest()


def one_attempt(llm):
    from .llm.litellm_llm import LiteLLMClient
    from .llm.cache import CachedLLM
    if isinstance(llm, CachedLLM):
        llm = copy(llm)
        llm.inner = one_attempt(llm.inner)
    if isinstance(llm, LiteLLMClient):
        llm = copy(llm)
        llm.max_attempts = 1
        llm.kwargs = {**llm.kwargs, 'num_retries': 0}
    return llm


class ModelMeter(LLM):
    """Measure attempts at the call boundary, including rejected or discarded completions."""
    def __init__(self, inner, metrics, prefix='buffered'):
        self.inner, self.metrics, self.prefix = inner, metrics, prefix

    def complete(self, prompt, **kwargs):
        return self.complete_result(prompt, **kwargs).text

    def complete_result(self, prompt, **kwargs):
        started, result = time.perf_counter(), None
        try:
            result = complete_result(self.inner, prompt, **kwargs)
            return result
        finally:
            self.metrics.observe(self.prefix + '_model_call', time.perf_counter() - started)
            usage = result.usage if result is not None else None
            for name, count in (usage or {}).items():
                self.metrics.count(self.prefix + '_' + name, count)
            if usage is None or 'total_tokens' not in usage:
                self.metrics.count(self.prefix + '_usage_unreported')


class DecisionTape:
    """Record optional conflict judgements off-lock; committing may only replay them."""
    def __init__(self, llm):
        self.llm, self.answers, self.replaying, self.missed = llm, {}, False, False

    def complete(self, prompt, **kwargs):
        key = json.dumps([prompt, kwargs], sort_keys=True)
        if self.replaying:
            if key not in self.answers:
                self.missed = True
                raise RuntimeError('Conflict inputs changed')
            answer = self.answers[key]
        else:
            try:
                answer = self.llm.complete(prompt, **kwargs)
            except Exception:
                answer = None
            self.answers[key] = answer
        if answer is None:
            raise RuntimeError('Conflict judgement unavailable')
        return answer


def conflict_inputs(mem):
    facts = [asdict(f) for f in mem._all_facts()]
    return hashlib.sha256(json.dumps([sorted(facts, key=lambda f: f['id']), mem._identity,
        {k: sorted(v) for k, v in mem._aliases.items()},
        sorted((asdict(c) for c in mem.conflicts.values()), key=lambda c: c['id'])],
        sort_keys=True).encode()).hexdigest()


def conflict_preview(mem, window, tape):
    from .memory import Memory
    staged = Memory(config=replace(mem.config, storage='memory', data_path=None),
                    embedder=mem.embedder, llm=tape)
    for source, target in ((mem.fact_store, staged.fact_store), (mem.cold_store, staged.cold_store)):
        for f in source.values():
            target.upsert(f.id, f.embedding or [], deepcopy(f))
    for span in (*window.context, *window.pending):
        staged.episodes_doc.put(span.episode.id, deepcopy(span.episode))
    staged._identity, staged._aliases = deepcopy(mem._identity), deepcopy(mem._aliases)
    staged.working_mem, staged.conflicts = deepcopy(mem.working_mem), deepcopy(mem.conflicts)
    staged.policy = deepcopy(mem.policy)
    staged._rewire()
    return staged


class BufferedExtraction:
    def __init__(self, service, *, clock=time.time, monotonic=time.monotonic):
        self.service, self.clock, self.monotonic = service, clock, monotonic
        self.condition = threading.Condition()
        self.users = OrderedDict()
        self.wake_revision = 0
        self.last_group = {}
        self.next_kind = {}
        self.stopping = False
        self.abort = threading.Event()
        self.recovery_error = False
        self.last_scan = 0.0
        self.thread = threading.Thread(target=self.consume, name='mnemotrace-extraction', daemon=True)
        self.scan()  # Invalid durable feature state fails startup visibly.

    def lease(self, user):
        return namespace_lock(self.service._contained_child(
            '@' + self.service._safe_user(user) + '.processing.lock'))

    def notify(self, user):
        with self.condition:
            self.wake_revision += 1
            self.users[user] = self.wake_revision
            self.condition.notify_all()

    def scan(self):
        from .store.persist import discover_processing_namespaces
        namespaces = []
        for user, active in discover_processing_namespaces(self.service.data_dir):
            namespaces.append(user)
            if active:
                self.notify(user)
        self.last_scan = self.monotonic()
        self.recovery_error = False
        return namespaces

    def status(self, user, session_id=None, ids=None):
        result = self.service._processing_status(user, session_id, ids)
        result['enabled'] = True
        result['recovery_error'] = self.recovery_error
        return result

    def _select(self, mem, tasks, ids=None):
        now = self.clock()
        groups = OrderedDict()
        for key, job in tasks.rows(ids=ids).items():
            if job['state'] not in ACTIVE:
                continue
            groups.setdefault((job['session_id'], job['scope']), []).append((key, job))
        # updated_at moves successful segments behind other eligible groups.
        with self.condition:
            previous = self.last_group.get(tasks.namespace)
        for group in sorted(groups.values(), key=lambda g: (
                (g[0][1]['session_id'], g[0][1]['scope']) == previous,
                min(j['updated_at'] for _, j in g))):
            route = (group[0][1]['session_id'], group[0][1]['scope'])
            if any(j['state'] == 'failed' and (j['session_id'], j['scope']) == route
                   for j in tasks.rows().values()):
                continue
            if any(j['state'] == 'retry_wait' and j['next_attempt_at'] > now for _, j in group):
                continue
            keys = [key for key, _ in group]
            sources = tasks.sources(keys)
            mem._apply_policy()
            prepared = mem.engine.prepare(sources, first_only=True)
            if not prepared.windows:
                continue
            window = prepared.windows[0]
            selected = [s.episode.id for s in window.pending]
            jobs = [tasks.rows()[key] for key in selected]
            first = min(j['queued_at'] for _, j in group)
            last = max(j['queued_at'] for _, j in group)
            incomplete = len(selected) < len(keys) or any(s.end < len(s.episode.content) for s in window.pending)
            if (self.stopping or any(j['forced'] for j in jobs) or now - last >= 5 or now - first >= 30 or incomplete
                    or mem.engine.extractor.window_size(window) >= mem.config.extraction_max_input_bytes):
                return prepared, selected
        return None

    def run_one(self, user, ids=None):
        from ._messages import run_one as run_messages
        from ._summaries import run_one as run_summary
        steps = (lambda: run_messages(self.service, user),
                 lambda: self._run_extraction(user, ids),
                 lambda: run_summary(self.service, user, ids))
        start = self.next_kind.get(user, 0)
        for offset in range(len(steps)):
            index = (start + offset) % len(steps)
            if steps[index]():
                self.next_kind[user] = (index + 1) % len(steps)
                return True
        return False

    def _run_extraction(self, user, ids=None):
        """A single lease protects preparation/model/commit, never namespace reads or appends."""
        svc = self.service
        with self.lease(user).try_file_lock() as acquired:
            if not acquired or self.abort.is_set():
                return False
            with svc.write_lock(user):
                if self.abort.is_set():
                    return False
                if not os.path.isfile(os.path.join(svc._path(user), 'manifest.json')):
                    return False
                mem = svc.get(user)
                tasks = ProcessingTasks(mem, user)
                before = copy(tasks.state) if tasks.state is None else json.dumps(tasks.state, sort_keys=True)
                tasks.reconcile(self.clock())
                try:
                    selected = self._select(mem, tasks, ids)
                except ExtractionError as exc:
                    keys = [k for k, j in tasks.rows(ids=ids).items() if j['state'] in ACTIVE]
                    tasks.mark(keys, 'failed', self.clock(), error_code=exc.code)
                    svc._save(user, mem)
                    return False
                if selected is None:
                    after = None if tasks.state is None else json.dumps(tasks.state, sort_keys=True)
                    if before != after:
                        svc._save(user, mem)
                    return False
                prepared, keys = selected
                with self.condition:
                    job = tasks.rows()[keys[0]]
                    self.last_group[user] = (job['session_id'], job['scope'])
                claimed_llm = svc.llm
                token = (tasks.state['generation'], tasks.state['revision'], fingerprint(mem, claimed_llm))
                for key in keys:
                    job = tasks.rows()[key]
                    tasks.mark([key], 'running', self.clock(), attempts=job['attempts'] + 1,
                               window_attempts=job['window_attempts'] + 1, error_code=None)
                svc.metrics.observe('buffered_queue_wait', max(0, self.clock() - min(
                    tasks.rows()[key]['queued_at'] for key in keys)))
                svc._save(user, mem)
                detached = copy(prepared.extractor)
                detached.llm = ModelMeter(one_attempt(claimed_llm), svc.metrics)
                prepared = replace(prepared, extractor=detached)
                needs_conflicts = mem.config.conflict_detection or mem.config.conflict_llm_escalation
                mem = tasks = None
            # No live memory/store is accessed while awaiting the model.
            failure = None
            try:
                outputs = prepared.execute()
            except Exception as exc:
                failure = exc
            tape, conflict_version = None, None
            if failure is None and needs_conflicts and not self.abort.is_set():
                with svc.write_lock(user):
                    if self.abort.is_set() or not os.path.isfile(os.path.join(svc._path(user), 'manifest.json')):
                        return False
                    mem = svc.get(user)
                    if mem.config.conflict_detection or mem.config.conflict_llm_escalation:
                        tape = DecisionTape(ModelMeter(one_attempt(svc.llm), svc.metrics, 'buffered_conflict'))
                        conflict_version = conflict_inputs(mem)
                        preview = conflict_preview(mem, prepared.windows[0], tape)
                    mem = None
                if tape is not None:
                    try:
                        preview._apply_extraction(prepared.windows[0], outputs[0])
                    finally:
                        preview.close()
                    tape.replaying = True
            if self.abort.is_set():
                svc.metrics.count('buffered_results_discarded')
                return False
            with svc.write_lock(user):
                if self.abort.is_set():
                    return False
                if not os.path.isfile(os.path.join(svc._path(user), 'manifest.json')):
                    return False
                mem = svc.get(user)
                tasks = ProcessingTasks(mem, user)
                if tasks.state is None:
                    return False
                current_token = (tasks.state['generation'], tasks.state['revision'], fingerprint(mem, svc.llm))
                if any(tasks.rows().get(k, {}).get('state') != 'running' for k in keys):
                    svc.metrics.count('buffered_results_discarded')
                    return False
                if (svc.llm is not claimed_llm or current_token != token or mem.engine.rebind(prepared.windows[0]) is None
                        or (tape is not None and conflict_inputs(mem) != conflict_version)):
                    tasks.mark(keys, 'pending', self.clock(), window_attempts=0, error_code=None)
                    svc.metrics.count('buffered_results_discarded')
                    svc._save(user, mem)
                    return True
                if failure is not None:
                    retryable = isinstance(failure, ExtractionError) and not isinstance(
                        failure, (ExtractionResponseError, LLMNotConfiguredError))
                    cause = failure.__cause__
                    if cause and (isinstance(cause, (ValueError, TypeError, ImportError)) or any(
                            t in type(cause).__name__.lower() for t in (
                                'authentication', 'permission', 'badrequest', 'notfound'))):
                        retryable = False
                    attempts = max(tasks.rows()[k]['window_attempts'] for k in keys)
                    retry = retryable and attempts < 3
                    tasks.mark(keys, 'retry_wait' if retry else 'failed', self.clock(),
                               error_code=failure.code if isinstance(failure, ExtractionError) else 'extraction_failed',
                               next_attempt_at=self.clock() + (30 if attempts == 1 else 120) if retry else 0)
                    svc.metrics.count('extraction_failed')
                    svc._save(user, mem)
                    return False
                try:
                    original_llm, original_conflict = mem.llm, mem.engine.conflict
                    try:
                        if tape is not None:
                            mem.llm = tape
                            mem.engine.conflict = copy(original_conflict)
                            if original_conflict.llm is not None:
                                mem.engine.conflict.llm = tape
                        mem._apply_extraction(prepared.windows[0], outputs[0])
                        if tape is not None and tape.missed:
                            raise RuntimeError('Conflict inputs changed during commit')
                    finally:
                        mem.llm, mem.engine.conflict = original_llm, original_conflict
                    tasks.completed(keys, self.clock())
                    from .consolidate.summaries import SummaryTasks
                    SummaryTasks(mem, user).register([mem.episodes_doc.get(k) for k in keys
                        if mem.episodes_doc.get(k).consolidated], self.clock())
                    svc._save(user, mem)
                except Exception:
                    # write_lock discards a possibly dirty backend; the running checkpoint is recoverable.
                    svc.metrics.count('consolidation_failed')
                    raise
                svc.metrics.count('buffered_windows_completed')
                svc.metrics.observe('buffered_extraction', outputs[0].latency_ms / 1000)
                with self.condition:
                    self.condition.notify_all()
                return True

    def flush(self, user, session_id=None, timeout=30, retry_failed=False, boundary=None):
        from ._messages import flush as flush_messages
        from .consolidate.messages import MessageTasks
        ids = set()
        with self.service._user_file_lock(user):
            if self.service._store_version(user) is not None:
                with self.service._read_scope(user) as mem:
                    ids = set(MessageTasks(mem, user).rows(session_id, boundary))
        started = self.monotonic()
        messages = flush_messages(self.service, user, session_id, timeout, retry_failed, ids) if ids else None
        result = self._flush_raw(user, session_id, max(0, timeout - (self.monotonic() - started)), retry_failed, boundary)
        if messages is not None:
            result['messages'] = messages
            status = ('timed_out' if 'timed_out' in (result['status'], messages['status']) else
                      'failed' if 'failed' in (result['status'], messages['status']) else 'completed')
            result.update(ok=status == 'completed', status=status)
        return result

    def _flush_raw(self, user, session_id=None, timeout=30, retry_failed=False, boundary=None):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 300:
            raise ValueError('timeout must be between 0 and 300 seconds')
        svc = self.service
        if svc._frames():
            raise RuntimeError('Cannot flush while holding a namespace transaction')
        with svc.write_lock(user):
            if svc._store_version(user) is None:
                result = ProcessingTasks.empty_snapshot(session_id)
                return dict(ok=not bool(boundary), status='failed' if boundary else 'completed', **result)
            mem = svc.get(user)
            tasks = ProcessingTasks(mem, user)
            ids = set(tasks.rows(session_id))
            if boundary is not None:
                ids.intersection_update(boundary)
            tasks.force(ids, self.clock(), retry_failed)
            generation = tasks.state['generation'] if tasks.state else None
            svc._save(user, mem)
        self.notify(user)
        deadline = self.monotonic() + timeout
        while True:
            result = self.status(user, session_id, ids)
            counts = result['counts']
            invalidated = ids and (result['generation'] != generation or len(result['jobs']) != len(ids))
            state = ('failed' if invalidated or counts['failed'] or counts['cancelled'] else
                     'completed' if not any(counts[s] for s in ACTIVE) else 'timed_out')
            if state != 'timed_out' or self.monotonic() >= deadline:
                return dict(ok=state == 'completed', status=state, **result)
            with self.condition:
                self.condition.wait(min(.1, max(0, deadline - self.monotonic())))

    def consume(self):
        while not self.abort.is_set():
            if self.monotonic() - self.last_scan >= 5:
                try:
                    self.scan()
                except Exception:
                    self.last_scan = self.monotonic()
                    self.recovery_error = True
                    self.service.metrics.count('buffered_recovery_failed')
                    logging.getLogger(__name__).warning('Buffered recovery failed; inspect namespace storage')
            with self.condition:
                users = list(self.users.items())
            progress = False
            for user, wake_revision in users:
                try:
                    progress |= self.run_one(user)
                except Exception:
                    self.service.metrics.count('buffered_processing_failed')
                    logging.getLogger(__name__).warning('Buffered extraction failed; durable work remains recoverable')
                with self.condition:
                    if user in self.users:
                        self.users.move_to_end(user)
                if not self.abort.is_set():
                    try:
                        with self.service._user_file_lock(user):
                            active = False
                            if os.path.isfile(os.path.join(self.service._path(user), 'manifest.json')):
                                counts = self.status(user)['counts']
                                active = any(counts[s] for s in ACTIVE)
                                with self.service._read_scope(user) as mem:
                                    from .consolidate.summaries import SummaryTasks
                                    summary_counts = SummaryTasks(mem, user).snapshot()['counts']
                                    active |= any(summary_counts[s] for s in ACTIVE)
                                    from .consolidate.messages import MessageTasks
                                    message_counts = MessageTasks(mem, user).snapshot()['counts']
                                    active |= any(message_counts[s] for s in ACTIVE)
                        if not active:
                            with self.condition:
                                if self.users.get(user) == wake_revision:
                                    self.users.pop(user, None)
                                    self.last_group.pop(user, None)
                    except Exception:
                        pass  # Recovery loop reports storage faults; it must retain the wakeup.
                if self.abort.is_set():
                    break
            if not progress:
                with self.condition:
                    if self.stopping:
                        break
                    self.condition.wait(.25)

    def close(self, timeout=30):
        self.stopping = True
        with self.condition:
            self.condition.notify_all()
        if self.thread.ident is not None and threading.current_thread() is not self.thread and not self.service._frames():
            self.thread.join(timeout)
        self.abort.set()
