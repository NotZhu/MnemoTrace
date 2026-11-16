"""写入管理。"""
from dataclasses import asdict

from ...profile import INPUT_FIELDS
from ...util import gen_id
from ..contracts import OperationKind
from .errors import DataCorruption, DatabaseConstraintError, MessageConflict, VersionConflict
from .lexical import project_document, project_text
from .records import (
    ENTITY, FACT, RELATION_TYPE, SOURCE, SCOPE, TARGET_COLUMNS,
    domain, timestamp,
)


def allocate_sequence(sql, session_id):
    """为已存在的会话分配下一个原文序号。

    Args:
        sql: 已限定记忆空间的事务。
        session_id: 会话 ID。

    Returns:
        从 0 开始的会话内原文序号。
    """
    return sql.run(f'UPDATE mnemotrace.sessions SET next_sequence = next_sequence + 1 '
                   f'WHERE {SCOPE} AND session_id = %s RETURNING next_sequence - 1',
                   (*sql.scope, session_id)).fetchone()[0]


def check_version(sql, ref, expected):
    """验证提交引用的内容版本。

    Args:
        sql: 已限定空间的事务。
        ref: 要检查的记录引用。
        expected: 提交时要求的内容版本。

    Raises:
        VersionConflict: 记录类型不受支持，或当前内容版本不符。
    """
    if ref.kind not in TARGET_COLUMNS or sql.version(ref) != expected:
        raise VersionConflict('Referenced content version changed')


def save_embedding(sql, item):
    """为对应的文本或摘要版本保存向量。

    Args:
        sql: 已限定记忆空间的事务。
        item: 向量及其关联记录版本。

    Raises:
        VersionConflict: 对应的文本或摘要版本已变化。
    """
    if sql.version(item.record, digest=item.field == 'digest') != item.content_version:
        raise VersionConflict('Embedding refers to an obsolete text version')
    # Bind the pgvector text representation as data. The database enforces its dimension.
    column = TARGET_COLUMNS[item.record.kind]
    sql.insert('embeddings', {
        column: item.record.record_id, 'target_kind': item.record.kind.value,
        'field': item.field, 'content_version': item.content_version, 'model': item.model,
        'dimension': len(item.vector), 'vector': '[' + ','.join(map(str, item.vector)) + ']',
    }, suffix='ON CONFLICT DO NOTHING')


def _replace_fact_sources(sql, fact_id, provenance):
    source_ids = list(dict.fromkeys(provenance))
    sql.delete('fact_sources', 'fact_id = %s AND NOT (source_id = ANY(%s))', (fact_id, source_ids))
    for order, source_id in enumerate(source_ids):
        sql.insert('fact_sources', {'fact_id': fact_id, 'source_id': source_id, 'source_order': order},
                   suffix="""ON CONFLICT (user_id, space_id, fact_id, source_id) DO UPDATE
                       SET source_order = excluded.source_order
                       WHERE fact_sources.source_order IS DISTINCT FROM excluded.source_order""")


def _source_digest(sql, record, values, old):
    """写入原文摘要及覆盖范围，并同步摘要索引。

    Args:
        sql: 已限定记忆空间的事务。
        record: 待更新的原文记录。
        values: 新的原文与摘要字段。
        old: 旧的原文与摘要字段。

    Returns:
        摘要文本或覆盖范围发生变化时返回 True。

    Raises:
        VersionConflict: 摘要版本已变化。
    """
    digest = values['digest']
    previous = old['digest'] if old is not None else ''
    if digest == previous and (old is None or values['digest_coverage'] == old['digest_coverage']):
        return False
    source_id = record.ref.record_id
    row = sql.run(f'SELECT digest_version FROM mnemotrace.source_digests '
                  f'WHERE {SCOPE} AND source_id = %s', (*sql.scope, source_id)).fetchone()
    changed = digest != previous
    # A cleared digest reserves the next version and invalidates old vectors.
    version = row[0] + int(changed and bool(previous)) if row else 1
    if record.digest_version != (version if digest else None):
        raise VersionConflict('Source digest version changed')
    data = dict(digest=digest, digest_coverage=values['digest_coverage'],
                digest_version=version, content_version=record.content_version)
    if changed:
        sql.delete('embeddings', "source_id = %s AND field = 'digest'", (source_id,))
        data['digest_written_at'] = sql.run('SELECT clock_timestamp()').fetchone()[0] if digest else None
    if row:
        sql.update('source_digests', data, 'source_id = %s', (source_id,))
    else:
        sql.insert('source_digests', {'source_id': source_id, **data})
    if changed:
        project_document(sql, SOURCE, source_id, version, digest or None, field='digest')
    return True


