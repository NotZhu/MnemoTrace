"""Processing steps for the in-memory algorithm runner."""
from __future__ import annotations

from copy import deepcopy
import time
import uuid

from .consolidate.pipeline import PipelineTasks, TERMINAL, config_identity, digest, reset_attempts, source
from .consolidate.planning import input_size, output_size, plan, summary_plan, busy, fits_input, source_anchor
from .consolidate.operations import prepare, decode
from .extraction import ExtractionError, ExtractionTruncatedError, LLMNotConfiguredError, require_llm
from .llm.base import complete_result
from .store._validation import validate_embedding
from .types import Fact, WorkingMemory

# invalidate() 作废在途窗口（manual_change）与检测到快照更替的窗口取消（source_changed）
# 都表示并发写到达，而非模型或配置失败。
_CONFLICT_CODES = frozenset({'manual_change', 'source_changed'})


def _pending(tasks, session, after=0):
    return [r for r in tasks.rows(session['session_id']).values() if not r['cancelled']
            and (r['mode'], r['scope'], r['source_kind']) == (session['mode'], session['scope'], session['source_kind'])
            and r['sequence'] > after and not busy(tasks, r) and (r['core'] == 'pending' or r['summary'] == 'pending')]


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
            data = plan(mem, tasks, session, cutoff, after=session['cutoff']) or summary_plan(mem, tasks, session, cutoff)
            if data and (len(data['pending']) < len(rows) or any(s['partial'] for s in data['pending'])
                    or input_size(data, mem.config) >= mem.config.extraction_max_input_bytes
                    or output_size(data, mem.config) + mem.config.joint_summary_output_tokens > mem.config.joint_max_output_tokens):
                tasks.trigger(session, data['pending'][-1]['sequence'])


def _advance(tasks, session):
    rows = [r for r in tasks.rows(session['session_id']).values()
            if (r['mode'], r['scope'], r['source_kind']) == (session['mode'], session['scope'], session['source_kind'])
            and r['sequence'] <= session['cutoff'] and not r['cancelled']]
    if any(busy(tasks, r) or r['core'] == 'pending' or r['summary'] == 'pending' for r in rows):
        return
    session.update(cutoff=session['following'], following=0,
                   batch_id=uuid.uuid4().hex if session['following'] else '')


def new_window(mem, tasks, route_key, data):
    return dict(route=route_key, batch_id=tasks.state['sessions'][route_key]['batch_id'], input=data,
        input_hash=digest(data), config=config_identity(mem), generation_config=config_identity(mem, include_embedding=False),
        state='pending', phase='generate', attempts=0,
        embed_attempts=0, next_attempt_at=0, result=None, vectors={}, core_saved=False, summary_saved=[],
        error_code=None, history=[], guard=tasks.state['generation'], result_hash=None, operation=data['operation'],
        stats=mem.writer.empty_stats(), limits=dict(summary_min_chars=mem.config.summary_min_chars,
                                                   summary_max_chars=mem.config.summary_max_chars))


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
            data = summary_plan(mem, tasks, session, session['cutoff']) or plan(mem, tasks, session, session['cutoff'])
            if data is None:
                _advance(tasks, session)
                continue
            key = uuid.uuid4().hex[:24]
            win = new_window(mem, tasks, session_key, data)
            tasks.state['windows'][key] = win
            for item in data['pending']:
                tasks.state['sources'][item['id']]['window_ids'].append(key)
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
            row = tasks.state['sources'][item['id']]
            if not win['core_saved'] and win['operation'] == 'extract':
                row['core'] = 'failed'
            if row['summary'] == 'pending' and win['operation'] in {'extract', 'summary'}:
                row['summary'] = 'failed'


def _retryable(exc):
    cause = exc.__cause__ or exc
    return not (isinstance(cause, (ImportError, TypeError, LLMNotConfiguredError)) or any(
        name in type(cause).__name__.lower() for name in ('authentication', 'permission', 'badrequest', 'notfound')))


