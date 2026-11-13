"""Apply explicit record changes inside the space write transaction."""
from dataclasses import asdict

from ...profile import INPUT_FIELDS
from ...util import gen_id
from ...types import relation_identifier
from ..contracts import OperationKind, OperationStats
from .errors import DatabaseConstraintError, MessageConflict, VersionConflict
from .lexical import project_document, project_text
from .records import (
    CONFLICT, ENTITY, ENTITY_ALIAS, FACT, RELATION_TYPE, SOURCE, SCOPE, TARGET_COLUMNS,
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
    if sql.version(item.record, digest=item.field == 'digest') != item.content_version:
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


def _source_digest(sql, record, values, old):
    keys = ('digest', 'coverage', 'digest_coverage', 'consolidated')
    if old is None and not any(values[key] for key in keys):
        return False
    if old is not None and all(values[key] == old[key] for key in keys):
        return False
    digest_changed = old is None or old['digest'] != values['digest']
    source_id = record.ref.record_id
    if digest_changed:
        sql.delete('embeddings', "source_id = %s AND field = 'digest'", (source_id,))
    row = sql.run(f'SELECT digest_version,record_version FROM mnemotrace.source_digests '
                  f'WHERE {SCOPE} AND source_id = %s', (*sql.scope, source_id)).fetchone()
    # Coverage can create the row before the first digest text exists.
    version = row[0] + int(digest_changed and bool(old['digest'])) if row else 1
    if record.digest_version != (version if values['digest'] else None):
        raise VersionConflict('Source digest version changed')
    data = {key: values[key] for key in keys[:3]}
    data.update(digest_version=version, content_version=record.content_version,
                record_version=row[1] + 1 if row else 1,
                processing_state='completed' if values['consolidated'] else 'pending')
    if digest_changed:
        data['digest_written_at'] = (
            sql.run('SELECT clock_timestamp()').fetchone()[0] if values['digest'] else None
        )
    if row:
        sql.update('source_digests', data, 'source_id = %s', (source_id,))
    else:
        sql.insert('source_digests', {'source_id': source_id, **data})
    if digest_changed:
        project_document(sql, SOURCE, source_id, version, values['digest'] or None, field='digest')
    return True


def write(sql, change, graph):
    record = change.record
    kind, record_id = record.ref.kind, record.ref.record_id
    previous = sql.get(kind, record_id)
    if kind == ENTITY_ALIAS and previous is not None and domain(previous) == domain(record):
        return False, False
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
    if kind == SOURCE:
        digest_fields = {'digest', 'coverage', 'digest_coverage', 'consolidated', 'digest_written_at'}
        if old is None:
            if current['message_id'] is not None:
                raise DatabaseConstraintError('Dialogue messages must use the message acceptance path')
        elif (previous.agent_id != record.agent_id
              or any(current[k] != old[k] for k in current.keys() - digest_fields)):
            raise DatabaseConstraintError('Source text and ingestion fields are immutable')
        digest_changed = _source_digest(sql, record, current, old) if previous else False
        if previous:
            if digest_changed:
                sql.update('sources', {'record_version': record.record_version}, 'id = %s', (record_id,))
            return digest_changed, False
    elif current == old:
        return False, False
    if text_changed:
        column = TARGET_COLUMNS[kind]
        sql.delete('embeddings', f'{column} = %s', (record_id,))
        sql.delete('lexical_documents', f'{column} = %s', (record_id,))
    if kind == FACT and current['relation_id'] == relation_identifier(current['predicate']):
        sql.insert('relation_types', {
            'id': current['relation_id'], 'predicate': current['predicate'],
            'meaning': current['predicate'].replace('_', ' ').strip() or current['predicate'], 'direction': 'forward',
            'endpoint_types': [], 'record_version': 1, 'content_version': 1,
        }, suffix='ON CONFLICT DO NOTHING')
    if kind == CONFLICT:
        for side in ('older', 'newer'):
            fact = sql.get(FACT, current[side])
            if fact is None or fact.record_version != current[side + '_version']:
                raise VersionConflict('Fact changed before the conflict was queued')
    if kind == ENTITY_ALIAS:
        parent = sql.get(FACT, current['fact_id'])
        if parent is None:
            raise DatabaseConstraintError('Alias evidence must reference a stored fact')
        fact = domain(parent)
        if current['entity_id'] not in (fact.subject_id, fact.object_id):
            raise DatabaseConstraintError('Alias evidence must link the same entity')
    sql.write_record(record, insert=previous is None)
    if kind == FACT and (old is None or text_changed or current['valid_at'] != old['valid_at']):
        project_text(sql, kind, record_id, record.content_version, current)
    if kind == SOURCE:
        _source_digest(sql, record, current, old)
    elif kind == FACT:
        if current['withdrawn_at'] is not None:
            sql.delete('conflicts', '(older = %s OR newer = %s)', (record_id, record_id))
        _replace_links(sql, 'fact_sources', 'fact_id', record_id,
                       list(dict.fromkeys(current['provenance'])), ordered=True)
        sql.delete('fact_supersedes', 'fact_id = %s AND NOT (predecessor_id = ANY(%s))',
                   (record_id, current['supersedes']))
        for predecessor_id in current['supersedes']:
            sql.insert('fact_supersedes', {'fact_id': record_id, 'predecessor_id': predecessor_id},
                       suffix='ON CONFLICT DO NOTHING')
        graph_fields = ('subject_id', 'object_id', 'relation_id',
                        'valid_at', 'invalid_at', 'withdrawn_at', 'review_pending')
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


def invalidate(sql, item, graph):
    record_id = item.ref.record_id
    previous = sql.get(FACT, record_id)
    if previous is None or previous.record_version != item.expected_version:
        raise VersionConflict('Fact version changed')
    fact = domain(previous)
    if fact.invalid_at is not None and fact.invalid_at <= item.invalid_at:
        return False
    fact.end_at(item.invalid_at, item.expired_at)
    sql.update('facts', {
        'record_version': item.expected_version + 1,
        'invalid_at': timestamp(fact.invalid_at), 'expired_at': timestamp(fact.expired_at),
    }, 'id = %s', (record_id,))
    graph.project(record_id)
    return True


def enqueue(sql, spec, space_generation, *, ready_at=None, parent_id=None):
    sequence = None if spec.session_id is None else allocate_sequence(sql, spec.session_id)
    if ready_at is None and spec.kind in (OperationKind.EXTRACT, OperationKind.DIGEST):
        ready_at = sql.run('SELECT clock_timestamp()').fetchone()[0]
    sql.insert('operations', dict(
        operation_id=spec.operation_id, kind=spec.kind.value, session_id=spec.session_id,
        sequence=sequence, state='pending', phase='queued', attempts=0, embed_attempts=0,
        space_generation=space_generation,
        stats=asdict(OperationStats(0, 0, 0, 0, 0)), stored_raw=True,
        extraction_saved=False, core_committed=False,
        ready_at=ready_at, parent_id=parent_id,
    ))
    for order, source in enumerate(spec.sources):
        append_input(sql, spec.operation_id, spec.session_id, source, order)
    for target in spec.targets:
        check_version(sql, target.record, target.content_version)
        if spec.session_id is not None and target.record.kind in (SOURCE):
            record = sql.get(target.record.kind, target.record.record_id)
            if domain(record).session_id != spec.session_id:
                raise DatabaseConstraintError('Operation target must belong to its session')
        sql.insert('operation_targets', {
            'operation_id': spec.operation_id,
            TARGET_COLUMNS[target.record.kind]: target.record.record_id,
            'target_kind': target.record.kind.value, 'content_version': target.content_version,
        })


def append_input(sql, operation_id, session_id, source, order):
    record = sql.get(SOURCE, source.source_id)
    if record is None or record.content_version != source.content_version:
        raise VersionConflict('Referenced content version changed')
    episode = domain(record)
    if episode.session_id != session_id or not episode.content:
        raise DatabaseConstraintError('Operation input must belong to its session')
    sql.insert('operation_sources', dict(
        operation_id=operation_id, source_id=source.source_id,
        content_version=source.content_version, input_start=0,
        input_end=len(episode.content), input_order=order, role=episode.role,
    ))


def check_lease(sql, conditions):
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
    sources = [(item['source_id'], item['content_version']) for item in product[0]] if product else sql.run(
        f'SELECT DISTINCT source_id, content_version FROM mnemotrace.operation_sources '
        f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, lease.operation_id)).fetchall()
    targets = sql.run(f'SELECT target_kind, coalesce(source_id, fact_id, entity_id), '
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
        fact_ids=list(receipt.fact_ids),
        completed_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
    ))