def write(sql, change, graph):
    """校验并写入单条记录，同步其图和文本索引。

    Args:
        sql: 已限定记忆空间的事务。
        change: 待写记录及期望版本。
        graph: 当前事务中的图投影。

    Returns:
        记录是否变化及是否影响用户画像的二元组。

    Raises:
        VersionConflict: 记录版本或内容版本不符。
        DatabaseConstraintError: 写入违反记录约束。
    """
    record = change.record
    kind, record_id = record.ref.kind, record.ref.record_id
    previous = sql.get(kind, record_id)
    if (previous.record_version if previous else None) != change.expected_version:
        raise VersionConflict('Record version changed')
    old = asdict(domain(previous)) if previous else None
    current = asdict(domain(record))
    text_fields = {
        FACT: ('text',), ENTITY: ('name',),
    }.get(kind, ())
    text_changed = old is not None and any(old[k] != current[k] for k in text_fields)
    expected_content = (previous.content_version if previous else 1) + int(text_changed)
    if record.content_version != expected_content:
        raise VersionConflict('Incorrect content version for record change')
    for key in ('embedding', 'digest_embedding'):
        if key in current:
            current[key] = None
    if kind == FACT:
        if current['source'] != 'extracted':
            raise DatabaseConstraintError('Product facts require an extracted source')
        current.update(last_access=current['created_at'], access_count=0)
    if kind == SOURCE:
        current['metadata'] = {}
        digest_fields = {'digest', 'coverage', 'digest_coverage', 'consolidated', 'digest_written_at'}
        if old is None:
            if current['message_id'] is not None:
                raise DatabaseConstraintError('Dialogue messages must use the message acceptance path')
        elif (previous.agent_id != record.agent_id
              or any(current[k] != old[k] for k in current.keys() - digest_fields)):
            raise DatabaseConstraintError('Source text and ingestion fields are immutable')
        digest_changed = _source_digest(sql, record, current, old) if previous else False
        if previous:
            progress_changed = any(current[key] != old[key] for key in ('coverage', 'consolidated'))
            if digest_changed or progress_changed:
                sql.update('sources', {'record_version': record.record_version,
                                       'coverage': current['coverage'],
                                       'consolidated': current['consolidated']}, 'id = %s', (record_id,))
            return digest_changed or progress_changed, False
    elif current == old:
        return False, False
    if text_changed:
        column = TARGET_COLUMNS.get(kind)
        if column is not None:
            sql.delete('embeddings', f'{column} = %s', (record_id,))
            sql.delete('lexical_documents', f'{column} = %s', (record_id,))
    sql.write_record(record, insert=previous is None)
    if kind == FACT and (old is None or text_changed or current['valid_at'] != old['valid_at']):
        project_text(sql, kind, record_id, record.content_version, current)
    if kind == SOURCE:
        _source_digest(sql, record, current, old)
    elif kind == FACT:
        if old is None or current['provenance'] != old['provenance']:
            _replace_fact_sources(sql, record_id, current['provenance'])
        if old is None or current['supersedes'] != old['supersedes']:
            sql.delete('fact_supersedes', 'fact_id = %s AND NOT (predecessor_id = ANY(%s))',
                       (record_id, current['supersedes']))
            for predecessor_id in current['supersedes']:
                sql.insert('fact_supersedes', {'fact_id': record_id, 'predecessor_id': predecessor_id},
                           suffix='ON CONFLICT DO NOTHING')
        graph_fields = ('subject_id', 'object_id', 'relation_id',
                        'valid_at', 'invalid_at')
        if old is None or any(old[k] != current[k] for k in graph_fields):
            graph.project(record_id)
    elif kind in (ENTITY, RELATION_TYPE) and previous is not None:
        column = 'relation_id' if kind == RELATION_TYPE else 'subject_id'
        condition = f'{column} = %s'
        args = (record_id,)
        if kind == ENTITY:
            condition += ' OR object_id = %s'
            args = (record_id, record_id)
        for (fact_id,) in sql.run(f'SELECT id FROM mnemotrace.facts WHERE {SCOPE} AND ({condition})',
                                  (*sql.scope, *args)).fetchall():
            graph.project(fact_id)
    profile_changed = kind == FACT and (old is None or any(old[key] != current[key] for key in INPUT_FIELDS))
    return True, profile_changed


def enqueue(sql, spec, space_generation, *, ready_at=None, planned_request=None, sequence=None):
    """在当前事务中添加任务及其输入引用。

    Args:
        sql: 已限定记忆空间的事务。
        spec: 任务定义及原文引用。
        space_generation: 当前空间世代。
        ready_at: 可领取任务的最早时间。
        planned_request: 可选的预先生成的请求计划。
        sequence: 可选的任务顺序号。
    """
    if sequence is None and spec.kind is OperationKind.EXTRACT:
        # All writes for this space hold its row lock.
        sequence = sql.run(f"SELECT coalesce(max(sequence) + 1, 0) FROM mnemotrace.operations "
                           f"WHERE {SCOPE} AND kind = 'extract'", sql.scope).fetchone()[0]
    if ready_at is None and spec.kind is OperationKind.EXTRACT:
        ready_at = sql.run('SELECT clock_timestamp()').fetchone()[0]
    sql.insert('operations', dict(
        operation_id=spec.operation_id, kind=spec.kind.value, session_id=spec.session_id,
        sequence=sequence, state='pending', phase='queued', attempts=0, embed_attempts=0,
        space_generation=space_generation,
        extraction_saved=False, core_committed=False,
        ready_at=ready_at, planned_request=planned_request,
    ))
    for order, source in enumerate(spec.sources):
        append_input(sql, spec.operation_id, spec.session_id, source, order)