def texts_for(win):
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
        if (ep is None or source(ep) != item['source'] or ep.event_time != item['event_time']
                or len(ep.content) != item['source_length'] or ep.message_id != item['message_id']
                or (ep.role if ep.source_kind == 'dialogue' else None) != item['role']
                or tasks.rows()[ep.id]['sequence'] != item['sequence']):
            return False
        if item['representation_source'] is not None and digest(ep.summary) != item['representation_source']:
            return False
    if win['operation'] == 'summary' and win['input']['parts']:
        row = tasks.rows()[win['input']['pending'][0]['id']]
        if not set(win['input']['parts']).issubset(p['key'] for p in row['parts']):
            return False
    return True


def _parts_complete(row):
    end = 0
    for part in sorted(row['parts'], key=lambda p: p['start']):
        if part['start'] != end:
            return False
        end = part['end']
    return end == row['length']


def _merge_input(mem, namespace, win, now):
    from .consolidate.planning import fact_valid_at
    from .consolidate.topic_rules import resolve_topic
    from .consolidate.fact_rules import _NAME_PREDICATES, _SELF_REFS

    data, result = win['input'], win['result']
    views = {s['id']: s for s in (*data['context'], *data['pending'])}
    pending = {s['id']: s for s in data['pending']}
    facts, names = [], []

    def anchor(item):
        return source_anchor(item['source_ids'], pending, data['source_kind'])

    def vector(text):
        return win['vectors'][digest(text)] if mem.embedder is not None else None

    for item in sorted(result['facts'], key=lambda item: anchor(item)['sequence']):
        origin = anchor(item)
        subject_input = item.get('subject_input', item['subject']).strip().lower()
        owner_refs = {*_SELF_REFS, namespace.lower(), *mem._aliases.get(namespace, set())}
        if namespace in mem._identity:
            owner_refs.add(mem._identity[namespace].lower())
        owner_name = subject_input in owner_refs
        topic, topic_source = resolve_topic(item['predicate'], item['object'], item.get('topic') if mem.config.llm_topics else None)
        fact = Fact(subject=item['subject'], predicate=item['predicate'], object=item['object'], text=item['text'],
            display=item.get('display', item['text']), user_id=namespace, valid_at=fact_valid_at(item, origin['event_time']), created_at=now, last_access=now,
            category=item['category'], sensitive=item['sensitive'],
            provenance=item['source_ids'],
            source_roles=list(dict.fromkeys(views[k]['role'] for k in item['source_ids'] if views[k]['role'])),
            topic=topic, topic_source=topic_source, id='ft_' + digest([win['input_hash'], item])[:24], embedding=vector(item['text']))
        if not (data['source_kind'] != 'dialogue' and item['predicate'] in _NAME_PREDICATES and owner_name):
            facts.append(fact)
        if owner_name and item['predicate'] in _NAME_PREDICATES:
            names.append(item['object'])
    return facts, names


def _merge_snapshot(mem, namespace, facts):
    subjects = {fact.subject.strip().lower() for fact in facts}
    return sorted((fact for fact in mem._all_facts(include_unpublished=True)
                   if fact.user_id == namespace and fact.subject.strip().lower() in subjects),
                  key=lambda fact: fact.id)


