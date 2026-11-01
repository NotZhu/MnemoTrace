"""Durable dialogue identities and batches; wake queues carry no business state."""
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

VERSION = 1
TERMINAL = {'completed', 'failed', 'cancelled'}
KINDS = {'user_statement': 'user', 'user_confirmation': 'user',
         'assistant_suggestion': 'assistant', 'assistant_report': 'assistant'}


class MessageConflictError(ExtractionError):
    code = 'message_conflict'
    status_code = 409
    message = 'This message ID already identifies different input.'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def route(session_id, mode, scope):
    return json.dumps([session_id, mode, scope], ensure_ascii=False)


def identity(message, mode, scope):
    return digest([message['role'], message['content'], message['event_time'], mode, scope])


def normalized(messages, session_id, mode, scope):
    if (not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 512
            or mode not in {'live', 'historical'} or scope not in {'auto', 'long', 'raw'}):
        raise ValueError('Invalid session, mode or dialogue scope')
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
        fingerprint = identity(row, mode, scope)
        if row['message_id'] in seen and seen[row['message_id']] != fingerprint:
            raise MessageConflictError()
        seen[row['message_id']] = fingerprint
        rows.append(row)
    return rows


def source(ep):
    return digest([ep.user_id, ep.session_id, ep.message_id, ep.sequence, ep.role,
                   ep.event_time, ep.ingested_at, ep.mode, ep.content])


def config_identity(mem):
    # Stable across restarts; runtime provider object identity is checked separately.
    options = {k: v for k, v in asdict(mem.config).items()
               if k.startswith(('extraction_', 'joint_', 'summary_')) or k in {
                   'llm_topics', 'explicit_preference_extraction', 'preference_object_filter',
                   'preference_object_normalization', 'preference_reversal_extraction',
                   'procedural_extraction', 'conflict_detection', 'conflict_llm_escalation'}}
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
        identity = [type(model).__module__, type(model).__qualname__,
                    *[setting(getattr(model, k, None)) for k in ('model', 'temperature', 'max_tokens', 'kwargs', 'model_tag')]]
        from ..llm.cache import CachedLLM
        if isinstance(model, CachedLLM):
            identity.append(provider_identity(model.inner, (*seen, id(model))))
        return identity
    provider = provider_identity(llm)
    embedder = mem.embedder
    embedding = [type(embedder).__module__, type(embedder).__qualname__, embedder.dim,
                 *[setting(getattr(embedder, k, None)) for k in ('model', 'model_name')]]
    return digest([options, mem.policy, provider, embedding])


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reset_attempts(window, field, reason, now):
    stage = 'reset_model' if field == 'attempts' else 'reset_embed'
    window['history'].append(dict(stage=stage, attempt=0, started_at=now, finished_at=now,
        outcome='reset', finish_reason=None, usage=None, error_code=reason))
    window[field] = 0


def validate_state(value, episodes=None, embedding_dim=None):
    try:
        if value is None:
            return _validate_state(value, episodes, embedding_dim)
        if not isinstance(value, dict) or set(value) != {'version', 'namespaces'} or value['version'] != VERSION:
            raise ValueError('manifest.state.messages: invalid namespace envelope')
        if not isinstance(value['namespaces'], dict):
            raise ValueError('manifest.state.messages: invalid namespace map')
        documents = list(episodes) if episodes is not None else None
        if documents is not None and any(e.message_id is not None and e.user_id not in value['namespaces'] for e in documents):
            raise ValueError('manifest.state.messages: missing message namespace')
        for namespace, state in value['namespaces'].items():
            if not isinstance(state, dict) or state.get('namespace') != namespace:
                raise ValueError('manifest.state.messages: namespace ownership mismatch')
            _validate_state(state, [e for e in documents if e.user_id == namespace] if documents is not None else None,
                            embedding_dim)
        return value
    except (TypeError, KeyError, AttributeError, IndexError, RecursionError) as exc:
        raise ValueError('manifest.state.messages: malformed checkpoint') from exc