def append_input(sql, operation_id, session_id, source, order):
    """为任务写入指定的原文版本引用。

    Args:
        sql: 已限定记忆空间的事务。
        operation_id: 任务操作 ID。
        session_id: 任务所在的会话 ID。
        source: 原文 ID 和内容版本。
        order: 该原文在任务输入中的位置。

    Raises:
        VersionConflict: 原文缺失或内容版本变化。
        DatabaseConstraintError: 原文不属于该会话，或内容为空。
    """
    record = sql.get(SOURCE, source.source_id)
    if record is None or record.content_version != source.content_version:
        raise VersionConflict('Referenced content version changed')
    episode = domain(record)
    if episode.session_id != session_id or not episode.content:
        raise DatabaseConstraintError('Operation input must belong to its session')
    sql.insert('operation_sources', dict(
        operation_id=operation_id, source_id=source.source_id,
        content_version=source.content_version, input_order=order,
    ))


def check_lease(sql, conditions):
    """验证提交所持租约及其冻结的输入。

    Args:
        sql: 已限定记忆空间的事务。
        conditions: 提交依赖的租约、快照和原文版本。

    Raises:
        VersionConflict: 租约、任务前置状态或原文引用已变化。
        DataCorruption: 抽取任务缺少已保存的检查点。
    """
    lease = conditions.lease
    row = sql.run(f"""SELECT kind, state, extraction_saved FROM mnemotrace.operations WHERE {SCOPE}
        AND operation_id = %s AND lease_token = %s AND lease_until > clock_timestamp()
        AND space_generation = %s AND session_id IS NOT DISTINCT FROM %s
        AND extraction_config IS NOT DISTINCT FROM %s
        FOR UPDATE""", (*sql.scope, lease.operation_id, lease.token,
        conditions.snapshot.space_generation, conditions.snapshot.session_id,
        conditions.extraction_config)).fetchone()
    if not row or row[1] != 'running' or row[0] == OperationKind.EXTRACT.value and not row[2]:
        raise VersionConflict('Task lease or prerequisites changed')
    product = sql.run(f'SELECT source_versions FROM mnemotrace.operation_products '
                      f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, lease.operation_id)).fetchone()
    if product is None and row[0] == OperationKind.EXTRACT.value:
        raise DataCorruption('Missing extraction checkpoint')
    sources = [(item['source_id'], item['content_version']) for item in product[0]] if product else sql.run(
        f'SELECT source_id, content_version FROM mnemotrace.operation_sources '
        f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, lease.operation_id)).fetchall()

    if set(sources) != {(item.source_id, item.content_version) for item in conditions.sources}:
        raise VersionConflict('Task input references changed')


def commit_task(sql, lease):
    """将已提交记忆变更的任务标记为完成并释放租约。

    Args:
        sql: 已限定记忆空间的事务。
        lease: 已验证的任务租约。
    """
    sql.update('operations', dict(state='completed', phase='done', core_committed=True,
               lease_token=None, lease_until=None, next_attempt_at=None, error_code=None),
               'operation_id = %s', (lease.operation_id,))


def store_messages(sql, session_id, messages, *, extract, check_ready=None):
    """在当前事务中写入原始消息。

    Args:
        sql: 已限定记忆空间的事务。
        session_id: 会话 ID。
        messages: 规范化的消息记录。
        extract: 是否标记为待抽取。
        check_ready: 写入前的可选就绪检查。

    Returns:
        按输入顺序排列的原文记录 ID 和新增记录数。

    Raises:
        MessageConflict: 请求引用了冲突的已有消息。
    """
    existing = sql.run(f'SELECT message_id,id,message_fingerprint,extract FROM mnemotrace.sources '
                       f'WHERE {SCOPE} AND session_id = %s AND message_id = ANY(%s)',
                       (*sql.scope, session_id, [msg.message_id for msg in messages])).fetchall()
    known = {key: (record_id, fingerprint, eligible) for key, record_id, fingerprint, eligible in existing}
    if extract and known:
        raise MessageConflict('Retry the original request without changing its members or order')
    if check_ready is not None:
        check_ready()
    sql.ensure_session(session_id)
    saved = []
    inserted = 0
    for item in messages:
        content_hash = item.fingerprint
        if item.message_id in known:
            record_id, stored_hash, eligible = known[item.message_id]
            if content_hash != stored_hash or extract != eligible:
                raise MessageConflict('Message identity was already used for a different input')
        else:
            record_id = gen_id('ep')
            sql.insert('sources', dict(
                id=record_id, session_id=session_id, message_id=item.message_id,
                content=item.content, role=item.role, event_time=timestamp(item.event_time),
                ingested_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
                extract=extract,
                agent_id=item.agent_id, message_fingerprint=content_hash,
                sequence=allocate_sequence(sql, session_id), record_version=1, content_version=1,
            ))
            known[item.message_id] = (record_id, content_hash, extract)
            inserted += 1
        saved.append(record_id)
    return tuple(saved), inserted