def _commit(mem, tasks, win, now, merge=None, names=()):
    data, result = win['input'], win['result']
    pending = {s['id']: s for s in data['pending']}
    key = next(key for key, candidate in tasks.state['windows'].items() if candidate is win)
    def anchor(item):
        return source_anchor(item['source_ids'], pending, data['source_kind'])
    def vector(text):
        return win['vectors'][digest(text)] if mem.embedder is not None else None
    if not win['core_saved']:
        if merge is not None:
            changed, conflicts, stats = merge
            for fact in changed:
                mem._upsert_fact(fact)
                mem.graph.delete_relations_for_fact(fact.id)
                if fact.is_published():
                    mem.writer.graph_builder.add_fact(fact)
            for conflict in conflicts:
                mem.conflicts.setdefault(conflict.id, conflict)
            for field, count in stats.items():
                win['stats'][field] += count
            for name in names:
                mem._identity.setdefault(tasks.namespace, name)
                mem._aliases.setdefault(tasks.namespace, set()).add(name.lower())
        for item in result['working']:
            origin = anchor(item)
            working_id = 'wm_' + digest([win['input_hash'], item])[:24]
            if working_id not in mem.working_mem:
                # 自动抽取的工作记忆也要有墙钟生命周期，否则客户端不关闭会话时会永久累积。
                # TTL 由配置决定；None 保留旧行为（仅随会话清除）。显式手动写入可另带 TTL。
                ttl = mem.config.working_memory_ttl_seconds
                mem.working_mem[working_id] = WorkingMemory(id=working_id, content=item['content'], kind=item['kind'],
                    user_id=tasks.namespace, session_id=tasks.rows()[origin['id']]['session_id'],
                    event_time=origin['event_time'], created_at=now,
                    expires_at=(now + ttl) if ttl else None, embedding=vector(item['content']),
                    metadata=dict(provenance=item['source_ids'], episode_id=origin['id']))
                win['stats']['working_added'] += 1
        win['core_saved'] = True
        if win['operation'] == 'extract':
            for item in data['pending']:
                row, ep = tasks.rows()[item['id']], mem.episodes_doc.get(item['id'])
                if row['source_kind'] == 'dialogue':
                    row['position'], row['core'] = len(ep.content), 'completed'
                    ranges, partial, policy, completed = item['ranges'], item['partial'], 'head_tail_v1' if item['partial'] else 'full_message_v1', True
                else:
                    if row['position'] != item['ranges'][0][0]:
                        raise ValueError('Non-contiguous full-source commit')
                    row['position'] = item['ranges'][-1][1]
                    completed = row['position'] == len(ep.content)
                    row['core'] = 'completed' if completed else 'pending'
                    ranges, partial, policy = [[0, row['position']]], not completed, 'full_source_v1'
                row['commits'].append(dict(key=key, input_hash=win['input_hash'],
                                           ranges=item['ranges'], erased_sources=[]))
                ep.consolidated = row['core'] == 'completed'
                ep.coverage = dict(unit='unicode_code_points', ranges=ranges, partial=partial, policy=policy,
                    source=item['source'], original_length=item['source_length'], input_hash=win['input_hash'], strategy_completed=completed)
                mem._sync_episode(ep)
        if win['operation'] == 'extract':
            mem.writer._decay_sweep()
        mem._persona_cache.clear()
        mem._enforce_hot_limit()
    for item in data['pending']:
        source_id = item['id']
        if source_id not in result['summaries'] or source_id in win['summary_saved']:
            continue
        row, ep = tasks.rows()[source_id], mem.episodes_doc.get(source_id)
        text = result['summaries'][source_id]
        if data['source_kind'] == 'dialogue':
            ep.summary, ep.summary_embedding = text, vector(text)
            ep.summary_coverage = dict(unit='unicode_code_points', ranges=item['ranges'], partial=item['partial'],
                policy='head_tail_v1' if item['partial'] else 'full_message_v1', source=item['source'],
                original_length=item['source_length'], input_hash=win['input_hash'], strategy_completed=True)
            row['summary'], row['summary_requested'] = 'completed', False
        else:
            row['parts'] = [p for p in row['parts'] if p['key'] not in [key, *data['parts']]]
            row['parts'].append(dict(key=key, text=text, start=item['ranges'][0][0], end=item['ranges'][-1][1]))
            row['parts'].sort(key=lambda part: part['start'])
            if _parts_complete(row):
                if len(row['parts']) == 1:
                    ep.summary, ep.summary_embedding = text, vector(text)
                    row['summary'], row['summary_requested'] = 'completed', False
                    ep.summary_coverage = dict(unit='unicode_code_points', ranges=[[0, len(ep.content)]], partial=False,
                        policy='summary_source_v1', source=item['source'], original_length=len(ep.content),
                        input_hash=win['input_hash'], strategy_completed=True)
                else:
                    row['summary'] = 'pending'
        if row['summary'] == 'completed':
            row['summary_receipt'] = dict(key=key, summary_hash=digest(ep.summary),
                                          coverage=deepcopy(ep.summary_coverage), erased_sources=[])
        mem._sync_episode(ep)
        win['summary_saved'].append(source_id)
    wanted = [item['id'] for item in data['pending'] if win['operation'] == 'summary' or
              win['operation'] == 'extract' and item['needs_summary']]
    missing = [source_id for source_id in wanted if source_id not in win['summary_saved']]
    if missing:
        win.update(phase='compensate', vectors={})
        reset_attempts(win, 'embed_attempts', 'summary_pending', now)
        _fail(tasks, win, 'summary_invalid_response', now)
    else:
        win['stats']['windows_completed'] += 1
        win.update(phase='done', state='completed', error_code=None, next_attempt_at=0, vectors={})
    _advance(tasks, tasks.state['sessions'][win['route']])