def store_messages(sql, session_id, messages, *, check_model=None,
                   source_metadata=None):
    existing = sql.run(f'SELECT message_id,id,message_fingerprint FROM mnemotrace.sources '
                       f'WHERE {SCOPE} AND session_id = %s AND message_id = ANY(%s)',
                       (*sql.scope, session_id, [msg.message_id for msg in messages])).fetchall()
    known = {key: (record_id, content_hash) for key, record_id, content_hash in existing}
    if check_model is not None and any(item.extract and item.message_id not in known for item in messages):
        check_model()
    sql.ensure_session(session_id)
    saved, inserted = [], []
    for item in messages:
        content_hash = item.fingerprint
        if item.message_id in known:
            record_id, stored_hash = known[item.message_id]
            if content_hash != stored_hash:
                raise MessageConflict('Message identity was already used for a different input')
        else:
            record_id = gen_id('ep')
            sql.insert('sources', dict(
                id=record_id, session_id=session_id, message_id=item.message_id,
                content=item.content, role=item.role, event_time=timestamp(item.event_time),
                ingested_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
                source_kind='dialogue', extract=item.extract,
                agent_id=item.agent_id, message_fingerprint=content_hash, metadata=source_metadata or {},
                sequence=allocate_sequence(sql, session_id), record_version=1, content_version=1,
            ))
            known[item.message_id] = (record_id, content_hash)
            inserted.append(record_id)
        saved.append(record_id)
    return tuple(saved), tuple(inserted)
