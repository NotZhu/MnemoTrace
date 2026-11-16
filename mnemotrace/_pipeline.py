"""Processing steps for the in-memory algorithm runner."""
from __future__ import annotations

from copy import deepcopy
import time
import uuid

from .consolidate.pipeline import PipelineTasks, TERMINAL, config_identity, content_hash, reset_attempts, source_fingerprint
from .consolidate.planning import PREVIOUS_MESSAGES, build_batch, source_coverage
from .consolidate.operations import decode, generate
from .llm.failures import ATTEMPT_LIMIT, RETRY_DELAYS, exceeds_model_budget, retryable as model_retryable
from .extraction import ExtractionBudgetError, ExtractionConfigurationError, ExtractionError, ExtractionTruncatedError, LLMNotConfiguredError, require_llm
from .llm.execution import one_attempt
from .store._validation import validate_embedding
from .consolidate.facts import extraction_entries, materialize_facts

# invalidate() 作废在途窗口（manual_change）与检测到快照更替的窗口取消（source_changed）
# 都表示并发写到达，而非模型或配置失败。
_CONFLICT_CODES = frozenset({'manual_change', 'source_changed'})


def new_window(mem, data):
    """为一批抽取输入建立初始检查点。

    Args:
        mem: 离线记忆引擎。
        data: 批次抽取输入。

    Returns:
        处于待生成阶段的窗口状态。
    """
    return dict(input=data,
        input_hash=content_hash(data), config=config_identity(mem), generation_config=config_identity(mem, include_embedding=False),
        state='pending', phase='generate', attempts=0,
        embed_attempts=0, next_attempt_at=0, result=None, vectors={},
        error_code=None, history=[], guard=None,
        stats=mem.writer.empty_stats())


def _select(mem, tasks, now, ids=None):
    """选择可执行的抽取窗口，或为待处理消息创建窗口。

    Args:
        mem: 离线记忆引擎。
        tasks: 当前用户的任务记录。
        now: 用于判断重试是否到期的时间戳。
        ids: 可选的原文 ID 筛选条件。

    Returns:
        窗口 ID 和状态，无可运行窗口时返回 None。
    """
    sources = tasks.rows(ids=ids)
    windows = tasks.state['windows']
    for key, window in windows.items():
        if (window['state'] not in TERMINAL and window['next_attempt_at'] <= now
                and all(item['id'] in sources for item in window['input']['pending'])):
            return key, window
    pending = [(key, row) for key, row in sources.items() if row['core'] == 'pending'
               and not row['cancelled'] and not any(windows[w]['state'] not in TERMINAL for w in row['window_ids'])]
    if not pending:
        return None
    _, first = pending[0]
    batch = sorted(((key, row) for key, row in pending if row['session_id'] == first['session_id']
                    and row.get('batch') == first.get('batch')), key=lambda item: item[1]['sequence'])
    if mem.llm is None:
        for _, row in batch:
            row.update(core='failed', error_code='llm_not_configured')
        return None
    previous = sorted(((key, row) for key, row in tasks.rows(first['session_id']).items()
                       if row['sequence'] < batch[0][1]['sequence'] and row['extract'] and not row['cancelled']),
                      key=lambda item: item[1]['sequence'])[-PREVIOUS_MESSAGES:]

    def episodes(rows):
        return ((mem.episodes_doc.get(key), row['sequence']) for key, row in rows)

    data = build_batch(episodes(batch), episodes(previous), mem.config)
    key = uuid.uuid4().hex[:24]
    win = new_window(mem, data)
    win['guard'] = tasks.state['generation']
    windows[key] = win
    for item in data['pending']:
        tasks.state['sources'][item['id']]['window_ids'].append(key)
    return key, win


def _fail(tasks, win, code, now, *, retryable=True, embedding=False):
    """将当前窗口标记为待重试或失败。

    Args:
        tasks: 当前用户的任务记录。
        win: 执行失败的窗口。
        code: 失败代码。
        now: 计算重试时间的基准时间戳。
        retryable: 是否允许重试。
        embedding: 是否使用向量或提交阶段的尝试计数。
    """
    attempts = win['embed_attempts'] if embedding else win['attempts']
    retry = retryable and attempts < ATTEMPT_LIMIT
    win.update(state='retry_wait' if retry else 'failed', error_code=code,
               next_attempt_at=now + RETRY_DELAYS[attempts - 1] if retry else 0)
    if not retry:
        for item in win['input']['pending']:
            tasks.state['sources'][item['id']]['core'] = 'failed'