def run_one(service, user):
    with service.processing_lease() as acquired:
        if not acquired or service._closed:
            return False
        with service.write_lock(user):
            mem = service.get(user)
            tasks = PipelineTasks(mem, user)
            if tasks.state is None:
                return False
            tasks.cancel_missing()
            selected = _select(mem, tasks, time.time())
            if selected is None:
                return False
            key, win = selected
            if win['history'] and win['history'][-1]['outcome'] == 'in_flight':
                win['history'][-1]['outcome'] = 'interrupted'
            if config_identity(mem) != win['config']:
                _fail(tasks, win, 'configuration_changed', time.time(), retryable=False)
                return True
            if tasks.state['generation'] != win['guard'] or not _current(mem, tasks, win, win['guard'], mem.llm):
                tasks.cancel_window(win, 'derived_state_changed')
                return True
            adjudicate = win['phase'] == 'commit' and win['operation'] == 'extract' and not win['core_saved']
            if win['phase'] == 'commit' and not adjudicate:
                _commit(mem, tasks, win, time.time())
                return True
            if adjudicate:
                merge_at = time.time()
                facts, names = _merge_input(mem, user, win, merge_at)
                previous = deepcopy(_merge_snapshot(mem, user, facts))
                existing_pairs = tuple(c.pair_key for c in mem.conflicts.values() if c.user_id == user)
            if win['phase'] in {'generate', 'compensate'}:
                budget_policy = mem._effective('summary_system') if win['operation'] == 'summary' else (
                    mem.policy.get('extract_system', '') + mem.policy.get('extract_instruction', ''))
                if not fits_input(win['input'], mem.config, budget_policy):
                    _fail(tasks, win, 'configuration_invalid', time.time(), retryable=False)
                    return True
            embedding = win['phase'] == 'embed'
            counter = 'embed_attempts' if embedding or adjudicate else 'attempts'
            if win[counter] >= 3:
                _fail(tasks, win, 'adjudication_failed' if adjudicate else 'embedding_failed' if embedding else 'extraction_failed',
                      time.time(), retryable=False, embedding=embedding or adjudicate)
                _advance(tasks, tasks.state['sessions'][win['route']])
                return True
            win[counter] += 1
            if not embedding and not adjudicate:
                win['stats']['model_calls'] += 1
            win['state'] = 'running'
            win['history'].append(dict(stage='adjudicate' if adjudicate else win['phase'], attempt=win[counter], started_at=time.time(),
                finished_at=None, outcome='in_flight', finish_reason=None, usage=None, error_code=None))
            config, data, phase = deepcopy(mem.config), deepcopy(win['input']), win['phase']
            model, embedder = mem.llm, mem.embedder
            policy = (mem._effective('summary_system') if win['operation'] == 'summary' else
                      mem.policy.get('extract_system', '') + mem.policy.get('extract_instruction', ''))
            texts = texts_for(win) if embedding else []
            saved_summaries = list(win['summary_saved'])
            token = tasks.state['generation']
            mem = tasks = win = None
        failure, output, completion = None, None, None
        try:
            if adjudicate:
                from .consolidate.adjudication import plan_merge
                from .llm.execution import ModelMeter, one_attempt
                output = plan_merge(facts, previous, ModelMeter(one_attempt(model), service.metrics, 'pipeline'),
                                    config, embedder, merge_at, existing_pairs)
            elif embedding and embedder is None:
                output = {}
            elif embedding:
                vectors = embedder.embed_batch(texts) if texts else []
                if len(vectors) != len(texts):
                    raise ValueError('Embedding batch length mismatch')
                for v in vectors:
                    validate_embedding(v, 'message embedding', embedder.dim)
                output = {digest(text): vector for text, vector in zip(texts, vectors)}
            else:
                from .llm.execution import ModelMeter, one_attempt
                require_llm(model)
                if phase == 'compensate':
                    data['pending'] = [item for item in data['pending'] if item['id'] not in saved_summaries]
                prompt, system, max_tokens = prepare(data, config, policy, phase == 'compensate')
                completion = complete_result(ModelMeter(one_attempt(model), service.metrics, 'pipeline'), prompt,
                    system=system, max_tokens=max_tokens)
                if completion.finish_reason not in {None, 'stop', 'end_turn'}:
                    raise ExtractionTruncatedError()
                output = decode(completion.text, data, config, phase == 'compensate')

        except Exception as exc:
            failure = exc
        if service._closed:
            return False
        with service.write_lock(user):
            mem = service.get(user)
            tasks = PipelineTasks(mem, user)
            win = tasks.state['windows'].get(key) if tasks.state else None
            if win is None or win['state'] != 'running':
                return False
            win['history'][-1].update(finished_at=time.time(),
                finish_reason=completion.finish_reason if completion is not None else None,
                usage=completion.usage if completion is not None else None,
                outcome='failed' if failure is not None else 'saved')
            if not _current(mem, tasks, win, token, model):
                tasks.cancel_window(win, 'source_changed')
                win['history'][-1]['outcome'] = 'discarded'
            elif failure is not None:
                code = 'adjudication_failed' if adjudicate else 'embedding_failed' if embedding else (
                    failure.code if isinstance(failure, ExtractionError) else 'extraction_failed')
                _fail(tasks, win, code, time.time(), retryable=_retryable(failure), embedding=embedding or adjudicate)
                win['history'][-1]['error_code'] = code
            elif adjudicate:
                if previous != _merge_snapshot(mem, user, facts):
                    win['history'][-1].update(outcome='discarded', error_code='version_conflict')
                    _fail(tasks, win, 'version_conflict', time.time(), embedding=True)
                else:
                    _commit(mem, tasks, win, merge_at, output, names)
                return True
            elif embedding:
                win['vectors'] = output
                reset_attempts(win, 'embed_attempts', 'adjudication_pending', time.time())
                win.update(phase='commit', state='pending', next_attempt_at=0)
            else:
                if phase == 'compensate':
                    for mid, text in output['summaries'].items():
                        if mid not in win['summary_saved']:
                            win['result']['summaries'][mid] = text
                else:
                    from .consolidate.fact_rules import _norm_predicate, _SELF_REFS, _NAME_PREDICATES
                    from .consolidate.topic_rules import log_topic_inputs
                    if mem.config.llm_topics:
                        log_topic_inputs(output['facts'])
                    owner_refs = {*_SELF_REFS, user.lower(), *mem._aliases.get(user, set())}
                    if user in mem._identity:
                        owner_refs.add(mem._identity[user].lower())
                    identities = [item for item in output['facts'] if item['predicate'] in _NAME_PREDICATES
                                  and item['subject'].strip().lower() in owner_refs]
                    pending_order = {v['id']: v['sequence'] for v in data['pending']}
                    identities.sort(key=lambda item: min(pending_order.get(k, 10**18) for k in item['source_ids']))
                    self_name = mem._identity.get(user, identities[0]['object'] if identities else user)
                    accepted = []
                    for item in output['facts']:
                        subject = item['subject'].strip()
                        if subject.lower() in _SELF_REFS or subject.lower() in mem._aliases.get(user, set()):
                            subject = self_name
                        canonical = data['source_kind'] != 'dialogue' and win['operation'] == 'extract'
                        candidate = Fact(subject=subject, predicate=_norm_predicate(item['predicate']), object=item['object'],
                                         text='' if canonical else item['text'])
                        if mem.writer._accepted(candidate, '\n'.join(s['content'] for s in data['pending'])):
                            accepted.append({**item, 'subject': candidate.subject, 'predicate': candidate.predicate,
                                             'object': candidate.object, 'text': candidate.text, 'display': item['text'],
                                             'subject_input': item['subject'].strip()})
                    output['facts'] = accepted
                    win['result'] = output
                win['result_hash'] = digest(win['result'])
                win.update(phase='embed' if texts_for(win) else 'commit', state='pending', next_attempt_at=0)
                reset_attempts(win, 'embed_attempts', 'generated_result', time.time())
            _advance(tasks, tasks.state['sessions'][win['route']])
        return True


