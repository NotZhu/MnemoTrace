"""任务管理。"""
from dataclasses import asdict, replace
from datetime import timedelta
import json
from uuid import uuid4

from ...consolidate.pipeline import content_hash, source_fingerprint
from ..contracts import EXTRACTION_POLICY_FIELDS, CommitConditions, ExtractionProduct, Lease
from ..contracts import OperationKind, OperationResult
from ..contracts import OperationSpec, ProcessingPhase, ProcessingState, SourceVersion
from .dao import Applied, _apply_changes, _space_row, new_operation_id
from .errors import DataCorruption, MessageConflict, VersionConflict
from .records import SCOPE, SOURCE, domain
from .writes import enqueue, store_messages
from ...llm.failures import ATTEMPT_LIMIT, RETRY_DELAYS

LEASE_SECONDS = 60
RENEW_SECONDS = 20
ACTIVE = ('pending', 'retry_wait', 'running')
MODEL_OPERATIONS = ('extract', 'profile')
RUNNABLE = MODEL_OPERATIONS


def _attempt_counter(row):
    return 'embed_attempts' if row['phase'] in ('embed', 'commit') else 'generation_attempts'


def _result(row):
    """将任务状态行转换为对外的操作快照。

    Args:
        row: 数据库存储的任务状态行。

    Returns:
        包含阶段、错误及重试时间的 OperationResult。
    """
    return OperationResult(
        row['operation_id'], OperationKind(row['kind']), ProcessingState(row['state']),
        ProcessingPhase(row['phase']), row['extraction_saved'],
        row['core_committed'], row['attempts'],
        row['next_attempt_at'].timestamp() if row['next_attempt_at'] else None,
        row['error_code'],
    )


def _operation(sql, operation_id, *, lock=False):
    rows = sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} AND operation_id = %s'
                    + (' FOR UPDATE SKIP LOCKED' if lock else ''), (*sql.scope, operation_id))
    return rows[0] if rows else None


def _sources(sql, operation_id, *, context=False):
    """读取任务按输入顺序记录的原文版本。

    Args:
        sql: 已限定空间的事务。
        operation_id: 任务操作 ID。
        context: 是否同时返回为抽取冻结的历史上下文引用。

    Returns:
        原文版本引用。
    """
    rows = sql.run(f'SELECT source_id,content_version '
                   f'FROM mnemotrace.operation_sources WHERE {SCOPE} AND operation_id = %s '
                   'AND (NOT is_context OR %s) ORDER BY input_order,source_id',
                   (*sql.scope, operation_id, context)).fetchall()
    return tuple(SourceVersion(source_id, version) for source_id, version in rows)


def _spec(operation_id, kind, session_id, records):
    return OperationSpec(operation_id, kind, session_id, tuple(
        SourceVersion(record.ref.record_id, record.content_version) for record in records))


def _accept_batch(sql, session_id, messages, operation_id, generation, check_ready):
    """在当前事务中保存原文并按需创建抽取任务。

    Args:
        sql: 已限定记忆空间的事务。
        session_id: 会话 ID。
        messages: 规范化的消息记录。
        operation_id: 抽取操作 ID。为 None 时仅保存原文。
        generation: 当前空间世代。
        check_ready: 写入前的可选就绪检查。

    Returns:
        原文记录 ID 和新增原文数量。
    """
    ids, count = store_messages(sql, session_id, messages, extract=operation_id is not None,
                                check_ready=check_ready)
    if operation_id is not None:
        spec = OperationSpec(operation_id, OperationKind.EXTRACT, session_id,
                             tuple(SourceVersion(source_id, 1) for source_id in ids))
        enqueue(sql, spec, generation)
    return ids, count


