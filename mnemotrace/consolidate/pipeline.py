"""In-process ledger for dialogue and note operations."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
import time
import uuid

from ..types import Episode
from ..extraction import ExtractionError

STATES = ('pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')
TERMINAL = {'completed', 'failed', 'cancelled'}


class MessageConflictError(ExtractionError):
    code = 'message_conflict'
    status_code = 409
    message = 'This message ID already identifies different input.'


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def route(session_id, extract, source_kind='dialogue'):
    return json.dumps([session_id, extract, source_kind], ensure_ascii=False)


def message_fingerprint(message, extract):
    return content_hash([message['role'], message['content'], message['event_time'], extract])


def normalized(messages, session_id, extract):
    if (not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512
            or type(extract) is not bool):
        raise ValueError('Invalid session or extract flag')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 1000:
        raise ValueError('messages must contain 1 to 1000 complete messages')
    rows, seen = [], {}
    for value in messages:
        if not isinstance(value, dict) or set(value) - {'message_id', 'role', 'content', 'event_time'}:
            raise ValueError('Invalid message fields')
        row = {**value, 'event_time': value.get('event_time')}
        if (not isinstance(row.get('message_id'), str) or not row['message_id'].strip()
                or len(row['message_id']) > 512 or row.get('role') not in {'user', 'assistant'}
                or not isinstance(row.get('content'), str) or not row['content'].strip()
                or len(row['content']) > 1_000_000):
            raise ValueError('Invalid message ID, role or content')
        row['content'].encode('utf-8')
        stamp = row['event_time']
        if stamp is not None and (type(stamp) not in (int, float) or not math.isfinite(stamp)):
            raise ValueError('event_time must be finite or unknown')
        row['event_time'] = float(stamp) if stamp is not None else None
        fingerprint = message_fingerprint(row, extract)
        if row['message_id'] in seen and seen[row['message_id']] != fingerprint:
            raise MessageConflictError()
        seen[row['message_id']] = fingerprint
        rows.append(row)
    return rows


def source_fingerprint(ep):
    return content_hash([ep.user_id, ep.session_id, ep.message_id, ep.sequence, ep.role,
                   ep.event_time, ep.ingested_at, ep.content, ep.source_kind, ep.extract])


def config_identity(mem, *, include_embedding=True):
    # Stable across restarts; runtime provider object identity is checked separately.
    options = {k: v for k, v in asdict(mem.config).items()
               if k.startswith(('extraction_', 'joint_', 'digest_')) or k in {
                   'conflict_sim_threshold', 'max_hot_facts', 'salience_decay_per_day'}}
    llm = mem.llm
    def setting(value, depth=0):
        if value is None or type(value) in (str, int, float, bool):
            return value
        if depth < 4 and isinstance(value, (list, tuple)):
            return [setting(v, depth + 1) for v in value]
        if depth < 4 and isinstance(value, dict) and all(isinstance(k, str) for k in value):
            return {k: setting(v, depth + 1) for k, v in value.items()}
        return [type(value).__module__, type(value).__qualname__]
    def provider_identity(model, seen=()):
        if id(model) in seen:
            raise ValueError('Cyclic model wrapper configuration')
        from ..llm.execution import ModelMeter
        if isinstance(model, ModelMeter):
            return provider_identity(model.inner, (*seen, id(model)))
        identity = [type(model).__module__, type(model).__qualname__,
                    *[setting(getattr(model, k, None)) for k in ('model', 'temperature', 'max_tokens', 'kwargs', 'model_tag')]]
        from ..llm.cache import CachedLLM
        if isinstance(model, CachedLLM):
            identity.append(provider_identity(model.inner, (*seen, id(model))))
        return identity
    provider = provider_identity(llm)
    embedder = mem.embedder
    embedding = None if embedder is None else [
        type(embedder).__module__, type(embedder).__qualname__, embedder.dim,
        *[setting(getattr(embedder, k, None)) for k in ('model', 'model_name', 'model_id')],
    ]
    return content_hash([options, mem.policy, provider, embedding] if include_embedding else [options, mem.policy, provider])


def reset_attempts(window, field, reason, now):
    stage = 'reset_model' if field == 'attempts' else 'reset_embed'
    window['history'].append(dict(stage=stage, attempt=0, started_at=now, finished_at=now,
        outcome='reset', finish_reason=None, usage=None, error_code=reason))
    window[field] = 0


class PipelineTasks:
    def __init__(self, memory, namespace):
        self.memory, self.namespace = memory, namespace

    @property
    def state(self):
        return (self.memory._pipeline_state or {}).get('namespaces', {}).get(self.namespace)

    def ensure(self):
        if self.memory._pipeline_state is None:
            self.memory._pipeline_state = dict(namespaces={}, profiles={})
        if self.state is None:
            self.memory._pipeline_state['namespaces'][self.namespace] = dict(namespace=self.namespace,
                generation=uuid.uuid4().hex, sequences={}, sources={}, sessions={}, windows={})
        return self.state

    def rows(self, session_id=None, ids=None):
        return {k: r for k, r in (self.state or {}).get('sources', {}).items()
                if (session_id is None or r['session_id'] == session_id) and (ids is None or k in ids)}

    def group(self, row, timestamp=0):
        key = route(row['session_id'], row['extract'], row['source_kind'])
        return self.ensure()['sessions'].setdefault(key, dict(session_id=row['session_id'], extract=row['extract'],
            source_kind=row['source_kind'], last_activity=timestamp, cutoff=0, following=0, last_turn=0))

    def inspect(self, messages, session_id, extract=True):
        rows = normalized(messages, session_id, extract)
        existing = {(r['session_id'], r['message_id']): r for r in self.rows().values() if r['source_kind'] == 'dialogue'}
        unseen = {}
        for row in rows:
            old = existing.get((session_id, row['message_id']))
            fingerprint = message_fingerprint(row, extract)
            if old and old['fingerprint'] != fingerprint:
                raise MessageConflictError()
            if old is None:
                unseen[fingerprint] = row['content']
        return rows, unseen

    def _record(self, ep, *, fingerprint, sequence, timestamp, requested):
        row = dict(session_id=ep.session_id, message_id=ep.message_id, fingerprint=fingerprint, source=source_fingerprint(ep),
            length=len(ep.content), sequence=sequence, extract=ep.extract, source_kind=ep.source_kind,
            core='pending' if requested else 'not_requested', digest='completed' if ep.digest else
                'pending' if requested and len(ep.content) >= self.memory.config.digest_min_chars else 'not_needed',
            window_ids=[], cancelled=False, position=0, parts=[], digest_requested=False, commits=[], digest_receipt=None)
        state = self.ensure()
        state['sources'][ep.id] = row
        state['sequences'][ep.session_id] = sequence
        self.group(row, timestamp)['last_activity'] = timestamp
        return row

    def accept(self, messages, session_id, extract=True, timestamp=0):
        rows, _ = self.inspect(messages, session_id, extract)
        existing = {(r['session_id'], r['message_id']): key for key, r in self.rows().items() if r['source_kind'] == 'dialogue'}
        output, fresh = [], []
        for row in rows:
            key = (session_id, row['message_id'])
            if key in existing:
                output.append(existing[key])
                continue
            sequence = self.ensure()['sequences'].get(session_id, 0) + 1
            ep = Episode(content=row['content'], role=row['role'], event_time=row['event_time'], ingested_at=timestamp,
                user_id=self.namespace, session_id=session_id, message_id=row['message_id'], sequence=sequence,
                source_kind='dialogue', extract=extract, consolidated=not extract)
            self.memory._sync_episode(ep)
            self._record(ep, fingerprint=message_fingerprint(row, extract), sequence=sequence, timestamp=timestamp, requested=extract)
            existing[key] = ep.id
            output.append(ep.id)
            fresh.append(ep.id)
        return output, fresh

    def register(self, episodes, timestamp, operation='extract'):
        ids = []
        for ep in episodes:
            if ep.user_id != self.namespace:
                raise ValueError('Source ownership mismatch')
            if operation == 'extract' and not ep.extract:
                continue
            state = self.ensure()
            row = state['sources'].get(ep.id)
            if row is None:
                seq = state['sequences'].get(ep.session_id, 0) + 1
                row = self._record(ep, fingerprint=source_fingerprint(ep), sequence=seq, timestamp=timestamp, requested=False)
            if row['cancelled'] or source_fingerprint(ep) != row['source']:
                raise ValueError('Source changed; register a new event identity')
            if operation == 'extract' and row['core'] == 'not_requested' and not ep.consolidated:
                row['core'] = 'pending'
            if operation == 'digest' and len(ep.content) >= self.memory.config.digest_min_chars and not ep.digest:
                if row['digest'] == 'not_needed':
                    row['digest'] = 'pending'
                row['digest_requested'] = row['core'] != 'pending'
            ids.append(ep.id)
        return ids

    def trigger(self, session, cutoff):
        if cutoff <= 0:
            return
        if not session['cutoff']:
            session['cutoff'] = cutoff
        elif cutoff > session['cutoff']:
            session['following'] = max(session['following'], cutoff)

    def force(self, session_id=None, ids=None, retry_failed=False, operation=None):
        selected = self.rows(session_id, ids)
        if retry_failed and operation in {None, 'digest'}:
            for row in selected.values():
                # Erasure removes the cached window and its plaintext. An explicit
                # retry can prepare missing digests from the surviving source.
                if (not row['cancelled'] and row['digest'] == 'failed' and not any(
                        self.state['windows'][key]['state'] != 'completed'
                        and self.state['windows'][key]['operation'] in {'extract', 'digest'}
                        for key in row['window_ids'])):
                    row.update(digest='pending', digest_requested=True)
        cutoffs = {}
        for row in selected.values():
            if not row['cancelled'] and (row['core'] != 'not_requested' or row['digest'] == 'pending'):
                key = route(row['session_id'], row['extract'], row['source_kind'])
                cutoffs[key] = max(cutoffs.get(key, 0), row['sequence'])
        for key, cutoff in cutoffs.items():
            self.trigger(self.state['sessions'][key], cutoff)
        if retry_failed and self.state:
            for win in self.state['windows'].values():
                if operation == 'digest' and not (win['operation'] == 'digest' or win['operation'] == 'extract' and win['core_saved']):
                    continue
                if win['state'] not in {'failed', 'cancelled', 'retry_wait'} or not any(v['id'] in selected for v in win['input']['pending']):
                    continue
                changed = win['guard'] != self.state['generation'] or win['config'] != config_identity(self.memory)
                if changed and not win['core_saved']:
                    win.update(state='cancelled', error_code='explicit_reprepare')
                    for item in win['input']['pending']:
                        row = self.state['sources'][item['id']]
                        if not row['cancelled']:
                            row['core'] = 'pending' if win['operation'] == 'extract' else row['core']
                            if row['digest'] == 'failed':
                                row['digest'] = 'pending'
                    continue
                win.update(guard=self.state['generation'], config=config_identity(self.memory),
                    generation_config=config_identity(self.memory, include_embedding=False),
                    state='pending', next_attempt_at=0, error_code=None)
                reset_attempts(win, 'embed_attempts', 'explicit_retry', time.time())
                if win['phase'] in {'generate', 'compensate'}:
                    reset_attempts(win, 'attempts', 'explicit_retry', time.time())
                for item in win['input']['pending']:
                    row = self.state['sources'][item['id']]
                    if row['core'] == 'failed':
                        row['core'] = 'pending'
                    if row['digest'] == 'failed':
                        row['digest'] = 'pending'

    def cancel_missing(self):
        if not self.state:
            return
        for key, row in self.rows().items():
            ep = self.memory.episodes_doc.get(key)
            if ep is None or source_fingerprint(ep) != row['source']:
                row['cancelled'] = True
        erased_windows = {}
        changed = True
        while changed:
            changed = False
            for key, win in self.state['windows'].items():
                erased = {v['id'] for v in (*win['input']['pending'], *win['input']['context'])
                          if self.state['sources'][v['id']]['cancelled']}
                for parent in win['input']['parts']:
                    erased.update(erased_windows.get(parent, ()))
                if erased and erased_windows.get(key) != erased:
                    erased_windows[key] = erased
                    changed = True
        for key, erased in erased_windows.items():
            for row in self.rows().values():
                for commit in row['commits']:
                    if commit['key'] == key:
                        commit['erased_sources'] = sorted(erased)
                if row['digest_receipt'] is not None and row['digest_receipt']['key'] == key:
                    row['digest_receipt']['erased_sources'] = sorted(erased)
                if key in row['window_ids']:
                    row['window_ids'].remove(key)
                    row['parts'] = [p for p in row['parts'] if p['key'] != key]
                    if row['digest'] == 'pending':
                        row['digest'] = 'failed'
            del self.state['windows'][key]

    def cancel_window(self, win, code):
        for attempt in win['history']:
            if attempt['outcome'] == 'in_flight':
                attempt.update(outcome='discarded', finished_at=time.time(), error_code=code)
        win.update(state='cancelled', error_code=code)
        rows = self.state['sources']
        for item in win['input']['pending']:
            row = rows[item['id']]
            if win['operation'] == 'extract' and not win['core_saved']:
                row['core'] = 'failed'
            if row['digest'] == 'pending':
                row['digest'] = 'failed'

    def invalidate(self, session_id=None):
        if not self.state:
            return
        self.state['generation'] = uuid.uuid4().hex
        for win in self.state['windows'].values():
            if win['state'] not in TERMINAL:
                self.cancel_window(win, 'manual_change')

    def snapshot(self, session_id=None, ids=None):
        jobs = []
        # Persistence sorts keys, so raw dict order is not stable across a restart.
        # Report dialogue/raw jobs by their session sequence for a stable contract.
        rows = sorted(self.rows(session_id, ids).items(), key=lambda item: item[1]['sequence'])
        for key, row in rows:
            wins = [self.state['windows'][k] for k in row['window_ids'] if self.state['windows'][k]['operation'] in {'extract', 'digest'}]
            active = next((w for w in reversed(wins) if w['state'] not in TERMINAL), None)
            failed = next((w for w in reversed(wins) if w['state'] in {'failed', 'cancelled'}), None)
            complete = row['core'] in {'completed', 'not_requested'} and row['digest'] in {'completed', 'not_needed'}
            win = active or failed or (wins[-1] if wins else None)
            state = 'cancelled' if row['cancelled'] else 'completed' if complete else (active['state'] if active else failed['state'] if failed and row['core'] != 'pending' else 'failed' if row['core'] == 'failed' or row['digest'] == 'failed' else 'pending')
            jobs.append(dict(episode_id=key, message_id=row['message_id'], source_kind=row['source_kind'], operation=win['operation'] if win else 'extract',
                sequence=row['sequence'], state=state, phase=win['phase'] if win else 'queued', core=row['core'],
                summarization=row['digest'], coverage=deepcopy(self.memory.episodes_doc.get(key).coverage) if self.memory.episodes_doc.get(key) else None,
                error_code=win['error_code'] if win else 'source_changed' if state == 'failed' else None, attempts=win['attempts'] if win else 0,
                embed_attempts=win['embed_attempts'] if win else 0))
        return dict(session_id=session_id, generation=self.state['generation'] if self.state else None,
                    counts={s: sum(j['state'] == s for j in jobs) for s in STATES}, jobs=jobs)

    def response(self, ids):
        jobs = {j['episode_id']: j for j in self.snapshot(ids=set(ids))['jobs'] if 'core' in j}
        return dict(ok=True, messages=[deepcopy(jobs[k]) for k in ids])