def extract_partial_success(result, ids, operation='extract'):
    """抽取窗口的“部分成功”：核心事实已全部落库，仅摘要等辅助步骤失败。

    这种情形 facts 已持久化，调用方不应把它当成整批失败去重试。返回 True 表示
    “虽然 result['ok'] 为 False，但不需要抛错”。execute_sources 与 /v1/messages 的
    immediate 分支共用同一判定，避免两处语义漂移。
    """
    if result.get('ok'):
        return False
    ids = set(ids)
    if operation != 'extract':
        return False
    source_jobs = [j for j in result.get('jobs', []) if 'core' in j]
    core_complete = ({j['episode_id'] for j in source_jobs} == ids and
                     all(j['core'] in {'completed', 'not_requested'} and j['state'] != 'cancelled'
                         for j in source_jobs))
    auxiliary_failed = any('core' not in j and j['state'] in {'failed', 'cancelled'}
                           for j in result.get('jobs', []))
    return core_complete and not auxiliary_failed


def raise_for_failed_result(result, ids, operation='extract'):
    """把一次 flush 的失败结果映射成与 /v1/remember 一致的异常。

    immediate 语义要求同步返回结果，失败必须能被调用方识别，而不是悄悄返回 200。
    冲突取消与模型失败分别映射为 ExtractionConflictError 与其他 ExtractionError 子类，
    携带 stored_raw 与 episode_ids，和 execute_sources 的错误语义保持一致。
    “核心成功、仅辅助失败”的部分成功不在此抛错（见 extract_partial_success）。
    """
    from .extraction import ExtractionConflictError, ExtractionResponseError, ProcessingIncompleteError
    if extract_partial_success(result, ids, operation):
        return
    ids = set(ids)
    if _conflict_cancelled(result):
        raise ExtractionConflictError(stored_raw=bool(ids), episode_ids=sorted(ids))
    code = next((j.get('error_code') for j in result.get('jobs', []) if j.get('error_code')),
                'processing_incomplete')
    error_type = {
        'llm_not_configured': LLMNotConfiguredError,
        'extraction_invalid_response': ExtractionResponseError,
        'extraction_truncated': ExtractionTruncatedError,
        'processing_incomplete': ProcessingIncompleteError,
    }.get(code, ExtractionError)
    raise error_type(stored_raw=bool(ids), episode_ids=sorted(ids))


