"""One persisted ledger for dialogue, raw sources and document operations."""
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

VERSION = 2
TERMINAL = {'completed', 'failed', 'cancelled'}


class MessageConflictError(ExtractionError):
    code = 'message_conflict'
    status_code = 409
    message = 'This message ID already identifies different input.'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def route(session_id, mode, scope, source_kind='dialogue'):
    return json.dumps([session_id, mode, scope, source_kind], ensure_ascii=False)


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
        row['event_time'] = float(stamp) if stamp is not None else None
        fingerprint = identity(row, mode, scope)
        if row['message_id'] in seen and seen[row['message_id']] != fingerprint:
            raise MessageConflictError()
        seen[row['message_id']] = fingerprint
        rows.append(row)
    return rows


def source(ep):
    return digest([ep.user_id, ep.session_id, ep.message_id, ep.sequence, ep.role,
                   ep.event_time, ep.ingested_at, ep.mode, ep.content, ep.source_kind, ep.scope])


def config_identity(mem, *, include_embedding=True):
    # Stable across restarts; runtime provider object identity is checked separately.
    options = {k: v for k, v in asdict(mem.config).items()
               if k.startswith(('extraction_', 'joint_', 'summary_')) or k in {
                   'llm_topics', 'explicit_preference_extraction', 'preference_object_filter',
                   'preference_object_normalization', 'preference_reversal_extraction',
                   'procedural_extraction',
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
    return digest([options, mem.policy, provider, embedding] if include_embedding else [options, mem.policy, provider])


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def reset_attempts(window, field, reason, now):
    stage = 'reset_model' if field == 'attempts' else 'reset_embed'
    window['history'].append(dict(stage=stage, attempt=0, started_at=now, finished_at=now,
        outcome='reset', finish_reason=None, usage=None, error_code=reason))
    window[field] = 0


STATES = {'pending', 'running', 'retry_wait', 'completed', 'failed', 'cancelled'}
SOURCE_FIELDS = {'session_id', 'message_id', 'fingerprint', 'source', 'length', 'sequence', 'mode',
                 'scope', 'source_kind', 'core', 'summary', 'window_ids', 'cancelled', 'position', 'parts', 'summary_requested', 'commits', 'summary_receipt'}
SESSION_FIELDS = {'session_id', 'mode', 'scope', 'source_kind', 'last_activity', 'cutoff', 'following', 'batch_id', 'last_turn'}
WINDOW_FIELDS = {'route', 'batch_id', 'input', 'input_hash', 'config', 'generation_config', 'state', 'phase', 'attempts',
                 'embed_attempts', 'next_attempt_at', 'result', 'vectors', 'core_saved', 'summary_saved',
                 'error_code', 'history', 'guard', 'limits', 'result_hash', 'operation', 'stats'}


def validate_state(value, episodes=None, embedding_dim=None):
    """Validate an offline processing checkpoint and its references."""
    def require(condition):
        if not condition:
            raise ValueError('Invalid offline processing checkpoint')
    try:
        docs = {ep.id: ep for ep in episodes} if episodes is not None else None
        if value is None:
            require(docs is None or not any(ep.message_id is not None for ep in docs.values()))
            return None
        require(isinstance(value, dict) and set(value) == {'version', 'namespaces'} and value['version'] == VERSION)
        require(isinstance(value['namespaces'], dict))
        for namespace, state in value['namespaces'].items():
            require(isinstance(namespace, str) and bool(namespace))
            require(isinstance(state, dict) and set(state) == {'version', 'namespace', 'generation', 'sequences', 'sources', 'sessions', 'windows'})
            require(state['namespace'] == namespace and state['version'] == VERSION)
            require(isinstance(state['generation'], str) and bool(state['generation']))
            require(all(isinstance(state[k], dict) for k in ('sequences', 'sources', 'sessions', 'windows')))
            require(all(isinstance(k, str) and k and type(v) is int and v > 0 for k, v in state['sequences'].items()))
            identities, order = set(), set()
            for key, row in state['sources'].items():
                require(isinstance(key, str) and isinstance(row, dict) and set(row) == SOURCE_FIELDS)
                require(row['source_kind'] in {'dialogue', 'raw', 'document'})
                dialogue = row['source_kind'] == 'dialogue'
                require(isinstance(row['session_id'], str) and bool(row['session_id']))
                require(row['mode'] in {'live', 'historical'} if dialogue else row['mode'] is None)
                require(isinstance(row['message_id'], str) and bool(row['message_id']) if dialogue else row['message_id'] is None)
                require(row['scope'] in {'auto', 'long', 'raw', 'working'})
                require(row['core'] in {'not_requested', 'pending', 'completed', 'failed'})
                require(row['summary'] in {'not_needed', 'pending', 'completed', 'failed'})
                require(type(row['cancelled']) is bool and type(row['summary_requested']) is bool and type(row['sequence']) is int and row['sequence'] > 0)
                require(type(row['length']) is int and type(row['position']) is int and 0 <= row['position'] <= row['length'])
                require(all(isinstance(row[k], str) and len(row[k]) == 64 for k in ('source', 'fingerprint')))
                require(isinstance(row['window_ids'], list) and len(set(row['window_ids'])) == len(row['window_ids']))
                require(all(k in state['windows'] for k in row['window_ids']))
                require(isinstance(row['parts'], list))
                for part in row['parts']:
                    require(isinstance(part, dict) and set(part) == {'key', 'text', 'start', 'end'})
                    require(part['key'] in state['windows'] and isinstance(part['text'], str) and bool(part['text']))
                    require(type(part['start']) is int and type(part['end']) is int and 0 <= part['start'] < part['end'] <= row['length'])
                if row['core'] == 'completed':
                    require(row['position'] == row['length'] and row['length'] > 0)
                committed = []
                require(isinstance(row['commits'], list))
                seen_commits = set()
                for commit in row['commits']:
                    require(isinstance(commit, dict) and set(commit) == {'key', 'input_hash', 'ranges', 'erased_sources'})
                    require(isinstance(commit['key'], str) and len(commit['key']) == 24)
                    require(isinstance(commit['erased_sources'], list) and
                            all(isinstance(k, str) and k for k in commit['erased_sources']) and
                            len(set(commit['erased_sources'])) == len(commit['erased_sources']))
                    require(commit['key'] not in seen_commits)
                    seen_commits.add(commit['key'])
                    require(isinstance(commit['input_hash'], str) and len(commit['input_hash']) == 64 and
                            all(c in '0123456789abcdef' for c in commit['input_hash']))
                    require(isinstance(commit['ranges'], list) and bool(commit['ranges']))
                    for span in commit['ranges']:
                        require(isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span))
                        require(0 <= span[0] < span[1] <= row['length'])
                    window = state['windows'].get(commit['key'])
                    if window is None:
                        # A selected erasure removes cached input/results, not another
                        # event's already committed proof. Keep only its opaque receipt.
                        require(bool(commit['erased_sources']) and all(
                            source_id in state['sources'] and state['sources'][source_id]['cancelled']
                            for source_id in commit['erased_sources']))
                    else:
                        require(not commit['erased_sources'] and window['operation'] == 'extract' and window['core_saved'])
                        require(commit['input_hash'] == window['input_hash'])
                        view = next(v for v in window['input']['pending'] if v['id'] == key)
                        require(commit['ranges'] == view['ranges'])
                    committed.extend(commit['ranges'])
                for window_id in row['window_ids']:
                    window = state['windows'][window_id]
                    require(any(v['id'] == key for v in window['input']['pending']))
                    if window['operation'] == 'extract' and window['core_saved']:
                        require(window_id in seen_commits)
                if not dialogue:
                    position = 0
                    for start, end in sorted(committed):
                        require(start == position and start < end <= row['length'])
                        position = end
                    require(position == row['position'])
                elif row['position']:
                    require(bool(committed))
                last = 0
                for part in sorted(row['parts'], key=lambda part: part['start']):
                    require(last <= part['start'])
                    last = part['end']
                    window = state['windows'][part['key']]
                    require(window['result'] is not None and window['result']['summaries'].get(key) == part['text'])
                    require(key in window['summary_saved'])
                    view = next(v for v in window['input']['pending'] if v['id'] == key)
                    require((part['start'], part['end']) == (view['ranges'][0][0], view['ranges'][-1][1]))
                receipt = row['summary_receipt']
                if receipt is not None:
                    require(isinstance(receipt, dict) and set(receipt) == {'key', 'summary_hash', 'coverage', 'erased_sources'})
                    require(isinstance(receipt['key'], str) and len(receipt['key']) == 24)
                    require(isinstance(receipt['summary_hash'], str) and len(receipt['summary_hash']) == 64 and
                            all(c in '0123456789abcdef' for c in receipt['summary_hash']))
                    from ..store._validation import validate_coverage
                    validate_coverage(receipt['coverage'], 'pipeline.summary_receipt.coverage')
                    require(isinstance(receipt['erased_sources'], list) and all(isinstance(k, str) and k for k in receipt['erased_sources']))
                    require(len(set(receipt['erased_sources'])) == len(receipt['erased_sources']))
                    require(receipt['coverage']['source'] == row['source'] and receipt['coverage']['original_length'] == row['length'])
                    if receipt['key'] not in state['windows']:
                        require(bool(receipt['erased_sources']) and all(k in state['sources'] and state['sources'][k]['cancelled'] for k in receipt['erased_sources']))
                    else:
                        require(not receipt['erased_sources'])
                pair = (row['session_id'], row['message_id'])
                seq = (row['session_id'], row['sequence'])
                require(seq not in order and row['sequence'] <= state['sequences'].get(row['session_id'], 0))
                order.add(seq)
                if dialogue:
                    require(pair not in identities)
                    identities.add(pair)
                require(route(row['session_id'], row['mode'], row['scope'], row['source_kind']) in state['sessions'])
                if docs is not None and row['cancelled']:
                    require(key not in docs or source(docs[key]) != row['source'])
                if docs is not None and not row['cancelled']:
                    ep = docs.get(key)
                    require(ep is not None and ep.user_id == namespace and source(ep) == row['source'])
                    require(len(ep.content) == row['length'] and ep.source_kind == row['source_kind'])
                    require(row['core'] != 'completed' or ep.consolidated and ep.coverage is not None)
                    require(row['summary'] != 'completed' or bool(ep.summary))
                    if dialogue:
                        require((ep.message_id, ep.sequence, ep.mode) == (row['message_id'], row['sequence'], row['mode']))
                        require(identity({'role': ep.role, 'content': ep.content, 'event_time': ep.event_time}, row['mode'], row['scope']) == row['fingerprint'])
                    receipt = row['summary_receipt']
                    if row['summary'] == 'completed' and any(key in state['windows'][wid]['summary_saved'] for wid in row['window_ids']):
                        require(receipt is not None)
                    if receipt is not None:
                        require(ep.summary and row['summary'] == 'completed')
                        require(receipt['summary_hash'] == digest(ep.summary) and receipt['coverage'] == ep.summary_coverage)
                        require(receipt['coverage']['source'] == row['source'])
                        window = state['windows'].get(receipt['key'])
                        if window is not None:
                            require(key in window['summary_saved'] and receipt['summary_hash'] == digest(window['result']['summaries'][key]))
                            require(receipt['coverage']['input_hash'] == window['input_hash'])
                            if dialogue:
                                view = next(v for v in window['input']['pending'] if v['id'] == key)
                                require(receipt['coverage']['ranges'] == view['ranges'])
                            else:
                                require(len(row['parts']) == 1 and row['parts'][0]['text'] == ep.summary)
                                require(row['parts'][0]['start'] == 0 and row['parts'][0]['end'] == row['length'])
                    if row['position']:
                        last_commit = row['commits'][-1]
                        ranges = last_commit['ranges'] if dialogue else [[0, row['position']]]
                        partial = ranges != [[0, row['length']]]
                        policy = ('head_tail_v1' if partial else 'full_message_v1') if dialogue else 'full_source_v1'
                        require(ep.coverage == dict(unit='unicode_code_points', ranges=ranges, partial=partial,
                            policy=policy, source=row['source'], original_length=row['length'],
                            input_hash=last_commit['input_hash'], strategy_completed=row['core'] == 'completed'))
            for key, session in state['sessions'].items():
                require(isinstance(session, dict) and set(session) == SESSION_FIELDS)
                require(key == route(session['session_id'], session['mode'], session['scope'], session['source_kind']))
                require(_number(session['last_activity']) and _number(session['last_turn']))
                require(all(type(session[k]) is int and 0 <= session[k] <= state['sequences'].get(session['session_id'], 0) for k in ('cutoff', 'following')))
                require(session['following'] == 0 or session['following'] > session['cutoff'])
                require(isinstance(session['batch_id'], str))
            for key, win in state['windows'].items():
                require(isinstance(key, str) and len(key) == 24 and isinstance(win, dict) and set(win) == WINDOW_FIELDS)
                require(win['route'] in state['sessions'] and win['operation'] in {'extract', 'summary', 'conflicts'})
                require(win['state'] in STATES and win['phase'] in {'generate', 'embed', 'commit', 'compensate', 'done'})
                require(win['input_hash'] == digest(win['input']) and isinstance(win['config'], str) and len(win['config']) == 64)
                require(isinstance(win['generation_config'], str) and len(win['generation_config']) == 64
                        and all(c in '0123456789abcdef' for c in win['generation_config']))
                require(isinstance(win['guard'], str) and bool(win['guard']) and _number(win['next_attempt_at']))
                require(type(win['core_saved']) is bool and isinstance(win['summary_saved'], list) and isinstance(win['vectors'], dict))
                require(win['error_code'] is None or isinstance(win['error_code'], str))
                require(isinstance(win['stats'], dict) and all(type(n) is int and n >= 0 for n in win['stats'].values()))
                require(isinstance(win['limits'], dict) and set(win['limits']) == {'summary_min_chars', 'summary_max_chars'})
                require(all(type(n) is int and n > 0 for n in win['limits'].values()) and win['limits']['summary_max_chars'] < win['limits']['summary_min_chars'])
                counts = dict(attempts=0, embed_attempts=0)
                require(isinstance(win['history'], list))
                for attempt in win['history']:
                    require(isinstance(attempt, dict) and set(attempt) == {'stage', 'attempt', 'started_at', 'finished_at', 'outcome', 'finish_reason', 'usage', 'error_code'})
                    reset = attempt['stage'] in {'reset_model', 'reset_embed'}
                    require(attempt['stage'] in {'generate', 'embed', 'adjudicate', 'compensate', 'reset_model', 'reset_embed'})
                    field = 'embed_attempts' if attempt['stage'] in {'embed', 'adjudicate', 'reset_embed'} else 'attempts'
                    counts[field] = 0 if reset else counts[field] + 1
                    require(type(attempt['attempt']) is int and attempt['attempt'] == counts[field])
                    require(_number(attempt['started_at']) and (attempt['finished_at'] is None or _number(attempt['finished_at'])))
                    require(attempt['outcome'] in {'in_flight', 'saved', 'failed', 'discarded', 'interrupted', 'reset'} and (attempt['outcome'] == 'reset') == reset)
                    from ..llm.base import Completion
                    Completion('', attempt['finish_reason'], attempt['usage'])
                    require(attempt['error_code'] is None or isinstance(attempt['error_code'], str))
                require(all(type(win[k]) is int and 0 <= win[k] <= 3 and win[k] == n for k, n in counts.items()))
                require(win['result_hash'] == (digest(win['result']) if win['result'] is not None else None))
                require(win['phase'] not in {'embed', 'commit', 'compensate', 'done'} or isinstance(win['result'], dict))
                require(win['state'] != 'completed' or win['phase'] == 'done' and win['core_saved'])
                data = win['input']
                require(isinstance(data, dict) and set(data) == {'context', 'pending', 'mode', 'scope', 'source_kind', 'operation', 'parts', 'candidates'})
                session = state['sessions'][win['route']]
                require(all(data.get(k) == session[k] for k in ('mode', 'scope', 'source_kind')))
                require(data['operation'] == win['operation'] and data['source_kind'] in {'dialogue', 'raw', 'document'})
                require(isinstance(data['context'], list) and isinstance(data['pending'], list) and bool(data['pending']))
                require(isinstance(data['parts'], list) and all(isinstance(k, str) for k in data['parts']))
                require(len(set(data['parts'])) == len(data['parts']) and key not in data['parts'])
                require(isinstance(data['candidates'], list))
                if win['operation'] == 'summary':
                    require(len(data['pending']) == 1 and not data['context'] and not data['candidates'])
                    require(not data['parts'] or len(data['parts']) >= 2)
                    if win['state'] not in TERMINAL:
                        row = state['sources'][data['pending'][0]['id']]
                        require(set(data['parts']).issubset(p['key'] for p in row['parts']))
                else:
                    require(not data['parts'])
                if win['operation'] == 'conflicts':
                    require(bool(data['candidates']) and not data['context'])
                    for pair in data['candidates']:
                        require(isinstance(pair, dict) and set(pair) == {'older', 'newer'})
                        for fact in pair.values():
                            require(isinstance(fact, dict) and set(fact) == {'id', 'subject', 'predicate', 'object',
                                'text', 'valid_at', 'source', 'source_roles'})
                            require(all(isinstance(fact[k], str) and fact[k] for k in ('id', 'subject', 'predicate', 'object')))
                            require(isinstance(fact['text'], str) and fact['source'] in {'user', 'extracted'})
                            require(isinstance(fact['source_roles'], list) and all(r in {'user', 'assistant'} for r in fact['source_roles']))
                            require(fact['valid_at'] is None or type(fact['valid_at']) in (int, float) and math.isfinite(fact['valid_at']))
                        require(pair['older']['id'] != pair['newer']['id'])
                        require(pair['older']['subject'].lower() == pair['newer']['subject'].lower())
                else:
                    require(not data['candidates'])

                views = [*data['context'], *data['pending']]
                require(len({v['id'] for v in views}) == len(views))
                require(all(a['sequence'] < b['sequence'] for a, b in zip(views, views[1:])))
                for item in (*data['context'], *data['pending']):
                    from .planning import validate_view
                    validate_view(item)
                    require(item['id'] in state['sources'] and item['source'] == state['sources'][item['id']]['source'])
                    row = state['sources'][item['id']]
                    require(row['session_id'] == session['session_id'] and row['source_kind'] == data['source_kind'])
                    require(row['mode'] == data['mode'])
                    require(win['operation'] == 'conflicts' or row['scope'] == data['scope'])
                    require(item['message_id'] == row['message_id'] and item['sequence'] == row['sequence'] and item['source_length'] == row['length'])
                    require((item['role'] in {'user', 'assistant'}) == (data['source_kind'] == 'dialogue'))
                    if docs is not None and not state['sources'][item['id']]['cancelled']:
                        ep = docs[item['id']]
                        require(source(ep) == item['source'] and ep.event_time == item['event_time'])
                        require(item['role'] == (ep.role if ep.source_kind == 'dialogue' else None))
                        if item['representation'] == 'raw':
                            from .planning import OMITTED
                            require(item['content'] == OMITTED.join(ep.content[a:b] for a, b in item['ranges']))
                    if item in data['pending'] and win['operation'] == 'extract':
                        archived = win['state'] == 'cancelled'
                        require(archived or key in state['sources'][item['id']]['window_ids'])
                if win['result'] is not None:
                    from types import SimpleNamespace
                    from .planning import validate_result
                    validate_result(win['result'], data, SimpleNamespace(**win['limits']), normalized=True)
                    require(set(win['summary_saved']).issubset(win['result']['summaries']))
                for vector in win['vectors'].values():
                    from ..store._validation import validate_embedding
                    validate_embedding(vector, 'pipeline vector', embedding_dim or len(vector))
                if win['phase'] == 'commit':
                    from .._pipeline import texts_for
                    vectors_expected = embedding_dim is not None or bool(win['vectors'])
                    expected = {digest(text) for text in texts_for(win)} if vectors_expected else set()
                    require(set(win['vectors']) == expected)
        if docs is not None:
            require(all(ep.message_id is None or ep.user_id in value['namespaces'] and ep.id in value['namespaces'][ep.user_id]['sources'] for ep in docs.values()))
        return value
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError, StopIteration, OverflowError) as exc:
        raise ValueError('Malformed offline processing checkpoint') from exc


