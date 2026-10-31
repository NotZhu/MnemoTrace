"""Service orchestration for summary jobs; model work never holds a namespace lock."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import time

from ._namespace_lock import namespace_lock
from .consolidate.summaries import (
    ACTIVE, SummaryError, SummaryTasks, advance, fingerprint, prepare_step, seal_progress, source_identity,
)
from .util import now


def timestamp(service):
    return service._buffered.clock() if service._buffered else now()


def register(service, user, ids, retry_failed=False):
    with service.write_lock(user):
        if service._store_version(user) is None:
            return
        mem = service.get(user)
        tasks = SummaryTasks(mem, user)
        episodes = [ep for ep in mem.episodes_doc.values() if ep.id in ids]
        if tasks.register(episodes, timestamp(service), retry_failed):
            service._save(user, mem)
    if service._buffered:
        service._buffered.notify(user)


def run_one(service, user, ids=None):
    lease = namespace_lock(service._contained_child('@' + service._safe_user(user) + '.processing.lock'))
    with lease.try_file_lock() as acquired:
        if not acquired or service._closed:
            return False
        with service.write_lock(user):
            if service._store_version(user) is None:
                return False
            mem = service.get(user)
            tasks = SummaryTasks(mem, user)
            eligible = [ep for ep in mem.episodes_doc.values()
                        if ep.consolidated and (ids is None or ep.id in ids)]
            # Explicit requests register their own set; the worker only handles durable jobs.
            if tasks.state:
                eligible = [ep for ep in eligible if ep.id in tasks.rows()]
                changed = tasks.register(eligible, timestamp(service))
            else:
                changed = False
            key = None
            for candidate, row in tasks.rows(ids=ids).items():
                if row['state'] == 'running':
                    # Holding the process-shared lease proves a previous owner has stopped.
                    row.update(state='failed' if row['step_attempts'] >= 3 else 'pending',
                               error_code='summary_failed' if row['step_attempts'] >= 3 else None)
                    changed = True
                if row['state'] in ACTIVE and row['next_attempt_at'] <= timestamp(service):
                    key = candidate
                    break
            if key is None:
                if changed:
                    service._save(user, mem)
                return False
            job = tasks.rows()[key]
            ep = mem.episodes_doc.get(key)
            if ep is None or source_identity(ep) != job['source']:
                job.update(state='cancelled', error_code='source_changed')
                service._save(user, mem)
                return True
            try:
                step = (None if job['phase'] == 'embed' else
                        prepare_step(ep, job, mem.config, mem._effective('summary_system')))
            except SummaryError as exc:
                job.update(state='failed', error_code=exc.code, updated_at=timestamp(service))
                service._save(user, mem)
                return True
            job.update(state='running', attempts=job['attempts'] + 1,
                       step_attempts=job['step_attempts'] + 1, error_code=None,
                       updated_at=timestamp(service))
            token = (tasks.state['generation'], tasks.state['revision'])
            claimed = deepcopy(job)
            config, llm, embedder = replace(mem.config), service.llm, mem.embedder
            source_length = len(ep.content)
            service._save(user, mem)
            mem = ep = tasks = job = None
        failure, text, vector = None, None, None
        try:
            from ._buffered import ModelMeter, one_attempt
            if step is None:
                text = claimed['pieces'][0]
                if len(text) > config.summary_max_chars or len(text) >= source_length:
                    raise SummaryError('summary_invalid_response')
                vector = embedder.embed(text)
                updated = deepcopy(claimed)
                updated.update(state='completed', phase='source', position=source_length,
                               pieces=[], outputs=[], step_attempts=0, next_attempt_at=0, error_code=None)
                seal_progress(updated)
            else:
                result, _usage = step.execute(ModelMeter(one_attempt(llm), service.metrics, 'summary')
                                             if llm is not None else None, config)
                updated, _final = advance(claimed, step, result, source_length)
        except SummaryError as exc:
            failure = exc
        except Exception as exc:
            failure = SummaryError(retryable=not isinstance(exc, (ValueError, TypeError, ImportError)))
        if service._closed or service._buffered and service._buffered.abort.is_set():
            return False
        with service.write_lock(user):
            if service._store_version(user) is None:
                return False
            mem = service.get(user)
            tasks = SummaryTasks(mem, user)
            current = tasks.rows().get(key)
            ep = mem.episodes_doc.get(key)
            if (tasks.state is None or (tasks.state['generation'], tasks.state['revision']) != token
                    or current != claimed or ep is None or source_identity(ep) != claimed['source']):
                service.metrics.count('summary_results_discarded')
                return False
            if service.llm is not llm or fingerprint(mem, service.llm) != claimed['config']:
                tasks.register([ep], timestamp(service), retry_failed=True)
                service._save(user, mem)
                service.metrics.count('summary_results_discarded')
                return True
            if failure is not None:
                retry = failure.retryable and current['step_attempts'] < 3
                current.update(state='retry_wait' if retry else 'failed', error_code=failure.code,
                    updated_at=timestamp(service), next_attempt_at=timestamp(service) + (
                        30 if current['step_attempts'] == 1 else 120) if retry else 0)
                service.metrics.count('summary_failed')
            else:
                updated['updated_at'] = timestamp(service)
                tasks.state['jobs'][key] = updated
                if text is not None:
                    ep.summary, ep.summary_embedding = text, vector
                    mem._sync_episode(ep)
                    service.metrics.count('summaries_completed')
            service._save(user, mem)
        return True


def drain(service, user, ids, retry_failed=True):
    # Serialize with a worker, without retaining a namespace lock during the wait/model.
    with service._processing_scope(user):
        register(service, user, ids, retry_failed)
        while run_one(service, user, ids):
            pass
        if service._store_version(user) is None:
            return {key: {'status': 'cancelled', 'error_code': 'source_changed'} for key in ids}
        with service._read_scope(user) as mem:
            tasks = SummaryTasks(mem, user)
            return {key: tasks.result(key) for key in ids}


def flush(service, user, session_id, timeout, retry_failed):
    if type(timeout) not in (int, float) or not 0 <= timeout <= 300:
        raise ValueError('timeout must be between 0 and 300 seconds')
    deadline = time.monotonic() + timeout
    if service._store_version(user) is None:
        result = service.flush(user, session_id, timeout, retry_failed)
        result['summarization'] = service.processing_status(user, session_id)['summarization']
        return result
    with service._read_scope(user) as mem:
        boundary = {ep.id for ep in mem.episodes_doc.values()
                    if ep.user_id == user and (session_id is None or ep.session_id == session_id)}
    if service._buffered:
        result = service._buffered.flush(user, session_id, timeout, retry_failed, boundary=boundary)
    else:
        result = service.flush(user, session_id, timeout, retry_failed)
    if not result['ok']:
        if service._store_version(user) is None:
            result['summarization'] = {'counts': {}, 'jobs': []}
        else:
            with service._read_scope(user) as mem:
                result['summarization'] = SummaryTasks(mem, user).snapshot(ids=boundary)
        return result
    register(service, user, boundary, retry_failed)
    while True:
        if service._store_version(user) is None:
            result.update(ok=False, status='failed')
            return result
        with service._read_scope(user) as mem:
            tasks = SummaryTasks(mem, user)
            statuses = [tasks.result(key)['status'] for key in boundary]
            result['summarization'] = tasks.snapshot(ids=boundary)
        status = ('failed' if any(s in {'failed', 'cancelled'} for s in statuses) else
                  'completed' if all(s in {'completed', 'not_needed'} for s in statuses) else 'timed_out')
        if status != 'timed_out' or time.monotonic() >= deadline or service._buffered is None:
            if service._buffered is None and status == 'timed_out':
                status = 'failed'
            result.update(ok=status == 'completed', status=status)
            return result
        with service._buffered.condition:
            service._buffered.condition.wait(min(.1, max(0, deadline - time.monotonic())))


def attempt(service, user, ids, retry_failed=True):
    """The primary write is already committed; report summary-stage failures separately."""
    from .store import PersistenceError
    try:
        return drain(service, user, ids, retry_failed)
    except Exception as exc:
        service.metrics.count('summary_processing_failed')
        code = 'storage_failed' if isinstance(exc, (PersistenceError, OSError)) else 'summary_failed'
        return {key: {'status': 'failed', 'error_code': code} for key in ids}