def flush(service, user, session_id=None, timeout=30, retry_failed=False, boundary=None, *, fail_fast=False, operation=None):
    if type(timeout) not in (int, float) or not 0 <= timeout <= 300:
        raise ValueError('timeout must be between 0 and 300 seconds')
    with service.write_lock(user):
        mem = service.get(user)
        tasks = PipelineTasks(mem, user)
        ids = set(tasks.rows(session_id, boundary))
        generation = tasks.state['generation'] if tasks.state else None
        tasks.force(session_id, ids, retry_failed, operation)
    deadline = time.monotonic() + timeout
    while True:
        with service._read_scope(user) as mem:
            tasks = PipelineTasks(mem, user)
            result = tasks.snapshot(session_id, ids)
        present = {job['episode_id'] for job in result['jobs'] if 'core' in job}
        invalidated = result['generation'] != generation or present != ids
        if operation == 'summary':
            result['jobs'] = [j for j in result['jobs'] if 'core' in j]
            for job in result['jobs']:
                if 'core' in job:
                    state = job['summarization']
                    job['state'] = 'completed' if state in {'completed', 'not_needed'} else 'failed' if state == 'failed' else 'retry_wait' if job['state'] == 'retry_wait' else 'pending'
        if operation == 'summary':
            result['counts'] = {s: sum(j['state'] == s for j in result['jobs']) for s in TERMINAL | {'pending', 'running', 'retry_wait'}}
        counts = result['counts']
        active = any(counts[s] for s in ('pending', 'running', 'retry_wait'))
        failed = invalidated or bool(counts['failed'] or counts['cancelled'])
        status = 'timed_out' if active else 'failed' if failed else 'completed'
        if not active or time.monotonic() >= deadline or fail_fast and counts['retry_wait']:
            return dict(ok=status == 'completed', status=status, **result)
        if run_one(service, user):
            continue
        time.sleep(min(.02, max(0, deadline - time.monotonic())))