def texts_for(win):
    """列出窗口中需要编码的去重文本。

    Args:
        win: 含已验证抽取结果的窗口。

    Returns:
        事实文本和摘要文本。
    """
    result = win['result']
    return list(dict.fromkeys([*(f['text'] for f in result['facts']), *result['digests'].values()]))


def _current(mem, tasks, win, token, model):
    """判断窗口依赖的配置、模型和原文是否仍有效。

    Args:
        mem: 离线记忆引擎。
        tasks: 当前用户的任务记录。
        win: 待检查的窗口。
        token: 执行开始时的命名空间世代。
        model: 执行使用的抽取模型。

    Returns:
        所有依赖均与开始执行时一致时返回 True。
    """
    if (tasks.state['generation'] != token or config_identity(mem) != win['config']
            or mem.llm is not model):
        return False
    rows = tasks.state['sources']
    for item in (*win['input']['pending'], *win['input']['context']):
        ep = mem.episodes_doc.get(item['id'])
        if (ep is None or source_fingerprint(ep) != item['source'] or ep.event_time != item['event_time']
                or len(ep.content) != item['source_length'] or ep.message_id != item['message_id']
                or ep.role != item['role']
                or rows[ep.id]['sequence'] != item['sequence']):
            return False
        if item['representation_source'] is not None and content_hash(ep.digest) != item['representation_source']:
            return False
    return True


def _merge_input(mem, namespace, win, now):
    """用窗口中保存的抽取结果构造待合并事实。

    Args:
        mem: 离线记忆引擎。
        namespace: 事实所属的用户命名空间。
        win: 含抽取结果和向量的窗口。
        now: 事实记录时间戳。

    Returns:
        待合并的事实列表。
    """
    data = win['input']
    entries = extraction_entries(data, win['result'], win['input_hash'])
    facts = materialize_facts(namespace, data, now, entries)
    if mem.embedder is not None:
        for fact in facts:
            fact.embedding = win['vectors'][content_hash(fact.text)]
    return facts


def _merge_snapshot(mem, namespace, facts):
    """读取与新事实主体相关的现有事实。

    Args:
        mem: 离线记忆引擎。
        namespace: 事实所属的用户命名空间。
        facts: 待合并的事实。

    Returns:
        按 ID 排序的现有事实。
    """
    subjects = {fact.subject.strip().lower() for fact in facts}
    return sorted((fact for fact in mem._all_facts()
                   if fact.user_id == namespace and fact.subject.strip().lower() in subjects),
                  key=lambda fact: fact.id)


def _commit(mem, tasks, win, now, merge):
    """应用事实合并结果与摘要，并将窗口标记为完成。

    Args:
        mem: 离线记忆引擎。
        tasks: 当前用户的任务记录。
        win: 待提交的窗口。
        now: 摘要写入时间戳。
        merge: 变更事实与合并统计。
    """
    changed, stats = merge
    for fact in changed:
        mem._upsert_fact(fact)
        mem.writer.graph_builder.add_fact(fact)
    if changed:
        mem.graph.prune_orphan_entities()
    for field, count in stats.items():
        win['stats'][field] += count
    digests = win['result']['digests']
    for item in win['input']['pending']:
        ep = mem.episodes_doc.get(item['id'])
        ep.consolidated = True
        ep.coverage = source_coverage(item, win['input_hash'])
        if ep.id in digests:
            ep.digest = digests[ep.id]
            if mem.embedder is not None:
                ep.digest_embedding = win['vectors'][content_hash(ep.digest)]
            ep.digest_coverage = source_coverage(item, win['input_hash'])
            ep.digest_written_at = now
        mem._sync_episode(ep)
        tasks.state['sources'][ep.id]['core'] = 'completed'
    mem.writer._decay_sweep()
    mem._enforce_hot_limit()
    win['stats']['windows_completed'] += 1
    first = tasks.state['sources'][win['input']['pending'][0]['id']]
    if 'batch' in first:
        first['batch_result'] = dict(win['stats'], digests=len(digests))
    win.update(phase='done', state='completed', error_code=None, next_attempt_at=0, vectors={})



