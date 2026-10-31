"""Snapshot-backed buffered work. Only service transactions may mutate this adapter."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import time
import uuid

STATES = {'pending', 'running', 'retry_wait', 'failed', 'completed', 'cancelled'}
ACTIVE = {'pending', 'running', 'retry_wait'}
ERRORS = {None, 'extraction_failed', 'extraction_invalid_response', 'extraction_truncated',
          'llm_not_configured', 'source_changed', 'cancelled', 'storage_failed'}


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def validate_tasks(value):
    """Absent feature state is legal; present state is never repaired on load."""
    if value is None:
        return None
    error = ValueError('manifest.state.processing: invalid buffered processing state')
    if not isinstance(value, dict) or set(value) != {'namespace', 'generation', 'revision', 'jobs'}:
        raise error
    if any(not isinstance(value[k], str) or not value[k] for k in ('namespace', 'generation')):
        raise error
    if type(value['revision']) is not int or value['revision'] < 0 or not isinstance(value['jobs'], dict):
        raise error
    for key, job in value['jobs'].items():
        if not isinstance(key, str) or not key or not isinstance(job, dict) or set(job) != {
            'session_id', 'scope', 'content_hash', 'state', 'attempts', 'window_attempts', 'next_attempt_at',
            'queued_at', 'updated_at', 'error_code', 'forced', 'completed_through', 'source_length'
        }:
            raise error
        if (not isinstance(job['session_id'], str) or not job['session_id']
                or not isinstance(job['scope'], str) or job['scope'] not in {'auto', 'long', 'working'}
                or not isinstance(job['state'], str) or job['state'] not in STATES
                or not isinstance(job['content_hash'], str) or len(job['content_hash']) != 64
                or any(c not in '0123456789abcdef' for c in job['content_hash'])
                or not isinstance(job['error_code'], (str, type(None))) or job['error_code'] not in ERRORS
                or type(job['forced']) is not bool):
            raise error
        for field in ('attempts', 'window_attempts', 'completed_through', 'source_length'):
            if type(job[field]) is not int or job[field] < 0:
                raise error
        for field in ('next_attempt_at', 'queued_at', 'updated_at'):
            if type(job[field]) not in (int, float) or not math.isfinite(job[field]) or job[field] < 0:
                raise error
        if job['window_attempts'] > 3 or job['window_attempts'] > job['attempts']:
            raise error
        if job['completed_through'] > job['source_length'] or (
                job['state'] == 'completed' and job['completed_through'] != job['source_length']):
            raise error
    return value


def validate_task_sources(state, episodes):
    validate_tasks(state)
    if state is None:
        return
    rows = {ep.id: ep for ep in episodes}
    for key, job in state['jobs'].items():
        ep = rows.get(key)
        if ep is None or (ep.user_id, ep.session_id, ep.metadata.get('memory_scope', 'long'),
                          text_hash(ep.content), len(ep.content)) != (
                state['namespace'], job['session_id'], job['scope'], job['content_hash'], job['source_length']):
            raise ValueError('manifest.state.processing: invalid source identity')
        from .processing import validate_progress
        progress = validate_progress(ep.content, ep.metadata)
        offset = progress['completed_through'] if progress else (
            len(ep.content) if ep.consolidated and ep.metadata.get('ephemeral') else 0)
        if offset != job['completed_through'] or (job['state'] == 'completed') != ep.consolidated:
            raise ValueError('manifest.state.processing: source/checkpoint commit mismatch')


class ProcessingTasks:
    @staticmethod
    def empty_snapshot(session_id=None):
        return dict(enabled=True, session_id=session_id, generation=None,
                    counts={state: 0 for state in sorted(STATES)}, jobs=[])

    @staticmethod
    def refresh_completed(memory, ids, timestamp):
        state = validate_tasks(memory._processing_state)
        if state is None:
            return
        tasks = ProcessingTasks(memory, state['namespace'])
        for key, job in tasks.rows(ids=ids).items():
            ep = tasks.sources([key])[0]
            if ep.consolidated or memory.engine.processing.offset(ep) > job['completed_through']:
                tasks.completed([key], timestamp)

    @staticmethod
    def registered_ids(memory):
        state = validate_tasks(memory._processing_state)
        return set(state['jobs']) if state else set()

    def __init__(self, memory, namespace):
        self.memory, self.namespace = memory, namespace
        from ..store import StoreFormatError
        try:
            state = validate_tasks(memory._processing_state)
            if state is not None and state['namespace'] != namespace:
                raise ValueError('Buffered processing namespace ownership mismatch')
        except ValueError as exc:
            raise StoreFormatError(str(exc)) from exc

    @property
    def state(self):
        return self.memory._processing_state

    def ensure(self):
        if self.state is None:
            self.memory._processing_state = dict(namespace=self.namespace, generation=uuid.uuid4().hex,
                                                 revision=0, jobs={})
        return self.state

    def rows(self, session_id=None, ids=None):
        jobs = self.state['jobs'] if self.state else {}
        return {k: v for k, v in jobs.items() if (session_id is None or v['session_id'] == session_id)
                and (ids is None or k in ids)}

    def enqueue(self, ep, timestamp):
        self.ensure()['jobs'][ep.id] = dict(session_id=ep.session_id,
            scope=ep.metadata.get('memory_scope', 'long'), content_hash=text_hash(ep.content),
            state='completed' if ep.consolidated else 'pending', attempts=0, window_attempts=0, next_attempt_at=0,
            queued_at=timestamp, updated_at=timestamp, error_code=None, forced=False,
            completed_through=len(ep.content) if ep.consolidated else 0, source_length=len(ep.content))

    def sources(self, ids):
        sources = []
        for key in ids:
            job = self.rows().get(key)
            ep = self.memory.episodes_doc.get(key)
            if ep is None or job is None or ep.user_id != self.namespace:
                raise ValueError('Buffered processing source ownership mismatch')
            if ep.session_id != job['session_id'] or ep.metadata.get('memory_scope', 'long') != job['scope']:
                raise ValueError('Buffered processing source routing mismatch')
            sources.append(ep)
        return sources

    def reconcile(self, timestamp):
        """Called only while the independent processing lease is held."""
        for key, job in self.rows().items():
            if job['state'] in {'cancelled', 'failed'}:
                continue
            ep = self.sources([key])[0]
            if text_hash(ep.content) != job['content_hash']:
                job.update(content_hash=text_hash(ep.content), source_length=len(ep.content),
                           completed_through=0, state='pending', window_attempts=0, error_code=None)
            if ep.consolidated:
                job.update(state='completed', completed_through=len(ep.content), error_code=None, forced=False)
            elif job['state'] == 'running':
                # A lease holder cannot coexist with an active previous worker.
                job.update(state='failed' if job['window_attempts'] >= 3 else 'pending', updated_at=timestamp,
                           error_code='extraction_failed' if job['window_attempts'] >= 3 else None)

    def mark(self, ids, state, timestamp, **fields):
        for job in self.rows(ids=ids).values():
            job.update(state=state, updated_at=timestamp, **fields)

    def cancel(self, timestamp, session_id=None):
        from .summaries import SummaryTasks
        SummaryTasks(self.memory, self.namespace).cancel(timestamp, session_id)
        if self.state:
            self.state['revision'] += 1
            for job in self.rows(session_id).values():
                if job['state'] != 'completed':
                    job.update(state='cancelled', error_code='cancelled', updated_at=timestamp, forced=False)

    def revise(self):
        from .summaries import SummaryTasks
        SummaryTasks(self.memory, self.namespace).cancel(time.time())
        if self.state:
            self.state['revision'] += 1

    def completed(self, ids, timestamp):
        for key, job in self.rows(ids=ids).items():
            ep = self.sources([key])[0]
            offset = self.memory.engine.processing.offset(ep)
            job.update(state='completed' if ep.consolidated else 'pending', completed_through=offset,
                       window_attempts=0, next_attempt_at=0, error_code=None, updated_at=timestamp,
                       forced=False if ep.consolidated else job['forced'])

    def force(self, ids, timestamp, retry_failed=False):
        for job in self.rows(ids=ids).values():
            if retry_failed and job['state'] in {'failed', 'cancelled'}:
                job.update(state='pending', window_attempts=0, next_attempt_at=0, error_code=None)
            if job['state'] in ACTIVE:
                job.update(forced=True, updated_at=timestamp)

    def snapshot(self, session_id=None, ids=None):
        jobs = self.rows(session_id, ids)
        return dict(enabled=True, session_id=session_id,
                    generation=self.state['generation'] if self.state else None,
                    counts={state: sum(j['state'] == state for j in jobs.values()) for state in sorted(STATES)},
                    jobs=[dict(episode_id=k, **deepcopy(v)) for k, v in jobs.items()])
