"""Apply explicit record changes inside the space write transaction."""
from dataclasses import asdict

from ...util import gen_id
from ..contracts import OperationKind, OperationStats, RecordRef
from .errors import DatabaseConstraintError, MessageConflict, VersionConflict
from .records import (
    ENTITY, FACT, SOURCE, WORKING, SCOPE, TARGET_COLUMNS,
    domain, timestamp,
)


def allocate_sequence(sql, session_id):
    return sql.run(f'UPDATE mnemotrace.sessions SET next_sequence = next_sequence + 1 '
                   f'WHERE {SCOPE} AND session_id = %s RETURNING next_sequence - 1',
                   (*sql.scope, session_id)).fetchone()[0]


def check_version(sql, ref, expected):
    if ref.kind not in TARGET_COLUMNS or sql.version(ref) != expected:
        raise VersionConflict('Referenced content version changed')


def save_embedding(sql, item):
    if sql.version(item.record, summary=item.field == 'summary') != item.content_version:
        raise VersionConflict('Embedding refers to an obsolete text version')
    # Bind the pgvector text representation as data. The database enforces its dimension.
    column = TARGET_COLUMNS[item.record.kind]
    return bool(sql.insert('embeddings', {
        column: item.record.record_id, 'target_kind': item.record.kind.value,
        'field': item.field, 'content_version': item.content_version, 'model': item.model,
        'dimension': len(item.vector), 'vector': '[' + ','.join(map(str, item.vector)) + ']',
    }, suffix='ON CONFLICT DO NOTHING').rowcount)


def _replace_links(sql, table, column, record_id, ids, *, ordered=False):
    sql.delete(table, f'{column} = %s AND NOT (source_id = ANY(%s))', (record_id, ids))
    for order, source_id in enumerate(ids):
        values = {column: record_id, 'source_id': source_id}
        suffix = 'ON CONFLICT DO NOTHING'
        if ordered:
            values['source_order'] = order
            suffix = f"""ON CONFLICT (user_id, space_id, {column}, source_id) DO UPDATE
                SET source_order = excluded.source_order
                WHERE {table}.source_order IS DISTINCT FROM excluded.source_order"""
        sql.insert(table, values, suffix=suffix)


def _source_summary(sql, record, values, old):
    keys = ('summary', 'coverage', 'summary_coverage', 'consolidated')
    if old is None and not any(values[key] for key in keys):
        return False
    if old is not None and all(values[key] == old[key] for key in keys):
        return False
    summary_changed = old is None or old['summary'] != values['summary']
    source_id = record.ref.record_id
    if summary_changed:
        sql.delete('embeddings', "source_id = %s AND field = 'summary'", (source_id,))
    row = sql.run(f'SELECT summary_version,record_version FROM mnemotrace.source_summaries '
                  f'WHERE {SCOPE} AND source_id = %s', (*sql.scope, source_id)).fetchone()
    version = row[0] + int(summary_changed) if row else 1
    if record.summary_version != (version if values['summary'] else None):
        raise VersionConflict('Source summary version changed')
    data = {key: values[key] for key in keys[:3]}
    data.update(summary_version=version, content_version=record.content_version,
                record_version=row[1] + 1 if row else 1,
                processing_state='completed' if values['consolidated'] else 'pending')
    if row:
        sql.update('source_summaries', data, 'source_id = %s', (source_id,))
    else:
        sql.insert('source_summaries', {'source_id': source_id, **data})
    return True


def write(sql, change, graph, explicit_edges):
    record = change.record
    kind, record_id = record.ref.kind, record.ref.record_id
    previous = sql.get(kind, record_id)
    if (previous.record_version if previous else None) != change.expected_version:
        raise VersionConflict('Record version changed')
    old = asdict(domain(previous)) if previous else None
    current = asdict(domain(record))
    text_fields = {
        FACT: ('text',), WORKING: ('content',), ENTITY: ('name',),
    }.get(kind, ())
    text_changed = old is not None and any(old[k] != current[k] for k in text_fields)
    expected_content = (previous.content_version if previous else 1) + int(text_changed)
    if record.content_version != expected_content:
        raise VersionConflict('Incorrect content version for record change')
    for key in ('embedding', 'summary_embedding'):
        if key in current:
            current[key] = None
    if kind == SOURCE:
        summary_fields = {'summary', 'coverage', 'summary_coverage', 'consolidated'}
        if old is None:
            if current['message_id'] is not None:
                raise DatabaseConstraintError('Dialogue messages must use the message acceptance path')
        elif (previous.agent_id != record.agent_id
              or any(current[k] != old[k] for k in current.keys() - summary_fields)):
            raise DatabaseConstraintError('Source text and ingestion fields are immutable')
        summary_changed = _source_summary(sql, record, current, old) if previous else False
        if previous:
            if summary_changed:
                sql.update('sources', {'record_version': record.record_version}, 'id = %s', (record_id,))
            return summary_changed
    elif current == old:
        return False
    if text_changed:
        column = TARGET_COLUMNS[kind]
        sql.delete('embeddings', f'{column} = %s', (record_id,))
        sql.delete('lexical_documents', f'{column} = %s', (record_id,))
    sql.write_record(record, insert=previous is None)
    if kind == SOURCE:
        _source_summary(sql, record, current, old)
    elif kind == FACT:
        _replace_links(sql, 'fact_sources', 'fact_id', record_id,
                       list(dict.fromkeys(current['provenance'])), ordered=True)
        graph_fields = ('subject', 'predicate', 'object', 'valid_at', 'invalid_at', 'expired_at')
        if record_id not in explicit_edges and (old is None or any(old[k] != current[k] for k in graph_fields)):
            graph.project(record_id)
    elif kind == WORKING:
        ids = current['metadata'].get('provenance', [])
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise DatabaseConstraintError('Invalid working memory provenance')
        _replace_links(sql, 'working_sources', 'working_id', record_id, sorted(set(ids)))
    return True