def run_next_stage(mem, user, ids=None):
    """推进一个抽取窗口的生成、向量或提交阶段。

    Args:
        mem: 离线记忆引擎。
        user: 用户命名空间。
        ids: 可选的原文 ID 筛选条件。

    Returns:
        此次调用推进或更新了窗口状态时返回 True。
    """
    with mem._pipeline_step_lock:
        if mem._pipeline_closed:
            return False
        with mem._pipeline_write():
            tasks = PipelineTasks(mem, user)
            if tasks.state is None:
                return False
            tasks.cancel_missing()
            selected = _select(mem, tasks, time.time(), ids)
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
            adjudicate = win['phase'] == 'commit'
            if adjudicate:
                merge_at = time.time()
                facts = _merge_input(mem, user, win, merge_at)
                previous = deepcopy(_merge_snapshot(mem, user, facts))
            policy = mem.policy['extract_system'] + mem.policy['extract_instruction']
            embedding = win['phase'] == 'embed'
            counter = 'embed_attempts' if embedding or adjudicate else 'attempts'
            if win[counter] >= ATTEMPT_LIMIT:
                _fail(tasks, win, 'adjudication_failed' if adjudicate else 'embedding_failed' if embedding else 'extraction_failed',
                      time.time(), retryable=False, embedding=embedding or adjudicate)

                return True
            win[counter] += 1
            if not embedding and not adjudicate:
                win['stats']['model_calls'] += 1
            win['state'] = 'running'
            win['history'].append(dict(stage='adjudicate' if adjudicate else win['phase'], attempt=win[counter], started_at=time.time(),
                finished_at=None, outcome='in_flight', finish_reason=None, usage=None, error_code=None))
            config, data = deepcopy(mem.config), deepcopy(win['input'])
            model, embedder = mem.llm, mem.embedder
            texts = texts_for(win) if embedding else []
            token = tasks.state['generation']
        failure, output, completion = None, None, None
        try:
            if adjudicate:
                from .consolidate.adjudication import plan_merge
                output = plan_merge(facts, previous, one_attempt(model), config, embedder, merge_at)
            elif embedding and embedder is None:
                output = {}
            elif embedding:
                vectors = embedder.embed_batch(texts) if texts else []
                if len(vectors) != len(texts):
                    raise ValueError('Embedding batch length mismatch')
                for v in vectors:
                    validate_embedding(v, 'message embedding', embedder.dim)
                output = {content_hash(text): vector for text, vector in zip(texts, vectors)}
            else:
                require_llm(model)
                with generate(model, data, config, policy) as completion:
                    output = decode(completion, data, config)

        except Exception as exc:
            failure = exc
        if mem._pipeline_closed:
            return False
        with mem._pipeline_write():
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
                if exceeds_model_budget(failure):
                    code = ExtractionBudgetError.code
                else:
                    code = 'adjudication_failed' if adjudicate else 'embedding_failed' if embedding else (
                        failure.code if isinstance(failure, ExtractionError) else 'extraction_failed')
                _fail(tasks, win, code, time.time(), retryable=model_retryable(failure), embedding=embedding or adjudicate)
                win['history'][-1]['error_code'] = code
            elif adjudicate:
                if previous != _merge_snapshot(mem, user, facts):
                    win['history'][-1].update(outcome='discarded', error_code='version_conflict')
                    _fail(tasks, win, 'version_conflict', time.time(), embedding=True)
                else:
                    _commit(mem, tasks, win, merge_at, output)
                return True
            elif embedding:
                win['vectors'] = output
                reset_attempts(win, 'embed_attempts', 'adjudication_pending', time.time())
                win.update(phase='commit', state='pending', next_attempt_at=0)
            else:
                win['result'] = output
                win['result_hash'] = content_hash(win['result'])
                win.update(phase='embed' if texts_for(win) else 'commit', state='pending', next_attempt_at=0)
                reset_attempts(win, 'embed_attempts', 'generated_result', time.time())

        return True


def raise_for_failed_result(result, ids):
    """将处理失败转换为带有原文 ID 的抽取异常。

    Args:
        result: 失败时的处理状态。
        ids: 已保存的原文 ID。

    Raises:
        ExtractionError: 具体类型由任务错误原因决定。
    """
    from .extraction import ExtractionConflictError, ExtractionResponseError, ProcessingIncompleteError
    ids = set(ids)
    if _conflict_cancelled(result):
        raise ExtractionConflictError(stored_raw=bool(ids), episode_ids=sorted(ids))
    code = next((j.get('error_code') for j in result.get('jobs', []) if j.get('error_code')),
                'processing_incomplete')
    error_type = {
        'llm_not_configured': LLMNotConfiguredError,
        'configuration_invalid': ExtractionConfigurationError,
        ExtractionBudgetError.code: ExtractionBudgetError,
        'extraction_invalid_response': ExtractionResponseError,
        'extraction_truncated': ExtractionTruncatedError,
        'processing_incomplete': ProcessingIncompleteError,
    }.get(code, ExtractionError)
    raise error_type(stored_raw=bool(ids), episode_ids=sorted(ids))