def memory_adapter(memory, namespace):
    """Drive the same checkpoint machine for the standalone Python facade."""
    from contextlib import contextmanager
    from .metrics import Metrics

    class LocalExecution:
        metrics = Metrics()

        @property
        def _closed(self):
            return memory._pipeline_closed

        @contextmanager
        def processing_lease(self):
            with memory._pipeline_step_lock:
                yield True

        def get(self, user):
            return memory

        @contextmanager
        def write_lock(self, user):
            with memory._pipeline_lock:
                if memory._pipeline_closed:
                    raise RuntimeError('Memory is closed; create a new instance')
                try:
                    yield
                except BaseException:
                    # A backend can fail inside commit before save is reached.
                    # The same dirty-state rule covers both kinds of failure.
                    memory._recover_pipeline_checkpoint()
                    raise

        @contextmanager
        def _read_scope(self, user):
            with memory._pipeline_lock:
                yield memory

    return LocalExecution()


def flush_memory(memory, namespace, session_id=None, timeout=30, retry_failed=False):
    return flush(memory_adapter(memory, namespace), namespace, session_id, timeout, retry_failed)


def _conflict_cancelled(result):
    """并发写（invalidate）取消的窗口是良性冲突：源已落盘，可从新快照重做，
    与模型失败必须区分开。仅当全部失败/取消都携带冲突码——没有 retry_wait、
    embedding 失败等其他失败形态时才成立。"""
    if not result['jobs']:
        return False
    marked = [job for job in result['jobs']
              if job['state'] in {'failed', 'cancelled'} or job.get('error_code')]
    return bool(marked) and all(job.get('error_code') in _CONFLICT_CODES for job in marked)


def execute_sources(host, user, episodes, operation='extract', retry_failed=False, *, summaries=False):
    # 冲突取消只重试一次：force 的 explicit_reprepare 路径会基于当前快照重整被取消的
    # 窗口；仍冲突说明写入方持续竞争，抛出专门的冲突异常交由调用方决定重试节奏。
    for attempt in range(2):
        with host.write_lock(user):
            mem = host.get(user)
            tasks = PipelineTasks(mem, user)
            before_facts = {f.id for f in mem._all_facts()}
            before_working = set(mem.working_mem)
            before_summaries = {ep.id for ep in episodes if ep.summary}
            before = {key: dict(win['stats']) for key, win in (tasks.state or {}).get('windows', {}).items()}
            ids = set(tasks.register(episodes, time.time(), operation, summaries=summaries))
        result = flush(host, user, timeout=30, retry_failed=retry_failed or attempt > 0, boundary=ids,
                       fail_fast=True, operation=operation)
        if result['ok'] or not _conflict_cancelled(result):
            break
    if not result['ok']:
        raise_for_failed_result(result, ids, operation)
    with host._read_scope(user) as mem:
        tasks = PipelineTasks(mem, user)
        stats = mem.writer.empty_stats()
        for key, win in (tasks.state or {}).get('windows', {}).items():
            if not any(s['id'] in ids for s in win['input']['pending']):
                continue
            for name in stats:
                stats[name] += max(0, win['stats'].get(name, 0) - before.get(key, {}).get(name, 0))
        stats['facts_added'] = sum(f.id not in before_facts and bool(ids.intersection(f.provenance)) for f in mem._all_facts())
        stats['working_added'] = len(set(mem.working_mem) - before_working)
        stats['summaries'] = sum(ep.id in ids and ep.id not in before_summaries and bool(ep.summary) for ep in mem.episodes_doc.values())
        return stats


def execute_memory(memory, user, episodes, operation='extract', *, summaries=False):
    return execute_sources(memory_adapter(memory, user), user, episodes, operation,
                           retry_failed=operation == 'summary', summaries=summaries)