class Tasks:
    """管理单个记忆空间内持久化任务的写入和执行状态。"""

    def __init__(self, dao):
        """绑定任务存储使用的记忆空间。

        Args:
            dao: 已绑定目标空间的数据访问对象。
        """
        self.dao = dao

    def operation(self, operation_id):
        """读取指定操作的任务快照。

        Args:
            operation_id: 任务操作 ID。

        Returns:
            任务快照。操作不存在时返回 None。
        """
        return self.operations((operation_id,))[0]

    def operations(self, operation_ids):
        """批量查询任务快照并保留请求顺序。

        Args:
            operation_ids: 要查询的操作 ID。

        Returns:
            任务快照序列。不存在的操作对应 None。
        """
        if not operation_ids:
            return ()
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} '
                            'AND operation_id = ANY(%s)', (*sql.scope, list(operation_ids)))
            by_id = {row['operation_id']: _result(row) for row in rows}
            return tuple(by_id.get(operation_id) for operation_id in operation_ids)

    def list_operations(self, session_id, after_id, limit):
        """分页读取当前空间的记忆任务。

        Args:
            session_id: 可选的会话 ID。
            after_id: 可选的上一页最后一个操作 ID。
            limit: 每页任务数量上限。

        Returns:
            按操作 ID 排序的任务快照。
        """
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} '
                            'AND (%s::text IS NULL OR session_id = %s) '
                            'AND (%s::text IS NULL OR operation_id > %s) '
                            'ORDER BY operation_id LIMIT %s',
                            (*sql.scope, session_id, session_id, after_id, after_id, limit))
            return tuple(map(_result, rows))

    def accept(self, session_id, messages, check_ready):
        """在同一事务内保存消息及其抽取任务，支持原样重试。

        Args:
            session_id: 会话 ID。
            messages: 规范化的消息记录。
            check_ready: 新请求写入前的就绪检查。

        Returns:
            按输入顺序排列的原文记录 ID 和操作 ID。

        Raises:
            ValueError: 请求中有重复的消息 ID。
            MessageConflict: 请求与已保存的消息冲突。
        """
        message_ids = [message.message_id for message in messages]
        if len(set(message_ids)) != len(message_ids):
            raise ValueError('Message IDs must be unique within a request')
        operation_id = 'op_' + content_hash([session_id, [(m.message_id, m.fingerprint) for m in messages]])

        def apply(sql, revision, generation, settings):
            ids, inserted = _accept_batch(sql, session_id, messages, operation_id, generation, check_ready)
            return Applied(True, source_ids=ids, count=inserted)

        receipt = self.dao._mutate('accept_' + operation_id, apply)
        return receipt.source_ids, operation_id

    def store_originals(self, session_id, messages):
        """在同一事务中归档整批原始消息。

        Args:
            session_id: 会话 ID。
            messages: 规范化的消息记录。

        Returns:
            按输入顺序排列的原文记录 ID。
        """
        def apply(sql, revision, generation, settings):
            ids, inserted = store_messages(sql, session_id, messages, extract=False)
            return Applied(bool(inserted), source_ids=ids, count=inserted)

        return self.dao._mutate(new_operation_id(), apply).source_ids

    def chat_replay(self, receipt_id, fingerprint):
        """查找同一聊天请求已保存的回复。

        Args:
            receipt_id: 聊天请求的收据 ID。
            fingerprint: 当前请求的内容指纹。

        Returns:
            已保存的回复和当前空间世代。无收据时回复为 None。

        Raises:
            MessageConflict: 已保存请求的指纹不同。
            DataCorruption: 保存的聊天收据无效。
        """
        with self.dao._transaction(read_only=True) as sql:
            generation = _space_row(sql)[1]
            row = sql.run(f'SELECT chat_replay FROM mnemotrace.commit_receipts WHERE {SCOPE} AND operation_id = %s',
                          (*sql.scope, receipt_id)).fetchone()
        if row is None:
            return None, generation
        saved = row[0]
        if not isinstance(saved, dict) or set(saved) != {'fingerprint', 'response'}:
            raise DataCorruption('Invalid chat replay receipt')
        if saved['fingerprint'] != fingerprint:
            raise MessageConflict('Chat message identity was already used for a different request')
        return saved['response'], generation

    def accept_chat(self, receipt_id, fingerprint, generation, session_id, messages, response, *, check_ready):
        """原子保存聊天回复、原文和可选抽取任务。

        Args:
            receipt_id: 聊天请求的收据 ID。
            fingerprint: 请求内容指纹。
            generation: 生成回复时的空间世代。
            session_id: 会话 ID。
            messages: 要保存的消息记录。
            response: 生成的聊天回复。
            check_ready: 写入前的就绪检查。为 None 时仅归档原文。

        Returns:
            带记忆写入状态的聊天回复。

        Raises:
            VersionConflict: 生成回复期间空间变化，或提交后原文已被移除。
        """
        operation_id = 'op_' + receipt_id.removeprefix('chat_') if check_ready is not None else None

        def apply(sql, revision, current_generation, settings):
            if generation != current_generation:
                raise VersionConflict('Space changed while generating the chat response')
            ids, count = _accept_batch(sql, session_id, messages, operation_id, generation, check_ready)
            task = _result(_operation(sql, operation_id)).to_dict() if operation_id is not None else None
            metadata = dict(response['mnemotrace'], remembered=True, remember_queued=task is not None,
                            remember_extract=task is not None, source_ids=list(ids), task=task)
            saved = dict(fingerprint=fingerprint, response=dict(response, mnemotrace=metadata))
            return Applied(bool(count), source_ids=ids, count=count, chat_replay=saved)

        self.dao._mutate(receipt_id, apply)
        response, _ = self.chat_replay(receipt_id, fingerprint)
        if response is None:
            raise VersionConflict('Chat sources were removed after acceptance')
        return response

    def input_ids(self, operation_id):
        """读取任务本批次输入的原文 ID。

        Args:
            operation_id: 任务操作 ID。

        Returns:
            按输入顺序排列的原文 ID。
        """
        with self.dao._transaction(read_only=True) as sql:
            return tuple(source.source_id for source in _sources(sql, operation_id))

    def source_positions(self, ids):
        """读取原文记录在各自会话内的序号。

        Args:
            ids: 原文记录 ID 集合。

        Returns:
            已找到的原文记录 ID 到会话序号的映射。
        """
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.run(f'SELECT id, sequence FROM mnemotrace.sources '
                           f'WHERE {SCOPE} AND id = ANY(%s)', (*sql.scope, list(ids))).fetchall()
            return dict(rows)

    def claim(self, operation_id) -> tuple[Lease, OperationKind] | None:
        """为符合执行条件的任务签发租约。

        Args:
            operation_id: 待领取的操作 ID。

        Returns:
            租约与操作类型。任务不可领取时返回 None。
        """
        with self.dao._transaction() as sql:
            row = _operation(sql, operation_id)
            if not row or row['kind'] not in RUNNABLE or row['state'] not in ACTIVE:
                return None
            # The runtime transaction has the space row locked, including when
            # multiple workers discovered the same candidate.
            kind = OperationKind(row['kind'])
            now = sql.run('SELECT clock_timestamp()').fetchone()[0]
            state = row['state']
            if not self._current(sql, row):
                return None
            due = (row['lease_until'] if state == 'running' else row['next_attempt_at']
                   if state == 'retry_wait' else row['ready_at'])
            if due is None or due > now:
                return None
            if kind is OperationKind.EXTRACT:
                active = sql.run(f"""SELECT 1 FROM mnemotrace.operations WHERE {SCOPE}
                    AND kind = 'extract' AND state = 'running' AND lease_until > %s""",
                    (*sql.scope, now)).fetchone()
                if active:
                    return None
                head = sql.run(f"""SELECT operation_id FROM mnemotrace.operations WHERE {SCOPE}
                    AND kind = 'extract' AND (
                        (state = 'pending' AND ready_at <= %s) OR
                        (state = 'retry_wait' AND next_attempt_at <= %s) OR
                        (state = 'running' AND lease_until <= %s))
                    ORDER BY sequence LIMIT 1""", (*sql.scope, now, now, now)).fetchone()
                if head is None or head[0] != operation_id:
                    return None
                if state != 'running':
                    # An earlier task may become ready during a later task's expired lease.
                    sql.update('operations',
                               dict(state='pending', lease_token=None, lease_until=None, ready_at=now),
                               "kind = 'extract' AND state = 'running' AND lease_until <= %s", (now,))
            phase = 'generate' if row['phase'] == 'queued' else row['phase']
            counter = _attempt_counter({**row, 'phase': phase})
            if row[counter] >= ATTEMPT_LIMIT:
                self._release(sql, row, 'failed', 'attempts_exhausted')
                return None
            token = uuid4().hex
            sql.update('operations', dict(state='running', lease_token=token,
                       lease_until=now + timedelta(seconds=LEASE_SECONDS), next_attempt_at=None,
                       phase=phase,
                       attempts=row['attempts'] + 1, **{counter: row[counter] + 1}),
                       'operation_id = %s', (operation_id,))
            return Lease(operation_id, token), kind

    def _current(self, sql, row):
        return row['space_generation'] == _space_row(sql)[1]

    def _leased(self, sql, lease):
        """验证租约仍有效并取得数据库当前时间。

        Args:
            sql: 已限定记忆空间的事务。
            lease: 待验证的任务租约。

        Returns:
            任务行及数据库当前时间。

        Raises:
            VersionConflict: 租约已过期、失效或空间世代已变化。
        """
        row = _operation(sql, lease.operation_id)
        if row is not None:
            sql.run(f'SELECT session_id FROM mnemotrace.sessions WHERE {SCOPE} AND session_id = %s FOR UPDATE',
                    (*sql.scope, row['session_id']))
            row = _operation(sql, lease.operation_id, lock=True)
        now = sql.run('SELECT clock_timestamp()').fetchone()[0]
        if (not row or row['state'] != 'running' or row['lease_token'] != lease.token
                or row['lease_until'] <= now or not self._current(sql, row)):
            raise VersionConflict('Task lease or generation changed')
        return row, now

    def freeze_request(self, lease, request):
        """保存抽取请求并固定其引用的原文版本。

        Args:
            lease: 当前任务租约。
            request: 已准备好的抽取请求及原文版本。

        Raises:
            VersionConflict: 租约已失效。
        """
        with self.dao._transaction() as sql:
            self._leased(sql, lease)
            sql.delete('operation_sources', 'operation_id = %s AND is_context', (lease.operation_id,))
            for source in request['pending_versions']:
                sql.update('operation_sources', {'content_version': source['content_version']},
                           'operation_id = %s AND source_id = %s AND NOT is_context',
                           (lease.operation_id, source['source_id']))
            sql.update('operations', {'planned_request': request}, 'operation_id = %s', (lease.operation_id,))
            for index, item in enumerate(request['context_versions'], start=len(_sources(sql, lease.operation_id))):
                sql.insert('operation_sources', dict(operation_id=lease.operation_id, input_order=index,
                                                     is_context=True, **item), suffix='ON CONFLICT DO NOTHING')

    def renew(self, lease):
        """延长当前有效租约。

        Args:
            lease: 当前任务租约。

        Raises:
            VersionConflict: 租约已失效。
        """
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            sql.update('operations', {'lease_until': now + timedelta(seconds=LEASE_SECONDS)},
                       'operation_id = %s', (row['operation_id'],))

    def _release(self, sql, row, state, code=None, *, due=None, **values):
        sql.update('operations', dict(state=state, lease_token=None, lease_until=None,
                   error_code=code, next_attempt_at=due, **values),
                   'operation_id = %s', (row['operation_id'],))

    def fail(self, lease, code, retryable):
        """释放租约并按剩余重试次数更新失败状态。

        Args:
            lease: 当前任务租约。
            code: 持久化的错误代码。
            retryable: 该错误是否允许重试。

        Raises:
            VersionConflict: 租约已失效。
        """
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            attempt = row[_attempt_counter(row)]
            if retryable and attempt < ATTEMPT_LIMIT:
                if code == 'version_conflict':
                    self._release(sql, row, 'pending', code, ready_at=now)
                else:
                    due = now + timedelta(seconds=RETRY_DELAYS[attempt - 1])
                    self._release(sql, row, 'retry_wait', code, due=due)
            else:
                self._release(sql, row, 'failed', code)

    def retry(self, operation_id):
        """重新排队同一空间世代内的失败或待重试任务。

        Args:
            operation_id: 任务操作 ID。

        Returns:
            当前任务快照。任务不存在时返回 None。

        Raises:
            VersionConflict: 另一个画像刷新任务正在执行。
        """
        with self.dao._transaction() as sql:
            row = _operation(sql, operation_id)
            if row and row['state'] in ('failed', 'retry_wait') and self._current(sql, row):
                if row['kind'] == 'profile' and sql.run(
                    f"SELECT 1 FROM mnemotrace.operations WHERE {SCOPE} AND kind = 'profile' "
                    "AND state = ANY(%s) AND operation_id <> %s",
                    (*sql.scope, list(ACTIVE), operation_id),
                ).fetchone():
                    raise VersionConflict('Another profile refresh is active')
                now = sql.run('SELECT clock_timestamp()').fetchone()[0]
                self._release(sql, row, 'pending', generation_attempts=0, embed_attempts=0, ready_at=now)
        return self.operation(operation_id)

    def load(self, view, lease):
        """读取租约对应的抽取任务及其输入。

        Args:
            view: 当前空间的读取快照。
            lease: 当前任务租约。

        Returns:
            会话快照标识、任务行和按输入顺序排列的原文记录。

        Raises:
            VersionConflict: 租约或空间世代已变化。
            DataCorruption: 任务的原文引用缺失或无效。
        """
        sql = view._sql
        row = _operation(sql, lease.operation_id)
        if not row or row['state'] != 'running' or row['lease_token'] != lease.token:
            raise VersionConflict('Task lease changed')
        if row['space_generation'] != view.snapshot.space_generation:
            raise VersionConflict('Task generation changed')
        snapshot = replace(view.snapshot, session_id=row['session_id'])
        refs = _sources(sql, lease.operation_id)
        records = {record.ref.record_id: record for record in view.sources(tuple(s.source_id for s in refs))}
        if not refs or any(s.source_id not in records for s in refs):
            raise DataCorruption('Invalid operation source references')
        return snapshot, row, tuple(records[s.source_id] for s in refs)

    def product(self, view, snapshot, lease, row):
        """读取并验证已保存的抽取检查点。

        Args:
            view: 当前空间的读取快照。
            snapshot: 任务的快照标识。
            lease: 当前任务租约。
            row: 当前任务行。

        Returns:
            可复用性待检查的抽取结果。尚未保存时返回 None。

        Raises:
            DataCorruption: 检查点缺失或内容不一致。
        """
        sql = view._sql
        saved = sql.rows(f'SELECT * FROM mnemotrace.operation_products WHERE {SCOPE} AND operation_id = %s',
                         (*sql.scope, lease.operation_id))
        if not saved:
            if row['extraction_saved']:
                raise DataCorruption('Missing extraction checkpoint')
            return None
        try:
            data = saved[0]
            if (not row['extraction_saved'] or data['result_hash'] != content_hash(data['result'])
                    or data['input_hash'] != content_hash(data['input'])
                    or data['input']['operation'] != row['kind']
                    or row['extraction_config'] != data['extraction_config']
                    or row['context_revision'] != data['context_revision']):
                raise ValueError('Invalid checkpoint metadata')
            if data['context_revision'] is not None:
                snapshot = replace(snapshot, core_revision=data['context_revision'])
            versions = tuple(SourceVersion(**source) for source in data['source_versions'])
            members = {ref.source_id for ref in _sources(sql, lease.operation_id)}
            if {item['id'] for item in data['input']['pending']} != members:
                raise ValueError('Checkpoint does not cover the frozen window')
            if not set(versions) <= set(_sources(sql, lease.operation_id, context=True)):
                raise ValueError('Checkpoint references differ from operation')
            return ExtractionProduct(CommitConditions(snapshot, versions, lease, data['extraction_config']),
                                     json.dumps(data['input']), json.dumps(data['result']),
                                     data['digest_max_chars'],
                                     data['context_revision'], data['format_version'])
        except (TypeError, ValueError, KeyError) as exc:
            raise DataCorruption('Invalid extraction checkpoint') from exc

    def save_product(self, product, settings):
        """保存生成结果并将任务推进到向量处理阶段。

        Args:
            product: 抽取输入、结果和提交前置条件。
            settings: 生成时使用的空间设置。

        Raises:
            ValueError: 抽取结果属于其他空间。
            VersionConflict: 租约、设置或原文版本已变化。
            DataCorruption: 抽取结果与计划引用的原文不一致。
        """
        conditions = product.conditions
        lease, snapshot = conditions.lease, conditions.snapshot
        if snapshot.context != self.dao.context:
            raise ValueError('Product belongs to another space')
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            revision, _, current_settings, _ = _space_row(sql)
            if (row['phase'] != 'generate'
                    or any(current_settings.get(field) != getattr(settings, field) for field in EXTRACTION_POLICY_FIELDS)
                    or product.context_revision is not None and revision != snapshot.core_revision):
                raise VersionConflict('Extraction context changed')
            data, result = json.loads(product.input_json), json.loads(product.result_json)
            members = {ref.source_id for ref in _sources(sql, lease.operation_id)}
            versions = {ref.source_id: ref for ref in conditions.sources}
            if ({item['id'] for item in data['pending']} != members
                    or set(conditions.sources) != set(_sources(sql, lease.operation_id, context=True))):
                raise DataCorruption('Product does not match the saved request sources')
            for item in (*data['context'], *data['pending']):
                record = sql.get(SOURCE, item['id'])
                if record is None or record.content_version != versions[item['id']].content_version:
                    raise VersionConflict('Source changed while extracting')
                episode = domain(record)
                if (episode.session_id != row['session_id'] or source_fingerprint(episode) != item['source']
                        or item['representation_source'] is not None
                        and content_hash(episode.digest) != item['representation_source']):
                    raise VersionConflict('Source representation changed while extracting')
            sql.insert('operation_products', dict(
                operation_id=lease.operation_id, input=data, result=result, format_version=product.format_version,
                extraction_config=conditions.extraction_config, context_revision=product.context_revision,
                source_versions=[asdict(ref) for ref in conditions.sources],
                digest_max_chars=product.digest_max_chars, result_hash=content_hash(result), input_hash=content_hash(data)))
            sql.update('operations', dict(phase='embed', embed_attempts=1, extraction_saved=True,
                          extraction_config=conditions.extraction_config, context_revision=product.context_revision,
                          error_code=None),
                       'operation_id = %s', (lease.operation_id,))

    def regenerate(self, lease):
        """清除过期检查点并重新安排抽取。

        Args:
            lease: 当前任务租约。

        Raises:
            VersionConflict: 租约已失效。
        """
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            self._reset_checkpoint(sql, row, now)

    def _reset_checkpoint(self, sql, row, now):
        """移除抽取结果和历史引用并重置任务生成阶段。

        Args:
            sql: 已限定空间的写事务。
            row: 当前任务行。
            now: 重新排队使用的数据库时间。
        """
        sql.delete('operation_products', 'operation_id = %s', (row['operation_id'],))
        sql.delete('operation_sources', 'operation_id = %s AND is_context', (row['operation_id'],))
        self._release(sql, row, 'pending', phase='generate', ready_at=now, extraction_saved=False,
                      generation_attempts=0, embed_attempts=0,
                      extraction_config=None, context_revision=None, planned_request=None)

    def commit(self, changes):
        """提交任务产生的记忆变更并清除抽取检查点。

        Args:
            changes: 待提交的记忆变更及前置条件。

        Returns:
            提交收据。
        """
        def apply(sql, revision, generation, settings):
            applied = _apply_changes(sql, changes, revision, generation, settings)
            sql.delete('operation_products', 'operation_id = %s', (changes.operation_id,))
            row = _operation(sql, changes.operation_id)
            if row and row['kind'] == 'extract':
                sql.update('operations', {'planned_request': None}, 'operation_id = %s', (changes.operation_id,))
            return applied

        return self.dao._mutate(changes.operation_id, apply)