def _validate_state(value, episodes=None, embedding_dim=None):
    """Reject malformed checkpoints before restoring any index. Never infer progress."""
    if value is None:
        if episodes is not None and any(e.message_id is not None for e in episodes):
            raise ValueError('manifest.state.messages: missing dialogue task state')
        return None
    def require(test):
        if not test:
            raise ValueError('manifest.state.messages: invalid message pipeline checkpoint')
    require(isinstance(value, dict) and set(value) == {
        'version', 'namespace', 'generation', 'sequences', 'messages', 'sessions', 'windows'})
    require(type(value['version']) is int and value['version'] == VERSION)
    require(all(isinstance(value[k], str) and value[k] for k in ('namespace', 'generation')))
    require(all(isinstance(value[k], dict) for k in ('sequences', 'messages', 'sessions', 'windows')))
    require(all(isinstance(k, str) and k and type(n) is int and n > 0
                for k, n in value['sequences'].items()))
    docs = {e.id: e for e in episodes} if episodes is not None else None
    if docs is not None:
        require(all(e.message_id is None or e.id in value['messages'] for e in docs.values()))
    identities, sequences = set(), set()
    for key, row in value['messages'].items():
        require(isinstance(key, str) and isinstance(row, dict) and set(row) == {
            'session_id', 'message_id', 'fingerprint', 'source', 'length', 'sequence', 'mode',
            'scope', 'core', 'summary', 'window_id', 'cancelled'})
        require(all(isinstance(row[k], str) and row[k] for k in ('session_id', 'message_id')))
        require(row['mode'] in {'live', 'historical'} and row['scope'] in {'auto', 'long', 'raw'})
        require(row['core'] in {'not_requested', 'pending', 'completed', 'failed'})
        require(row['summary'] in {'not_needed', 'pending', 'completed', 'failed'})
        require(type(row['cancelled']) is bool and type(row['sequence']) is int and row['sequence'] > 0)
        require(type(row['length']) is int and row['length'] >= 0)
        require(all(isinstance(row[k], str) and len(row[k]) == 64 for k in ('fingerprint', 'source')))
        require(row['window_id'] is None or row['window_id'] in value['windows'])
        pair = (row['session_id'], row['message_id'])
        seq = (row['session_id'], row['sequence'])
        require(pair not in identities and seq not in sequences)
        identities.add(pair)
        sequences.add(seq)
        require(row['sequence'] <= value['sequences'].get(row['session_id'], 0))
        require(route(row['session_id'], row['mode'], row['scope']) in value['sessions'])
        if docs is not None and not row['cancelled']:
            ep = docs.get(key)
            require(ep is not None and ep.user_id == value['namespace'] and source(ep) == row['source'])
            require(len(ep.content) == row['length'] and ep.consolidated == (row['core'] in {'completed', 'not_requested'}))
            require(row['summary'] != 'completed' or bool(ep.summary))
            if row['core'] == 'completed':
                require(isinstance(ep.coverage, dict) and set(ep.coverage) == {
                    'unit', 'ranges', 'partial', 'policy', 'source', 'original_length', 'input_hash', 'strategy_completed'})
                require(ep.coverage['unit'] == 'unicode_code_points' and ep.coverage['strategy_completed'] is True)
                require(type(ep.coverage['partial']) is bool and isinstance(ep.coverage['ranges'], list))
                require(isinstance(ep.coverage['input_hash'], str) and len(ep.coverage['input_hash']) == 64)
                expected_policy = 'head_tail_v1' if ep.coverage['partial'] else 'full_message_v1'
                require(ep.coverage['policy'] == expected_policy)
                ranges = ep.coverage['ranges']
                require(bool(ranges) and ranges[0][0] == 0 and ranges[-1][1] == len(ep.content))
                require((len(ranges) == 2 and ranges[0][1] < ranges[1][0]) if ep.coverage['partial'] else ranges == [[0, len(ep.content)]])
                require(ep.coverage.get('source') == row['source'] and ep.coverage.get('original_length') == row['length'])
            require(identity({'role': ep.role, 'content': ep.content, 'event_time': ep.event_time},
                             row['mode'], row['scope']) == row['fingerprint'])
    for key, session in value['sessions'].items():
        require(isinstance(session, dict) and set(session) == {
            'session_id', 'mode', 'scope', 'last_activity', 'cutoff', 'following', 'batch_id', 'last_turn'})
        require(key == route(session['session_id'], session['mode'], session['scope']))
        require(_number(session['last_activity']) and _number(session['last_turn']))
        for field in ('cutoff', 'following'):
            require(type(session[field]) is int and 0 <= session[field] <= value['sequences'].get(session['session_id'], 0))
        require(session['following'] == 0 or session['following'] > session['cutoff'])
        require(isinstance(session['batch_id'], str))
    for key, win in value['windows'].items():
        require(isinstance(win, dict) and set(win) == {
            'route', 'batch_id', 'input', 'input_hash', 'config', 'state', 'phase', 'attempts',
            'embed_attempts', 'next_attempt_at', 'result', 'vectors', 'core_saved', 'summary_saved',
            'error_code', 'history', 'guard', 'limits', 'result_hash'})
        require(win['route'] in value['sessions'] and isinstance(win['batch_id'], str))
        require(isinstance(win['input'], dict) and set(win['input']) == {'context', 'pending', 'mode', 'scope'})
        require(win['input_hash'] == digest(win['input']) and isinstance(key, str) and len(key) == 24)
        require(isinstance(win['config'], str) and len(win['config']) == 64)
        require(win['state'] in {'pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled'})
        require(win['phase'] in {'generate', 'embed', 'commit', 'compensate', 'done'})
        require(all(type(win[k]) is int and 0 <= win[k] <= 3 for k in ('attempts', 'embed_attempts')))
        require(_number(win['next_attempt_at']) and type(win['core_saved']) is bool)
        require(isinstance(win['history'], list) and isinstance(win['summary_saved'], list))
        counts = dict(attempts=0, embed_attempts=0)
        for attempt in win['history']:
            require(isinstance(attempt, dict) and set(attempt) == {
                'stage', 'attempt', 'started_at', 'finished_at', 'outcome', 'finish_reason', 'usage', 'error_code'})
            reset = attempt['stage'] in {'reset_model', 'reset_embed'}
            require(attempt['stage'] in {'generate', 'embed', 'compensate', 'reset_model', 'reset_embed'} and
                    type(attempt['attempt']) is int and (attempt['attempt'] == 0 if reset else 1 <= attempt['attempt'] <= 3))
            field = 'embed_attempts' if attempt['stage'] in {'embed', 'reset_embed'} else 'attempts'
            counts[field] = 0 if reset else counts[field] + 1
            require(attempt['attempt'] == counts[field])
            require(_number(attempt['started_at']) and
                    (attempt['finished_at'] is None or _number(attempt['finished_at'])))
            require(attempt['outcome'] in {'in_flight', 'saved', 'failed', 'discarded', 'interrupted', 'reset'})
            require((attempt['outcome'] == 'reset') == reset)
            from ..llm.base import Completion
            Completion('', attempt['finish_reason'], attempt['usage'])
            require(attempt['error_code'] is None or isinstance(attempt['error_code'], str))
        require(all(win[field] == count for field, count in counts.items()))
        require(win['error_code'] is None or isinstance(win['error_code'], str))
        require(win['result'] is None or isinstance(win['result'], dict))
        require(win['result_hash'] == (digest(win['result']) if win['result'] is not None else None))
        require(isinstance(win['guard'], str) and bool(win['guard']))
        require(win['state'] != 'completed' or (win['phase'] == 'done' and win['core_saved'] and win['result'] is not None))
        require(win['phase'] not in {'embed', 'commit', 'compensate', 'done'} or win['result'] is not None)
        require(win['phase'] != 'compensate' or win['core_saved'])
        require(isinstance(win['vectors'], dict))
        require(isinstance(win['limits'], dict) and set(win['limits']) == {'summary_min_chars', 'summary_max_chars'})
        require(all(type(n) is int and n > 0 for n in win['limits'].values()))
        require(win['limits']['summary_max_chars'] < win['limits']['summary_min_chars'])
        require(win['phase'] == 'generate' or win['attempts'] > 0 or
                win['phase'] == 'compensate' and win['core_saved'])
        if win['result'] is not None:
            from types import SimpleNamespace
            from .message_windows import validate_response
            result = win['result']
            require(set(result) == {'facts', 'working', 'summaries'} and isinstance(result['summaries'], dict))
            wire = {**result, 'summaries': [dict(message_id=k, text=v) for k, v in result['summaries'].items()]}
            try:
                require(validate_response(json.dumps(wire), win['input'], SimpleNamespace(**win['limits'])) == result)
            except ExtractionError as exc:
                raise ValueError('manifest.state.messages: invalid saved model result') from exc
            require(set(win['summary_saved']).issubset(result['summaries']))
            if win['phase'] == 'commit':
                texts = ([] if win['core_saved'] else [f['text'] for f in result['facts']] +
                         [w['content'] for w in result['working']])
                texts.extend(text for mid, text in result['summaries'].items() if mid not in win['summary_saved'])
                require({digest(text) for text in texts} == set(win['vectors']))
        for vector in win['vectors'].values():
            require(isinstance(vector, list) and all(type(n) in (int, float) and math.isfinite(n) for n in vector))
            require(embedding_dim is None or len(vector) == embedding_dim)
        for label in ('context', 'pending'):
            require(isinstance(win['input'][label], list))
            for supplied in win['input'][label]:
                require(isinstance(supplied, dict) and set(supplied) == {
                    'id', 'message_id', 'sequence', 'role', 'event_time', 'representation', 'content',
                    'source', 'source_length', 'ranges', 'partial'})
                require(supplied['id'] in value['messages'])
                require(supplied['role'] in {'user', 'assistant'} and isinstance(supplied['content'], str))
                require(supplied['representation'] in {'raw', 'summary'} and type(supplied['partial']) is bool)
                require(supplied['source'] == value['messages'][supplied['id']]['source'])
                require(type(supplied['source_length']) is int and supplied['source_length'] >= 0)
                require(supplied['source_length'] == value['messages'][supplied['id']]['length'])
                end = 0
                require(isinstance(supplied['ranges'], list))
                for span in supplied['ranges']:
                    require(isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span))
                    require(end <= span[0] < span[1] <= supplied['source_length'])
                    end = span[1]
                if docs is not None and not value['messages'][supplied['id']]['cancelled']:
                    ep = docs[supplied['id']]
                    require((supplied['role'], supplied['event_time'], supplied['sequence'], supplied['message_id']) ==
                            (ep.role, ep.event_time, ep.sequence, ep.message_id))
                    if supplied['representation'] == 'raw':
                        from .message_windows import OMITTED
                        visible = OMITTED.join(ep.content[start:end] for start, end in supplied['ranges'])
                        require(visible == supplied['content'])
                    if label == 'pending' and win['core_saved']:
                        require(ep.coverage['ranges'] == supplied['ranges'] and ep.coverage['partial'] == supplied['partial'])
                        require(ep.coverage['input_hash'] == win['input_hash'])
                if label == 'pending':
                    record = value['messages'][supplied['id']]
                    archived = win['state'] == 'cancelled' and record['window_id'] != key
                    require(record['window_id'] == key or archived)
                    require(record['sequence'] == supplied['sequence'] and record['message_id'] == supplied['message_id'])
                    require(archived or (record['core'] == 'completed') == win['core_saved'])
                    if win['state'] == 'completed':
                        require(record['summary'] in {'completed', 'not_needed'})
        require(bool(win['input']['pending']))
    return value