def flush(mem, user, session_id=None, timeout=30, retry_failed=False, boundary=None, *, fail_fast=False):
    """处理指定范围内的任务，直到结束或等待超时。

    Args:
        mem: 离线记忆引擎。
        user: 用户命名空间。
        session_id: 可选的会话 ID。
        timeout: 最长等待时间，单位为秒。
        retry_failed: 是否重新安排失败任务。
        boundary: 可选的原文 ID 范围。
        fail_fast: 是否在阶段进入重试等待时提前返回。

    Returns:
        包含处理状态和任务快照的字典。

    Raises:
        ValueError: 等待时间不在 0 到 300 秒内。
    """
    if type(timeout) not in (int, float) or not 0 <= timeout <= 300:
        raise ValueError('timeout must be between 0 and 300 seconds')
    tasks = PipelineTasks(mem, user)
    with mem._pipeline_write():
        ids = set(tasks.rows(session_id, boundary))
        generation = tasks.state['generation'] if tasks.state else None
        tasks.force(session_id, ids, retry_failed)
    deadline = time.monotonic() + timeout
    while True:
        with mem._pipeline_lock:
            result = tasks.snapshot(session_id, ids)
        present = {job['episode_id'] for job in result['jobs'] if 'core' in job}
        invalidated = result['generation'] != generation or present != ids
        counts = result['counts']
        active = any(counts[s] for s in ('pending', 'running', 'retry_wait'))
        failed = (invalidated or mem._pipeline_closed or mem._pipeline_broken
                  or bool(counts['failed'] or counts['cancelled']))
        status = 'failed' if failed else 'timed_out' if active else 'completed'
        if failed or not active or time.monotonic() >= deadline:
            return dict(ok=status == 'completed', status=status, **result)
        if run_next_stage(mem, user, ids):
            continue
        if fail_fast and counts['retry_wait']:
            return dict(ok=False, status=status, **result)
        time.sleep(min(.02, max(0, deadline - time.monotonic())))


def _conflict_cancelled(result):
    """判断已标记的失败或取消是否都由并发变更引起。

    Args:
        result: 包含任务状态的处理结果。

    Returns:
        存在冲突且无其他已标记错误时返回 True。
    """
    if not result['jobs']:
        return False
    marked = [job for job in result['jobs']
              if job['state'] in {'failed', 'cancelled'} or job.get('error_code')]
    return bool(marked) and all(job.get('error_code') in _CONFLICT_CODES for job in marked)


def execute_memory(mem, user, episodes):
    # 冲突取消只重试一次：force 的 explicit_reprepare 路径会基于当前快照重整被取消的
    # 窗口；仍冲突说明写入方持续竞争，抛出专门的冲突异常交由调用方决定重试节奏。
    """同步抽取指定原文，并在并发冲突时重试一次。

    Args:
        mem: 离线记忆引擎。
        user: 用户命名空间。
        episodes: 要处理的原文事件。

    Returns:
        本次执行的整合统计。

    Raises:
        ExtractionError: 抽取未完成，或重试后仍存在冲突。
    """
    tasks = PipelineTasks(mem, user)
    for attempt in range(2):
        with mem._pipeline_write():
            before_facts = {f.id for f in mem._all_facts()}
            before_digests = {ep.id for ep in episodes if ep.digest}
            before = {key: dict(win['stats']) for key, win in (tasks.state or {}).get('windows', {}).items()}
            ids = set(tasks.register(episodes, time.time()))
        result = flush(mem, user, timeout=30, retry_failed=attempt > 0, boundary=ids,
                       fail_fast=True)
        if result['ok'] or not _conflict_cancelled(result):
            break
    if not result['ok']:
        raise_for_failed_result(result, ids)
    with mem._pipeline_lock:
        stats = mem.writer.empty_stats()
        for key, win in (tasks.state or {}).get('windows', {}).items():
            if not any(s['id'] in ids for s in win['input']['pending']):
                continue
            for name in stats:
                stats[name] += max(0, win['stats'].get(name, 0) - before.get(key, {}).get(name, 0))
        stats['facts_added'] = sum(f.id not in before_facts and bool(ids.intersection(f.provenance)) for f in mem._all_facts())
        stats['digests'] = sum(ep.id in ids and ep.id not in before_digests and bool(ep.digest) for ep in mem.episodes_doc.values())
        return stats
