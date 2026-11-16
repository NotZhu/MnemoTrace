"""In-process ledger for message batches."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
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
    """计算 JSON 数据的稳定 SHA-256 指纹。

    Args:
        value: 可序列化为 JSON 的数据。

    Returns:
        十六进制指纹。对象键的插入顺序不影响结果。

    Raises:
        TypeError: 数据无法序列化为 JSON。
        ValueError: 数据包含非有限数值或循环引用。
    """
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def source_fingerprint(ep):
    """计算用于检查原文变化的指纹。

    Args:
        ep: 要检查的原文事件。

    Returns:
        原文内容及摄入字段的指纹。
    """
    return content_hash([ep.user_id, ep.session_id, ep.message_id, ep.sequence, ep.role,
                   ep.event_time, ep.ingested_at, ep.content, ep.extract])


def config_identity(mem, *, include_embedding=True, generation_only=False):
    # Stable across restarts; runtime provider object identity is checked separately.
    """计算用于判断抽取检查点能否复用的配置指纹。

    Args:
        mem: 提供模型、策略和配置的引擎或配置视图。
        include_embedding: 是否将向量编码器纳入指纹。
        generation_only: 是否只纳入与生成有关的配置。

    Returns:
        可跨进程重启比较的配置指纹。

    Raises:
        ValueError: 模型包装器存在循环，或配置无法编码。
    """
    options = {k: v for k, v in asdict(mem.config).items()
               if k.startswith('digest_') or (not generation_only and (
                   k.startswith('memory_max_') or k in {
                       'conflict_sim_threshold', 'max_hot_facts', 'salience_decay_per_day'}))}
    provider_fields = ('model', 'temperature', 'max_tokens', 'kwargs', 'model_tag')
    if not generation_only:
        provider_fields += ('context_window', 'token_counter_id')
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
                    *[setting(getattr(model, k, None)) for k in provider_fields]]
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
    """清零阶段尝试次数，并在窗口历史中记录原因。

    Args:
        window: 当前抽取窗口状态。
        field: 要重置的尝试次数字段。
        reason: 审计记录中的重置原因。
        now: 重置发生的时间戳。
    """
    stage = 'reset_model' if field == 'attempts' else 'reset_embed'
    window['history'].append(dict(stage=stage, attempt=0, started_at=now, finished_at=now,
        outcome='reset', finish_reason=None, usage=None, error_code=reason))
    window[field] = 0


class PipelineTasks:
    def __init__(self, memory, namespace):
        """绑定离线引擎及其用户任务命名空间。

        Args:
            memory: 离线记忆引擎。
            namespace: 用户命名空间。
        """
        self.memory, self.namespace = memory, namespace

    @property
    def state(self):
        """返回当前用户的任务状态。尚未初始化时返回 None。"""
        return (self.memory._pipeline_state or {}).get('namespaces', {}).get(self.namespace)

    def ensure(self):
        """初始化缺失的用户任务状态并返回它。"""
        if self.memory._pipeline_state is None:
            self.memory._pipeline_state = dict(namespaces={}, profiles={})
        if self.state is None:
            self.memory._pipeline_state['namespaces'][self.namespace] = dict(namespace=self.namespace,
                generation=uuid.uuid4().hex, sequences={}, sources={}, windows={})
        return self.state

    def rows(self, session_id=None, ids=None):
        """筛选当前用户的原文任务记录。

        Args:
            session_id: 可选的会话 ID。
            ids: 可选的原文 ID 范围。

        Returns:
            按原文 ID 索引的任务记录。
        """
        return {k: r for k, r in (self.state or {}).get('sources', {}).items()
                if (session_id is None or r['session_id'] == session_id) and (ids is None or k in ids)}

    def _record(self, ep, *, fingerprint, sequence, timestamp, requested):
        """登记原文任务并记录会话序号。

        Args:
            ep: 已保存的原文事件。
            fingerprint: 用于识别相同输入的指纹。
            sequence: 原文的会话内序号。
            timestamp: 原文摄入时间戳。
            requested: 是否立即标记为待抽取。

        Returns:
            已登记的原文任务状态。
        """
        row = dict(session_id=ep.session_id, message_id=ep.message_id, fingerprint=fingerprint, source=source_fingerprint(ep),
            length=len(ep.content), sequence=sequence, ingested_at=timestamp, extract=ep.extract,
            core='pending' if requested else 'not_requested', window_ids=[], cancelled=False)
        state = self.ensure()
        state['sources'][ep.id] = row
        state['sequences'][ep.session_id] = sequence
        return row

    def accept(self, messages, session_id, timestamp, *, check_ready=None):
        """保存一批消息，并复用原样重试的原文记录。

        Args:
            messages: 规范化的消息记录。
            session_id: 会话 ID。
            timestamp: 原文摄入时间戳。
            check_ready: 新请求写入前的可选就绪检查。

        Returns:
            按输入顺序排列的原文记录 ID。

        Raises:
            ValueError: 请求中有重复的消息 ID。
            MessageConflictError: 请求与已保存的消息冲突。
        """
        keys = [item.message_id for item in messages]
        if len(keys) != len(set(keys)):
            raise ValueError('Message IDs must be unique within a request')
        existing = {row['message_id']: (key, row) for key, row in self.rows(session_id).items()}
        known = [existing[item] for item in keys if item in existing]
        if known:
            if len(known) != len(keys) or any(
                    row.get('batch') != keys or row['fingerprint'] != item.fingerprint
                    for item, (_, row) in zip(messages, known)):
                raise MessageConflictError()
            return [key for key, _ in known]
        if check_ready is not None:
            check_ready()
        ids = []
        for item in messages:
            sequence = self.ensure()['sequences'].get(session_id, 0) + 1
            episode = Episode(content=item.content, role=item.role, event_time=item.event_time,
                              ingested_at=timestamp, user_id=self.namespace, session_id=session_id,
                              message_id=item.message_id, sequence=sequence)
            self.memory._sync_episode(episode)
            row = self._record(episode, fingerprint=item.fingerprint, sequence=sequence,
                               timestamp=timestamp, requested=True)
            row['batch'] = keys
            ids.append(episode.id)
        return ids

    def register(self, episodes, timestamp):
        """将选定的原文事件登记为抽取输入。

        Args:
            episodes: 待登记的原文事件。
            timestamp: 登记时间戳。

        Returns:
            已登记且允许抽取的原文 ID。

        Raises:
            ValueError: 原文归属不符、已取消或内容已变化。
        """
        ids = []
        for ep in episodes:
            if ep.user_id != self.namespace:
                raise ValueError('Source ownership mismatch')
            if not ep.extract:
                continue
            state = self.ensure()
            row = state['sources'].get(ep.id)
            if row is None:
                seq = state['sequences'].get(ep.session_id, 0) + 1
                ep.sequence = seq
                ep.message_id = ep.message_id or ep.id
                self.memory._sync_episode(ep)
                row = self._record(ep, fingerprint=source_fingerprint(ep), sequence=seq, timestamp=timestamp, requested=False)
            if row['cancelled'] or source_fingerprint(ep) != row['source']:
                raise ValueError('Source changed; register a new event identity')
            if row['core'] == 'not_requested' and not ep.consolidated:
                row['core'] = 'pending'
            ids.append(ep.id)
        return ids

    def force(self, session_id=None, ids=None, retry_failed=False):
        """按需重置选定范围内可重新处理的任务。

        Args:
            session_id: 可选的会话 ID。
            ids: 可选的原文 ID 范围。
            retry_failed: 是否重置失败、取消或待重试的处理状态。
        """
        selected = self.rows(session_id, ids)
        if retry_failed and self.state:
            identity = config_identity(self.memory)
            rows = self.state['sources']
            for key, win in self.state['windows'].items():
                if win['state'] not in {'failed', 'cancelled', 'retry_wait'}:
                    continue
                source_ids = [item['id'] for item in win['input']['pending']]
                pending = {source_id: rows[source_id] for source_id in source_ids
                           if not rows[source_id]['cancelled']
                           and rows[source_id]['core'] in {'pending', 'failed'}
                           and rows[source_id]['window_ids'][-1:] == [key]}
                if pending.keys().isdisjoint(selected):
                    continue
                changed = win['guard'] != self.state['generation'] or win['config'] != identity
                if changed or win['state'] == 'cancelled' or len(pending) != len(source_ids):
                    win.update(state='cancelled', error_code='explicit_reprepare')
                else:
                    win.update(state='pending', next_attempt_at=0, error_code=None)
                    reset_attempts(win, 'embed_attempts', 'explicit_retry', time.time())
                    if win['phase'] == 'generate':
                        reset_attempts(win, 'attempts', 'explicit_retry', time.time())
                for row in pending.values():
                    row['core'] = 'pending'
            for row in selected.values():
                if not row['cancelled'] and row['core'] == 'failed' and not row['window_ids']:
                    row['core'] = 'pending'

    def cancel_missing(self):
        """停止处理已删除或已变更的原文，并取消受影响的窗口。"""
        if not self.state:
            return
        for key, row in self.rows().items():
            ep = self.memory.episodes_doc.get(key)
            if ep is None or source_fingerprint(ep) != row['source']:
                row['cancelled'] = True
        rows = self.state['sources']
        windows = self.state['windows']
        for key, win in tuple(windows.items()):
            if any(rows[item['id']]['cancelled'] for item in (*win['input']['pending'], *win['input']['context'])):
                for item in win['input']['pending']:
                    row = rows[item['id']]
                    row['window_ids'].remove(key)
                    if win['state'] not in TERMINAL:
                        row['core'] = 'failed'
                del windows[key]

    def cancel_window(self, win, code):
        """取消抽取窗口，丢弃在途尝试并标记受影响的原文。

        Args:
            win: 要取消的抽取窗口。
            code: 记录到状态和执行历史的原因。
        """
        for attempt in win['history']:
            if attempt['outcome'] == 'in_flight':
                attempt.update(outcome='discarded', finished_at=time.time(), error_code=code)
        win.update(state='cancelled', error_code=code)
        rows = self.state['sources']
        for item in win['input']['pending']:
            row = rows[item['id']]
            row['core'] = 'failed'

    def invalidate(self, session_id=None):
        if not self.state:
            return
        self.state['generation'] = uuid.uuid4().hex
        for win in self.state['windows'].values():
            if win['state'] not in TERMINAL:
                self.cancel_window(win, 'manual_change')

    def snapshot(self, session_id=None, ids=None):
        """汇总原文的处理状态及对应窗口进度。

        Args:
            session_id: 可选的会话 ID。
            ids: 可选的原文 ID 范围。

        Returns:
            包含命名空间世代、状态计数和任务明细的字典。
        """
        jobs = []
        # Persistence sorts keys, so raw dict order is not stable across a restart.
        # Report dialogue/raw jobs by their session sequence for a stable contract.
        rows = sorted(self.rows(session_id, ids).items(), key=lambda item: item[1]['sequence'])
        for key, row in rows:
            wins = [self.state['windows'][k] for k in row['window_ids']]
            active = next((w for w in reversed(wins) if w['state'] not in TERMINAL), None)
            win = active or (wins[-1] if wins else None)
            if row['cancelled']:
                state = 'cancelled'
            elif row['core'] in {'completed', 'not_requested'}:
                state = 'completed'
            elif active:
                state = active['state']
            elif row['core'] == 'failed':
                state = win['state'] if win else 'failed'
            else:
                state = 'pending'
            episode = self.memory.episodes_doc.get(key)
            if episode and episode.digest:
                summary = 'completed'
            elif row['core'] in {'completed', 'not_requested'} or row['length'] < self.memory.config.digest_min_chars:
                summary = 'not_needed'
            else:
                summary = 'failed' if row['core'] == 'failed' else 'pending'
            jobs.append(dict(episode_id=key, message_id=row['message_id'], operation='extract',
                sequence=row['sequence'], state=state, phase=win['phase'] if win else 'queued', core=row['core'],
                summarization=summary, coverage=deepcopy(episode.coverage) if episode else None,
                error_code=win['error_code'] if win else row.get('error_code', 'source_changed') if state == 'failed' else None,
                attempts=win['attempts'] if win else 0, embed_attempts=win['embed_attempts'] if win else 0))
        return dict(session_id=session_id, generation=self.state['generation'] if self.state else None,
                    counts={s: sum(j['state'] == s for j in jobs) for s in STATES}, jobs=jobs)

    def response(self, ids):
        """整理同步抽取成功后的写入结果。

        Args:
            ids: 按请求顺序排列的原文 ID。

        Returns:
            完成状态、原文 ID、逐条消息状态和整合统计。
        """
        stats = self.state['sources'][ids[0]]['batch_result']
        jobs = {job['episode_id']: job for job in self.snapshot(ids=set(ids))['jobs']}
        return dict(ok=True, status='completed', source_ids=ids,
                    messages=[jobs[source_id] for source_id in ids], **stats)