class MessageTasks:
    def __init__(self, memory, namespace):
        self.memory, self.namespace = memory, namespace

    @property
    def state(self):
        return (self.memory._message_state or {}).get('namespaces', {}).get(self.namespace)

    def ensure(self):
        if self.memory._message_state is None:
            self.memory._message_state = dict(version=VERSION, namespaces={})
        if self.state is None:
            self.memory._message_state['namespaces'][self.namespace] = dict(version=VERSION, namespace=self.namespace,
                generation=uuid.uuid4().hex, sequences={}, messages={}, sessions={}, windows={})
        return self.state

    def rows(self, session_id=None, ids=None):
        return {k: v for k, v in (self.state or {}).get('messages', {}).items()
                if (session_id is None or v['session_id'] == session_id) and (ids is None or k in ids)}

    def inspect(self, messages, session_id, mode='live', scope='auto'):
        rows = normalized(messages, session_id, mode, scope)
        existing = {(r['session_id'], r['message_id']): r for r in self.rows().values()}
        unseen = {}
        for row in rows:
            old = existing.get((session_id, row['message_id']))
            fingerprint = identity(row, mode, scope)
            if old and old['fingerprint'] != fingerprint:
                raise MessageConflictError()
            if old is None:
                unseen[fingerprint] = row['content']
        return rows, unseen

    def accept(self, messages, session_id, mode='live', scope='auto', timestamp=0, embeddings=None):
        rows, unseen = self.inspect(messages, session_id, mode, scope)
        from ..store._validation import validate_embedding
        if not isinstance(embeddings, dict) or any(key not in embeddings for key in unseen):
            raise ValueError('Missing prepared message embeddings')
        for key in unseen:
            validate_embedding(embeddings[key], 'message embedding', self.memory.embedder.dim)
        existing = {(r['session_id'], r['message_id']): (key, r)
                    for key, r in self.rows().items()}
        state = self.ensure()
        key = route(session_id, mode, scope)
        session = state['sessions'].setdefault(key, dict(session_id=session_id, mode=mode, scope=scope,
            last_activity=timestamp, cutoff=0, following=0, batch_id='', last_turn=0))
        output, fresh = [], []
        for row in rows:
            old = existing.get((session_id, row['message_id']))
            if old:
                output.append(old[0])
                continue
            sequence = state['sequences'].get(session_id, 0) + 1
            ep = Episode(content=row['content'], role=row['role'], event_time=row['event_time'],
                         ingested_at=timestamp, user_id=self.namespace, session_id=session_id,
                         message_id=row['message_id'], sequence=sequence, mode=mode)
            ep.consolidated = scope == 'raw'
            ep.embedding = embeddings[identity(row, mode, scope)]
            self.memory._sync_episode(ep)
            record = dict(session_id=session_id, message_id=ep.message_id, sequence=sequence, mode=mode,
                scope=scope, fingerprint=identity(row, mode, scope), source=source(ep), length=len(ep.content),
                core='not_requested' if scope == 'raw' else 'pending',
                summary='pending' if scope != 'raw' and len(ep.content) >= self.memory.config.summary_min_chars else 'not_needed',
                window_id=None, cancelled=False)
            state['sequences'][session_id] = sequence
            state['messages'][ep.id] = record
            existing[(session_id, ep.message_id)] = (ep.id, record)
            session['last_activity'] = timestamp
            output.append(ep.id)
            fresh.append(ep.id)
        return output, fresh

    def trigger(self, session, cutoff):
        if cutoff <= 0:
            return
        if not session['cutoff']:
            session.update(cutoff=cutoff, batch_id=uuid.uuid4().hex)
        elif cutoff > session['cutoff']:
            session['following'] = max(session['following'], cutoff)

    def cancel_missing(self):
        if self.state is None:
            return
        for key, row in self.rows().items():
            ep = self.memory.episodes_doc.get(key)
            if ep is None or source(ep) != row['source']:
                row['cancelled'] = True
        for key, win in list(self.state['windows'].items()):
            if any(self.state['messages'][s['id']]['cancelled'] for s in
                   (*win['input']['pending'], *win['input']['context'])):
                # Erasure must remove hidden source/result copies, including completed windows.
                for row in self.rows().values():
                    if row['window_id'] == key:
                        row['window_id'] = None
                        if row['core'] == 'completed' and row['summary'] == 'pending':
                            row['summary'] = 'failed'
                del self.state['windows'][key]

    def invalidate(self, session_id=None):
        if not self.state:
            return
        self.state['generation'] = uuid.uuid4().hex
        for win in self.state['windows'].values():
            if win['state'] not in TERMINAL:
                win.update(state='cancelled', error_code='manual_change')

    def snapshot(self, session_id=None, ids=None):
        jobs = []
        for key, row in self.rows(session_id, ids).items():
            win = self.state['windows'].get(row['window_id'])
            complete = row['core'] in {'completed', 'not_requested'} and row['summary'] in {'completed', 'not_needed'}
            state = 'cancelled' if row['cancelled'] else 'completed' if complete else (win['state'] if win else
                'failed' if row['core'] == 'failed' or row['summary'] == 'failed' else
                'completed' if row['core'] in {'completed', 'not_requested'} else 'pending')
            jobs.append(dict(episode_id=key, message_id=row['message_id'], sequence=row['sequence'],
                state=state, core=row['core'], summarization=row['summary'],
                error_code=win['error_code'] if win else None,
                attempts=win['attempts'] if win else 0, embed_attempts=win['embed_attempts'] if win else 0))
        counts = {s: sum(j['state'] == s for j in jobs) for s in
                  ('pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled')}
        return dict(session_id=session_id, generation=self.state['generation'] if self.state else None,
                    counts=counts, jobs=sorted(jobs, key=lambda j: j['sequence']))

    def force(self, session_id=None, ids=None, retry_failed=False):
        if not self.state:
            return
        selected = self.rows(session_id, ids)
        cutoffs = {}
        for row in selected.values():
            if not row['cancelled'] and row['core'] != 'not_requested':
                key = route(row['session_id'], row['mode'], row['scope'])
                cutoffs[key] = max(cutoffs.get(key, 0), row['sequence'])
        for key, cutoff in cutoffs.items():
            self.trigger(self.state['sessions'][key], cutoff)
        for key, row in selected.items():
            if row['cancelled'] or row['core'] == 'not_requested':
                continue
            win = self.state['windows'].get(row['window_id'])
            if win and retry_failed and win['state'] in {'failed', 'cancelled'}:
                changed = win['guard'] != self.state['generation'] or win['config'] != config_identity(self.memory)
                if changed and not win['core_saved']:
                    win.update(state='cancelled', error_code='explicit_reprepare')
                    for item in win['input']['pending']:
                        record = self.state['messages'][item['id']]
                        if not record['cancelled']:
                            record.update(window_id=None, core='pending')
                            if record['summary'] == 'failed':
                                record['summary'] = 'pending'
                    continue
                win.update(guard=self.state['generation'], config=config_identity(self.memory))
                win.update(state='pending', next_attempt_at=0, error_code=None)
                reset_attempts(win, 'embed_attempts', 'explicit_retry', time.time())
                if win['phase'] in {'generate', 'compensate'}:
                    reset_attempts(win, 'attempts', 'explicit_retry', time.time())
                for item in win['input']['pending']:
                    record = self.state['messages'][item['id']]
                    if record['core'] == 'failed':
                        record['core'] = 'pending'
                    if record['summary'] == 'failed':
                        record['summary'] = 'pending'

    def response(self, ids):
        jobs = {j['episode_id']: j for j in self.snapshot(ids=set(ids))['jobs']}
        return {'ok': True, 'messages': [deepcopy(jobs[key]) for key in ids]}