def invalidate(sql, item, graph):
    record_id = item.ref.record_id
    row = sql.update('facts', {
        'record_version': item.expected_version + 1,
        'invalid_at': timestamp(item.invalid_at), 'expired_at': timestamp(item.expired_at),
    }, 'id = %s AND record_version = %s', (record_id, item.expected_version))
    if row.rowcount != 1:
        raise VersionConflict('Fact version changed')
    graph.project(record_id)


def enqueue(sql, spec, space_generation, session_generation):
    sequence = None if spec.session_id is None else allocate_sequence(sql, spec.session_id)
    sql.insert('operations', dict(
        operation_id=spec.operation_id, kind=spec.kind.value, session_id=spec.session_id,
        sequence=sequence, state='pending', phase='queued', attempts=0, embed_attempts=0,
        space_generation=space_generation, session_generation=session_generation,
        stats=asdict(OperationStats(0, 0, 0, 0, 0, 0)), stored_raw=True,
        extraction_saved=False, core_committed=False,
    ))
    for order, source in enumerate(spec.sources):
        ref = RecordRef(sql.context, SOURCE, source.source_id)
        record = sql.get(SOURCE, source.source_id)
        check_version(sql, ref, source.content_version)
        episode = domain(record)
        if episode.session_id != spec.session_id or not episode.content:
            raise DatabaseConstraintError('Operation input must belong to its session')
        sql.insert('operation_sources', dict(
            operation_id=spec.operation_id, source_id=source.source_id,
            content_version=source.content_version, input_start=0,
            input_end=len(episode.content), input_order=order, role=episode.role,
        ))
    for target in spec.targets:
        check_version(sql, target.record, target.content_version)
        if spec.session_id is not None and target.record.kind in (SOURCE, WORKING):
            record = sql.get(target.record.kind, target.record.record_id)
            if domain(record).session_id != spec.session_id:
                raise DatabaseConstraintError('Operation target must belong to its session')
        sql.insert('operation_targets', {
            'operation_id': spec.operation_id,
            TARGET_COLUMNS[target.record.kind]: target.record.record_id,
            'target_kind': target.record.kind.value, 'content_version': target.content_version,
        })


def check_lease(sql, conditions):
    lease = conditions.lease
    row = sql.run(f"""SELECT kind, state, extraction_saved FROM mnemotrace.operations WHERE {SCOPE}
        AND operation_id = %s AND lease_token = %s AND lease_until > clock_timestamp()
        AND space_generation = %s AND session_id IS NOT DISTINCT FROM %s
        AND session_generation IS NOT DISTINCT FROM %s AND extraction_config IS NOT DISTINCT FROM %s
        FOR UPDATE""", (*sql.scope, lease.operation_id, lease.token,
        conditions.snapshot.space_generation, conditions.snapshot.session_id,
        conditions.snapshot.session_generation, conditions.extraction_config)).fetchone()
    if not row or row[1] != 'running' or row[0] == OperationKind.EXTRACT.value and not row[2]:
        raise VersionConflict('Task lease or prerequisites changed')
    sources = sql.run(f'SELECT DISTINCT source_id, content_version FROM mnemotrace.operation_sources '
                      f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, lease.operation_id)).fetchall()
    targets = sql.run(f'SELECT target_kind, coalesce(source_id, fact_id, working_id, entity_id), '
                      f'content_version FROM mnemotrace.operation_targets WHERE {SCOPE} AND operation_id = %s',
                      (*sql.scope, lease.operation_id)).fetchall()
    if (set(sources) != {(item.source_id, item.content_version) for item in conditions.sources}
            or set(targets) != {(item.record.kind.value, item.record.record_id, item.content_version)
                                for item in conditions.targets}):
        raise VersionConflict('Task input references changed')


def commit_task(sql, lease, receipt):
    sql.update('operations', dict(state='completed', phase='done', core_committed=True,
               lease_token=None, lease_until=None, next_attempt_at=None, error_code=None),
               'operation_id = %s', (lease.operation_id,))
    sql.insert('operation_receipts', dict(
        operation_id=receipt.operation_id, core_revision=receipt.core_revision,
        fact_ids=list(receipt.fact_ids), working_ids=list(receipt.working_ids),
        completed_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
    ))


def store_messages(sql, session_id, messages):
    sql.ensure_session(session_id)
    existing = sql.run(f'SELECT message_id,id,message_fingerprint FROM mnemotrace.sources '
                       f'WHERE {SCOPE} AND session_id = %s AND message_id = ANY(%s)',
                       (*sql.scope, session_id, [msg.message_id for msg in messages])).fetchall()
    known = {key: (record_id, digest) for key, record_id, digest in existing}
    saved, inserted = [], 0
    for item in messages:
        digest = item.fingerprint
        if item.message_id in known:
            record_id, stored_digest = known[item.message_id]
            if digest != stored_digest:
                raise MessageConflict('Message identity was already used for a different input')
        else:
            record_id = gen_id('ep')
            sql.insert('sources', dict(
                id=record_id, session_id=session_id, message_id=item.message_id,
                content=item.content, role=item.role, event_time=timestamp(item.event_time),
                ingested_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
                source_kind='dialogue', scope=item.scope, mode=item.mode,
                agent_id=item.agent_id, message_fingerprint=digest, metadata={},
                sequence=allocate_sequence(sql, session_id), record_version=1, content_version=1,
            ))
            known[item.message_id] = (record_id, digest)
            inserted += 1
        saved.append(record_id)
    return tuple(saved), inserted