class PipelineTasks:
    def __init__(self, memory, namespace):
        self.memory, self.namespace = memory, namespace

    @property
    def state(self):
        return (self.memory._pipeline_state or {}).get('namespaces', {}).get(self.namespace)

    def ensure(self):
        if self.memory._pipeline_state is None:
            self.memory._pipeline_state = dict(version=VERSION, namespaces={})
        if self.state is None:
            self.memory._pipeline_state['namespaces'][self.namespace] = dict(version=VERSION, namespace=self.namespace,
                generation=uuid.uuid4().hex, sequences={}, sources={}, sessions={}, windows={})
        return self.state

    def rows(self, session_id=None, ids=None):
        return {k: r for k, r in (self.state or {}).get('sources', {}).items()
                if (session_id is None or r['session_id'] == session_id) and (ids is None or k in ids)}

    def group(self, row, timestamp=0):
        key = route(row['session_id'], row['mode'], row['scope'], row['source_kind'])
        return self.ensure()['sessions'].setdefault(key, dict(session_id=row['session_id'], mode=row['mode'], scope=row['scope'],
            source_kind=row['source_kind'], last_activity=timestamp, cutoff=0, following=0, batch_id='', last_turn=0))

    def inspect(self, messages, session_id, mode='live', scope='auto'):
        rows = normalized(messages, session_id, mode, scope)
        existing = {(r['session_id'], r['message_id']): r for r in self.rows().values() if r['source_kind'] == 'dialogue'}
        unseen = {}
        for row in rows:
            old = existing.get((session_id, row['message_id']))
            fingerprint = identity(row, mode, scope)
            if old and old['fingerprint'] != fingerprint:
                raise MessageConflictError()
            if old is None:
                unseen[fingerprint] = row['content']
        return rows, unseen

    def _record(self, ep, *, fingerprint, sequence, timestamp, requested):
        row = dict(session_id=ep.session_id, message_id=ep.message_id, fingerprint=fingerprint, source=source(ep),
            length=len(ep.content), sequence=sequence, mode=ep.mode, scope=ep.scope, source_kind=ep.source_kind,
            core='pending' if requested else 'not_requested', summary='completed' if ep.summary else
                'pending' if requested and len(ep.content) >= self.memory.config.summary_min_chars else 'not_needed',
            window_ids=[], cancelled=False, position=0, parts=[], summary_requested=False, commits=[], summary_receipt=None)
        state = self.ensure()
        state['sources'][ep.id] = row
        state['sequences'][ep.session_id] = sequence
        self.group(row, timestamp)['last_activity'] = timestamp
        return row

    def accept(self, messages, session_id, mode='live', scope='auto', timestamp=0, embeddings=None):
        rows, unseen = self.inspect(messages, session_id, mode, scope)
        from ..store._validation import validate_embedding
        if not isinstance(embeddings, dict) or any(k not in embeddings for k in unseen):
            raise ValueError('Missing prepared message embeddings')
        for key in unseen:
            if self.memory.embedder is not None:
                validate_embedding(embeddings[key], 'message embedding', self.memory.embedder.dim)
            elif embeddings[key] is not None:
                raise ValueError('Unexpected vector in lexical mode')
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
                mode=mode, source_kind='dialogue', scope=scope, consolidated=scope == 'raw')
            ep.embedding = embeddings[identity(row, mode, scope)]
            self.memory._sync_episode(ep)
            self._record(ep, fingerprint=identity(row, mode, scope), sequence=sequence, timestamp=timestamp, requested=scope != 'raw')
            existing[key] = ep.id
            output.append(ep.id)
            fresh.append(ep.id)
        return output, fresh

    def register(self, episodes, timestamp, operation='extract', *, summaries=False):
        ids = []
        for ep in episodes:
            if ep.user_id != self.namespace:
                raise ValueError('Source ownership mismatch')
            row = self.rows().get(ep.id)
            if row is None:
                seq = self.ensure()['sequences'].get(ep.session_id, 0) + 1
                row = self._record(ep, fingerprint=source(ep), sequence=seq, timestamp=timestamp, requested=False)
            if row['cancelled'] or source(ep) != row['source']:
                raise ValueError('Source changed; register a new event identity')
            if operation == 'extract' and row['core'] == 'not_requested' and not ep.consolidated:
                row['core'] = 'pending'
                if summaries and len(ep.content) >= self.memory.config.summary_min_chars and not ep.summary:
                    row['summary'] = 'pending'
            if operation == 'summary' and len(ep.content) >= self.memory.config.summary_min_chars and not ep.summary:
                if row['summary'] == 'not_needed':
                    row['summary'] = 'pending'
                row['summary_requested'] = row['core'] != 'pending'
            ids.append(ep.id)
        return ids

    def trigger(self, session, cutoff):
        if cutoff <= 0:
            return
        if not session['cutoff']:
            session.update(cutoff=cutoff, batch_id=uuid.uuid4().hex)
        elif cutoff > session['cutoff']:
            session['following'] = max(session['following'], cutoff)

    def force(self, session_id=None, ids=None, retry_failed=False, operation=None):
        selected = self.rows(session_id, ids)
        if retry_failed and operation in {None, 'summary'}:
            for row in selected.values():
                # Erasure removes the cached window and its plaintext. An explicit
                # retry can prepare missing summaries from the surviving source.
                if (not row['cancelled'] and row['summary'] == 'failed' and not any(
                        self.state['windows'][key]['state'] != 'completed'
                        and self.state['windows'][key]['operation'] in {'extract', 'summary'}
                        for key in row['window_ids'])):
                    row.update(summary='pending', summary_requested=True)
        cutoffs = {}
        for row in selected.values():
            if not row['cancelled'] and (row['core'] != 'not_requested' or row['summary'] == 'pending'):
                key = route(row['session_id'], row['mode'], row['scope'], row['source_kind'])
                cutoffs[key] = max(cutoffs.get(key, 0), row['sequence'])
        for key, cutoff in cutoffs.items():
            self.trigger(self.state['sessions'][key], cutoff)
        if retry_failed and self.state:
            for win in self.state['windows'].values():
                if operation == 'summary' and not (win['operation'] == 'summary' or win['operation'] == 'extract' and win['core_saved']):
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
                            if row['summary'] == 'failed':
                                row['summary'] = 'pending'
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
                    if row['summary'] == 'failed':
                        row['summary'] = 'pending'

    def cancel_missing(self):
        if not self.state:
            return
        for key, row in self.rows().items():
            ep = self.memory.episodes_doc.get(key)
            if ep is None or source(ep) != row['source']:
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
                if row['summary_receipt'] is not None and row['summary_receipt']['key'] == key:
                    row['summary_receipt']['erased_sources'] = sorted(erased)
                if key in row['window_ids']:
                    row['window_ids'].remove(key)
                    row['parts'] = [p for p in row['parts'] if p['key'] != key]
                    if row['summary'] == 'pending':
                        row['summary'] = 'failed'
            del self.state['windows'][key]

    def cancel_window(self, win, code):
        for attempt in win['history']:
            if attempt['outcome'] == 'in_flight':
                attempt.update(outcome='discarded', finished_at=time.time(), error_code=code)
        win.update(state='cancelled', error_code=code)
        for item in win['input']['pending']:
            row = self.rows()[item['id']]
            if win['operation'] == 'extract' and not win['core_saved']:
                row['core'] = 'failed'
            if row['summary'] == 'pending':
                row['summary'] = 'failed'

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
            wins = [self.state['windows'][k] for k in row['window_ids'] if self.state['windows'][k]['operation'] in {'extract', 'summary'}]
            active = next((w for w in reversed(wins) if w['state'] not in TERMINAL), None)
            failed = next((w for w in reversed(wins) if w['state'] in {'failed', 'cancelled'}), None)
            complete = row['core'] in {'completed', 'not_requested'} and row['summary'] in {'completed', 'not_needed'}
            win = active or failed or (wins[-1] if wins else None)
            state = 'cancelled' if row['cancelled'] else 'completed' if complete else (active['state'] if active else failed['state'] if failed and row['core'] != 'pending' else 'failed' if row['core'] == 'failed' or row['summary'] == 'failed' else 'pending')
            jobs.append(dict(episode_id=key, message_id=row['message_id'], source_kind=row['source_kind'], operation=win['operation'] if win else 'extract',
                sequence=row['sequence'], state=state, phase=win['phase'] if win else 'queued', core=row['core'],
                summarization=row['summary'], coverage=deepcopy(self.memory.episodes_doc.get(key).coverage) if self.memory.episodes_doc.get(key) else None,
                error_code=win['error_code'] if win else 'source_changed' if state == 'failed' else None, attempts=win['attempts'] if win else 0,
                embed_attempts=win['embed_attempts'] if win else 0))
        for key, win in (self.state or {}).get('windows', {}).items():
            if win['operation'] != 'conflicts':
                continue
            keys = {s['id'] for s in win['input']['pending']}
            if ids is not None and not keys.intersection(ids) or session_id is not None and not any(r['session_id'] == session_id for k, r in self.rows().items() if k in keys):
                continue
            jobs.append(dict(job_id=key, episode_id=next(iter(keys)), source_ids=sorted(keys), source_kind=win['input']['source_kind'],
                operation=win['operation'], state=win['state'], phase=win['phase'], error_code=win['error_code'],
                attempts=win['attempts'], embed_attempts=win['embed_attempts']))
        return dict(session_id=session_id, generation=self.state['generation'] if self.state else None,
                    counts={s: sum(j['state'] == s for j in jobs) for s in STATES}, jobs=jobs)

    def response(self, ids):
        jobs = {j['episode_id']: j for j in self.snapshot(ids=set(ids))['jobs'] if 'core' in j}
        return dict(ok=True, messages=[deepcopy(jobs[k]) for k in ids])

    def summary_result(self, episode_id):
        row = self.rows().get(episode_id)
        ep = self.memory.episodes_doc.get(episode_id)
        if ep is None:
            return dict(status='cancelled', error_code='source_changed')
        if ep.summary:
            return dict(status='completed', error_code=None)
        if row is None:
            return dict(status='not_needed' if len(ep.content) < self.memory.config.summary_min_chars else 'pending', error_code=None)
        wins = [self.state['windows'][k] for k in row['window_ids']]
        return dict(status=row['summary'], error_code=next((w['error_code'] for w in reversed(wins)
            if w['operation'] in {'extract', 'summary'} and w['error_code']), None) if row['summary'] in {'pending', 'failed'} else None)

    def identity_saved(self, episode_id):
        from .fact_rules import _NAME_PREDICATES
        names = set(self.memory._aliases.get(self.namespace, set()))
        if self.namespace in self.memory._identity:
            names.add(self.memory._identity[self.namespace].lower())
        return bool(names) and any(win['core_saved'] and any(
            fact['predicate'] in _NAME_PREDICATES and fact['object'].strip().lower() in names and episode_id in fact['source_ids']
            for fact in (win['result'] or {}).get('facts', []))
            for win in (self.state or {}).get('windows', {}).values())
