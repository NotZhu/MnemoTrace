"""Durable operation scheduling and model checkpoints within a runtime space."""
from dataclasses import asdict, replace
from datetime import timedelta
import json
from uuid import uuid4

from ...consolidate.pipeline import content_hash, source
from ..contracts import CommitConditions, CommitReceipt, EmbeddingWrite, ExtractionProduct, Lease
from ..contracts import OperationKind, OperationResult, RecordRef
from ..contracts import OperationSpec, OperationStats, ProcessingPhase, ProcessingState, SourceVersion, StoredRecord
from .dao import Applied, _apply_changes, _space_row, new_operation_id
from .errors import DataCorruption, VersionConflict
from .lexical import project_text
from .records import SCOPE, SOURCE, domain
from .writes import allocate_sequence, append_input, commit_task, enqueue, queue_source_vector, save_embedding, store_messages

IDLE_SECONDS = 60
from ...llm.failures import ATTEMPT_LIMIT, RETRY_DELAYS

LEASE_SECONDS = 60
RENEW_SECONDS = 20
ACTIVE = ('pending', 'retry_wait', 'running')
MODEL_OPERATIONS = ('extract', 'digest', 'profile')
RUNNABLE = (*MODEL_OPERATIONS, 'reembed')


def _attempt_counter(row):
    return 'embed_attempts' if row['phase'] in ('embed', 'commit') else 'generation_attempts'


def _result(row, auxiliary=()):
    return OperationResult(
        row['operation_id'], OperationKind(row['kind']), ProcessingState(row['state']),
        ProcessingPhase(row['phase']), row['stored_raw'], row['extraction_saved'],
        row['core_committed'], row['attempts'],
        row['next_attempt_at'].timestamp() if row['next_attempt_at'] else None,
        row['error_code'], auxiliary,
    )


def _operation(sql, operation_id, *, lock=False):
    rows = sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} AND operation_id = %s'
                    + (' FOR UPDATE SKIP LOCKED' if lock else ''), (*sql.scope, operation_id))
    return rows[0] if rows else None


def _sources(sql, operation_id, *, context=False):
    rows = sql.run(f'SELECT DISTINCT source_id,content_version,input_order '
                   f'FROM mnemotrace.operation_sources WHERE {SCOPE} AND operation_id = %s '
                   'AND (NOT is_context OR %s) ORDER BY input_order,source_id',
                   (*sql.scope, operation_id, context)).fetchall()
    return tuple(dict.fromkeys(SourceVersion(source_id, version) for source_id, version, _ in rows))


def _spec(operation_id, kind, session_id, records):
    return OperationSpec(operation_id, kind, session_id, tuple(
        SourceVersion(record.ref.record_id, record.content_version) for record in records))


class Tasks:
    def __init__(self, dao):
        self.dao = dao

    def _results(self, sql, rows):
        if not rows:
            return ()
        children = {row['operation_id']: [] for row in rows}
        linked = sql.run(f'SELECT parent_id,operation_id FROM mnemotrace.operations WHERE {SCOPE} '
                         'AND parent_id = ANY(%s) ORDER BY parent_id,sequence,operation_id',
                         (*sql.scope, list(children))).fetchall()
        for parent_id, child_id in linked:
            children[parent_id].append(child_id)
        return tuple(_result(row, tuple(children[row['operation_id']])) for row in rows)

    def operation(self, operation_id):
        with self.dao._transaction(read_only=True) as sql:
            row = _operation(sql, operation_id)
            return self._results(sql, [row])[0] if row else None

    def list_operations(self, session_id, after_id, limit):
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} '
                            'AND (%s::text IS NULL OR session_id = %s) '
                            'AND (%s::text IS NULL OR operation_id > %s) '
                            'ORDER BY operation_id LIMIT %s',
                            (*sql.scope, session_id, session_id, after_id, after_id, limit))
            return self._results(sql, rows)

    def source_vector_operation(self, source_id, embedding):
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.rows("""SELECT o.* FROM mnemotrace.operation_targets t
                JOIN mnemotrace.operations o USING (user_id,space_id,operation_id)
                JOIN mnemotrace.sources source ON (source.user_id,source.space_id,source.id,source.content_version) =
                    (t.user_id,t.space_id,t.source_id,t.content_version)
                JOIN mnemotrace.memory_spaces s ON (s.user_id,s.space_id) = (o.user_id,o.space_id)
                JOIN mnemotrace.sessions session ON (session.user_id,session.space_id,session.session_id) =
                    (o.user_id,o.space_id,o.session_id)
                WHERE t.user_id = %s AND t.space_id = %s AND t.source_id = %s
                AND o.embedding_model = %s AND o.embedding_dimension = %s
                AND o.space_generation = s.space_generation
                ORDER BY o.operation_id LIMIT 1""", (*sql.scope, source_id, *embedding))
            return _result(rows[0]) if rows else None

    def freeze(self, operation_ids, session_id, include_digests):
        with self.dao._transaction() as sql:
            if operation_ids is None:
                rows = sql.run(f'SELECT operation_id FROM mnemotrace.operations WHERE {SCOPE} '
                               "AND state <> 'cancelled' "
                               'AND kind = ANY(%s) AND (session_id = %s OR %s) '
                               "AND (kind <> 'profile' OR operation_id = ("
                               f"SELECT operation_id FROM mnemotrace.operations WHERE {SCOPE} AND kind = 'profile' "
                               "ORDER BY (state IN ('pending','running','retry_wait')) DESC,ready_at DESC,operation_id DESC LIMIT 1)) "
                               'ORDER BY sequence,operation_id',
                               (*sql.scope, list(MODEL_OPERATIONS if include_digests else ('extract', 'profile')),
                                session_id, session_id is None, *sql.scope)).fetchall()
                operation_ids = [row[0] for row in rows]
            self._freeze(sql, operation_ids)
            return operation_ids

    def _freeze(self, sql, operation_ids):
        if operation_ids:
            sql.run(f'UPDATE mnemotrace.operations SET frozen = true, ready_at = clock_timestamp() '
                    f'WHERE {SCOPE} AND operation_id = ANY(%s) AND phase = %s AND state = %s',
                    (*sql.scope, list(operation_ids), 'queued', 'pending'))

    def execution_order(self, operation_ids):
        with self.dao._transaction(read_only=True) as sql:
            rows = sql.run("""SELECT DISTINCT o.operation_id, o.sequence FROM mnemotrace.operations o
                JOIN mnemotrace.operations target ON (o.user_id,o.space_id) = (target.user_id,target.space_id)
                WHERE target.user_id = %s AND target.space_id = %s AND target.operation_id = ANY(%s)
                AND (o.operation_id = target.operation_id OR (target.kind = 'extract' AND o.kind = 'extract'
                    AND o.session_id = target.session_id AND o.sequence < target.sequence AND o.state = ANY(%s)))
                ORDER BY o.sequence, o.operation_id""",
                (*sql.scope, list(operation_ids), list(ACTIVE))).fetchall()
            return tuple(row[0] for row in rows)

    def accept(self, session_id, messages, check_model, fit, *, immediate=False, embedding=None, source_metadata=None):
        def apply(sql, revision, generation, settings):
            ids, fresh = store_messages(sql, session_id, messages, check_model=check_model,
                                        generation=generation, embedding=embedding, source_metadata=source_metadata)
            records = (sql.get(SOURCE, source_id) for source_id in fresh)
            new_records = [record for record in records if domain(record).extract]
            if new_records:
                self._queue_messages(sql, session_id, generation, settings, new_records, fit)
            if immediate:
                self._freeze(sql, [operation for _, operation in self._associated(sql, ids)])
            return Applied(bool(fresh), source_ids=ids, count=len(fresh))

        receipt = self.dao._mutate(new_operation_id(), apply)
        with self.dao._transaction(read_only=True) as sql:
            return receipt, self._associated(sql, receipt.source_ids)

    def accept_source(self, record, check_model, *, embedding=None,
                      kind=OperationKind.EXTRACT):
        episode = domain(record)
        operation_id = new_operation_id() if kind is not None else None
        acceptance_id = 'accept_' + operation_id if operation_id is not None else new_operation_id()

        def apply(sql, revision, generation, settings):
            if kind is not None:
                check_model()
            sql.ensure_session(episode.session_id)
            episode.ingested_at = sql.run('SELECT clock_timestamp()').fetchone()[0].timestamp()
            episode.sequence = allocate_sequence(sql, episode.session_id)
            stored = replace(StoredRecord.from_record(sql.context, episode), agent_id=record.agent_id)
            sql.write_record(stored, insert=True)
            project_text(sql, SOURCE, episode.id, stored.content_version, {'content': episode.content})
            if embedding is not None:
                queue_source_vector(sql, episode.id, episode.session_id, stored.content_version, generation, embedding)
            if kind is not None:
                enqueue(sql, _spec(operation_id, kind, episode.session_id, (stored,)), generation,
                        ready_at=sql.run('SELECT clock_timestamp()').fetchone()[0])
            return Applied(True, source_ids=(episode.id,), count=1)

        return self.dao._mutate(acceptance_id, apply), operation_id

    def _associated(self, sql, source_ids):
        return sql.run("""SELECT DISTINCT source_id, operation_id
            FROM mnemotrace.operation_sources r JOIN mnemotrace.operations o
            USING (user_id,space_id,operation_id)
            WHERE r.user_id = %s AND r.space_id = %s AND NOT r.is_context
            AND o.kind = 'extract' AND source_id = ANY(%s)
            ORDER BY source_id, operation_id""", (*sql.scope, list(source_ids))).fetchall()

    def _queue_messages(self, sql, session, generation, settings, records, fit):
        now = sql.run('SELECT clock_timestamp()').fetchone()[0]
        # Only the latest unclaimed extraction window can grow without reordering sources.
        row = next(iter(sql.rows(f'SELECT * FROM mnemotrace.operations WHERE {SCOPE} '
                                 "AND session_id = %s AND kind = 'extract' ORDER BY sequence DESC LIMIT 1",
                                 (*sql.scope, session))), None)
        open_id = row['operation_id'] if (row and row['state'] == 'pending' and not row['frozen']
                                        and row['ready_at'] and row['ready_at'] > now) else None
        current = [sql.get(SOURCE, source.source_id) for source in _sources(sql, open_id)] if open_id else []
        for record in records:
            fits, ready = fit(current, record, settings)
            if open_id and not fits:
                sql.update('operations', {'ready_at': now}, 'operation_id = %s', (open_id,))
                open_id, current = None, []
                _, ready = fit(current, record, settings)
            due = now if ready else now + timedelta(seconds=IDLE_SECONDS)
            if open_id:
                append_input(sql, open_id, session,
                             SourceVersion(record.ref.record_id, record.content_version), len(current))
                sql.update('operations', {'ready_at': due}, 'operation_id = %s', (open_id,))
            else:
                open_id = new_operation_id()
                enqueue(sql, _spec(open_id, OperationKind.EXTRACT, session, (record,)),
                        generation, ready_at=due)
            current.append(record)
            if ready:
                open_id, current = None, []

    def claim(self, operation_id, *, force=False, embedding=None):
        with self.dao._transaction() as sql:
            row = _operation(sql, operation_id)
            if not row or row['kind'] not in RUNNABLE or row['state'] not in ACTIVE:
                return None
            if row['kind'] == 'reembed' and (row['embedding_model'], row['embedding_dimension']) != embedding:
                return None
            session = row['session_id']
            sql.run(f'SELECT session_id FROM mnemotrace.sessions WHERE {SCOPE} AND session_id = %s FOR UPDATE',
                    (*sql.scope, session))
            row = _operation(sql, operation_id, lock=True)
            if not row or not self._current(sql, row):
                return None
            now = sql.run('SELECT clock_timestamp()').fetchone()[0]
            state = row['state']
            due = (row['lease_until'] if state == 'running' else row['next_attempt_at']
                   if state == 'retry_wait' else row['ready_at'] if row['phase'] == 'queued' and not force else now)
            if due is None or due > now:
                return None
            if row['kind'] == 'extract' and sql.run(
                    f'SELECT 1 FROM mnemotrace.operations WHERE {SCOPE} AND session_id = %s '
                    "AND kind = 'extract' AND sequence < %s AND state = ANY(%s) LIMIT 1",
                    (*sql.scope, session, row['sequence'], list(ACTIVE))).fetchone():
                return None
            phase = 'embed' if row['kind'] == 'reembed' else 'generate' if row['phase'] == 'queued' else row['phase']
            counter = _attempt_counter({**row, 'phase': phase})
            if row[counter] >= ATTEMPT_LIMIT:
                self._release(sql, row, 'failed', 'attempts_exhausted')
                return None
            token = uuid4().hex
            sql.update('operations', dict(state='running', frozen=True, lease_token=token,
                       lease_until=now + timedelta(seconds=LEASE_SECONDS), next_attempt_at=None,
                       phase=phase,
                       attempts=row['attempts'] + 1, **{counter: row[counter] + 1}),
                       'operation_id = %s', (operation_id,))
            return Lease(operation_id, token)

    def _current(self, sql, row):
        return row['space_generation'] == _space_row(sql)[1]

    def _leased(self, sql, lease):
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

    def renew(self, lease):
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            sql.update('operations', {'lease_until': now + timedelta(seconds=LEASE_SECONDS)},
                       'operation_id = %s', (row['operation_id'],))

    def _release(self, sql, row, state, code=None, *, due=None, **values):
        sql.update('operations', dict(state=state, lease_token=None, lease_until=None,
                   error_code=code, next_attempt_at=due, **values),
                   'operation_id = %s', (row['operation_id'],))

    def fail(self, lease, code, retryable):
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
        with self.dao._transaction() as sql:
            row = _operation(sql, operation_id)
            if row and row['state'] in ('failed', 'retry_wait') and self._current(sql, row):
                if row['kind'] == 'profile' and sql.run(
                    f"SELECT 1 FROM mnemotrace.operations WHERE {SCOPE} AND kind = 'profile' "
                    "AND state = ANY(%s) AND operation_id <> %s",
                    (*sql.scope, list(ACTIVE), operation_id),
                ).fetchone():
                    raise VersionConflict('Another profile refresh is active')
                values = ({'ready_at': sql.run('SELECT clock_timestamp()').fetchone()[0]}
                          if row['kind'] == 'profile' else {})
                self._release(sql, row, 'pending', generation_attempts=0, embed_attempts=0, **values)
        return self.operation(operation_id)

    def load(self, view, lease):
        sql = view._sql
        row = _operation(sql, lease.operation_id)
        if not row or row['state'] != 'running' or row['lease_token'] != lease.token:
            raise VersionConflict('Task lease changed')
        if row['space_generation'] != view.snapshot.space_generation:
            raise VersionConflict('Task generation changed')
        snapshot = replace(view.snapshot, session_id=row['session_id'])
        if row['kind'] == 'reembed':
            return snapshot, row, (self.source_vector_input(view, lease.operation_id),)
        refs = _sources(sql, lease.operation_id)
        if row['kind'] == 'digest' and len(refs) != 1:
            raise DataCorruption('Digest operation requires exactly one source')
        records = {record.ref.record_id: record for record in view.sources(tuple(s.source_id for s in refs))}
        if not refs or any(s.source_id not in records or records[s.source_id].content_version != s.content_version
                           for s in refs):
            raise DataCorruption('Invalid operation source references')
        return snapshot, row, tuple(records[s.source_id] for s in refs)

    def source_vector_input(self, view, operation_id):
        rows = view._sql.run(f'SELECT source_id,content_version FROM mnemotrace.operation_targets '
                             f'WHERE {SCOPE} AND operation_id = %s',
                             (*view._sql.scope, operation_id)).fetchall()
        if len(rows) != 1 or rows[0][0] is None:
            raise DataCorruption('Source embedding requires exactly one source target')
        source_id, version = rows[0]
        record = view.get(SOURCE, source_id)
        if record is None or record.content_version != version:
            raise VersionConflict('Embedding source changed')
        return record

    def commit_source_vector(self, lease, record, model, vector):
        with self.dao._transaction() as sql:
            row, _ = self._leased(sql, lease)
            if (row['kind'], row['embedding_model'], row['embedding_dimension']) != ('reembed', model, len(vector)):
                raise DataCorruption('Embedding task model or dimension differs from its output')
            target = sql.run(f'SELECT source_id,content_version FROM mnemotrace.operation_targets '
                             f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, lease.operation_id)).fetchall()
            if target != [(record.ref.record_id, record.content_version)]:
                raise VersionConflict('Embedding task target changed')
            ref = RecordRef(sql.context, SOURCE, record.ref.record_id)
            save_embedding(sql, EmbeddingWrite(ref, record.content_version, model, vector))
            commit_task(sql, lease, CommitReceipt(lease.operation_id, _space_row(sql)[0]))

    def product(self, view, snapshot, lease, row):
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
            if not set(versions) <= set(_sources(sql, lease.operation_id, context=True)):
                raise ValueError('Checkpoint references differ from operation')
            return ExtractionProduct(CommitConditions(snapshot, versions, lease, data['extraction_config']),
                                     json.dumps(data['input']), json.dumps(data['result']),
                                     OperationStats(**data['stats']), data['digest_max_chars'],
                                     data['context_revision'], data['format_version'])
        except (TypeError, ValueError, KeyError) as exc:
            raise DataCorruption('Invalid extraction checkpoint') from exc

    def save_product(self, product, settings):
        conditions = product.conditions
        lease, snapshot = conditions.lease, conditions.snapshot
        if snapshot.context != self.dao.context:
            raise ValueError('Product belongs to another space')
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            revision, _, current_settings, _ = _space_row(sql)
            if (row['phase'] != 'generate' or current_settings != json.loads(json.dumps(asdict(settings)))
                    or product.context_revision is not None and revision != snapshot.core_revision):
                raise VersionConflict('Extraction context changed')
            data, result = json.loads(product.input_json), json.loads(product.result_json)
            members = _sources(sql, lease.operation_id)
            versions = {ref.source_id: ref for ref in conditions.sources}
            if not {item['id'] for item in data['pending']} <= {ref.source_id for ref in members}:
                raise DataCorruption('Product does not belong to the frozen window')
            for item in (*data['context'], *data['pending']):
                record = sql.get(SOURCE, item['id'])
                if record is None or record.content_version != versions[item['id']].content_version:
                    raise VersionConflict('Source changed while extracting')
                episode = domain(record)
                if (episode.session_id != row['session_id'] or source(episode) != item['source']
                        or item['representation_source'] is not None
                        and content_hash(episode.digest) != item['representation_source']):
                    raise VersionConflict('Source representation changed while extracting')
            for index, item in enumerate(data['context'], start=len(members)):
                if item['id'] not in {ref.source_id for ref in members}:
                    sql.insert('operation_sources', dict(operation_id=lease.operation_id, source_id=item['id'],
                               content_version=versions[item['id']].content_version, input_start=0,
                               input_end=item['source_length'], input_order=index, role=item['role'],
                               is_context=True, representation_source=item['representation_source']),
                               suffix='ON CONFLICT DO NOTHING')
            sql.insert('operation_products', dict(
                operation_id=lease.operation_id, input=data, result=result, format_version=product.format_version,
                extraction_config=conditions.extraction_config, context_revision=product.context_revision,
                source_versions=[asdict(ref) for ref in conditions.sources], stats=asdict(product.stats),
                digest_max_chars=product.digest_max_chars, result_hash=content_hash(result), input_hash=content_hash(data)))
            self._release(sql, row, 'pending', phase='embed', embed_attempts=0, ready_at=now,
                          extraction_saved=True, extraction_config=conditions.extraction_config,
                          context_revision=product.context_revision)

    def regenerate(self, lease):
        with self.dao._transaction() as sql:
            row, now = self._leased(sql, lease)
            self._reset_checkpoint(sql, row, now)

    def digest_parts(self, view, operation_id, source_id, fingerprint):
        return view._sql.rows(f'SELECT part_id AS key,input_start AS start,input_end AS end,text '
                              f'FROM mnemotrace.operation_digest_parts WHERE {SCOPE} '
                              'AND operation_id = %s AND source_id = %s AND extraction_config = %s ORDER BY input_start',
                              (*view._sql.scope, operation_id, source_id, fingerprint))

    def save_digests(self, sql, operation_id, generation, session_id,
                       inputs, drafts, completed, minimum, fingerprint):
        for item in inputs:
            source_id = item['id']
            if source_id in drafts:
                self.save_part(sql, operation_id, item, drafts[source_id], fingerprint)
            if source_id not in completed or item['source_length'] < minimum:
                continue
            record = sql.get(SOURCE, source_id)
            if domain(record).digest:
                continue
            child_id = new_operation_id()
            enqueue(sql, _spec(child_id, OperationKind.DIGEST, session_id, (record,)), generation,
                    ready_at=sql.run('SELECT clock_timestamp()').fetchone()[0],
                    parent_id=operation_id)
            moved = sql.update('operation_digest_parts', {'operation_id': child_id},
                               'operation_id = %s AND source_id = %s', (operation_id, source_id)).rowcount
            if moved:
                # A cached draft saw the joint input even when it summarizes one source.
                sql.run(f"""INSERT INTO mnemotrace.operation_sources (
                    user_id,space_id,operation_id,source_id,content_version,input_start,input_end,
                    input_order,role,representation_source,is_context)
                    SELECT user_id,space_id,%s,source_id,content_version,input_start,input_end,
                    input_order,role,representation_source,true FROM mnemotrace.operation_sources
                    WHERE {SCOPE} AND operation_id = %s AND source_id <> %s""",
                    (child_id, *sql.scope, operation_id, source_id))

    def save_part(self, sql, operation_id, item, digest, fingerprint, replaced=()):
        if replaced:
            deleted = sql.delete('operation_digest_parts', 'operation_id = %s AND part_id = ANY(%s)',
                                 (operation_id, list(replaced))).rowcount
            if deleted != len(replaced):
                raise VersionConflict('Digest fragments changed')
        sql.insert('operation_digest_parts', dict(
            operation_id=operation_id, source_id=item['id'], part_id=uuid4().hex,
            input_start=item['ranges'][0][0], input_end=item['ranges'][-1][1], text=digest,
            extraction_config=fingerprint))

    def _reset_checkpoint(self, sql, row, now):
        sql.delete('operation_products', 'operation_id = %s', (row['operation_id'],))
        self._release(sql, row, 'pending', phase='generate', ready_at=now, extraction_saved=False,
                      extraction_config=None, context_revision=None)

    def commit(self, changes, *, continuation=None, after=None):
        """A segment has its own idempotency receipt and keeps the original job ID."""
        operation_id = changes.operation_id

        def apply(sql, revision, generation, settings):
            def checkpoint(sql):
                row = _operation(sql, operation_id)
                now = sql.run('SELECT clock_timestamp()').fetchone()[0]
                self._reset_checkpoint(sql, row, now)
                sql.update('operations', {'generation_attempts': 0, 'embed_attempts': 0},
                           'operation_id = %s', (operation_id,))

            commit = changes
            if not continuation:
                earlier = sql.run(f'SELECT fact_ids,source_ids FROM mnemotrace.record_commit_receipts '
                                  f'WHERE {SCOPE} AND segment_of = %s ORDER BY core_revision,operation_id',
                                  (*sql.scope, operation_id)).fetchall()
                receipt = changes.receipt
                if earlier:
                    facts = tuple(dict.fromkeys(item for ids, _ in earlier for item in ids))
                    receipt = replace(receipt, fact_ids=tuple(dict.fromkeys((*facts, *receipt.fact_ids))))
                    commit = replace(changes, receipt=receipt)
            applied = _apply_changes(sql, commit, revision, generation, settings,
                                     checkpoint=checkpoint if continuation else None)
            if after is not None:
                after(sql, generation)
            if not continuation:
                sql.delete('operation_products', 'operation_id = %s', (operation_id,))
                sql.delete('operation_digest_parts', 'operation_id = %s', (operation_id,))
                if earlier:
                    sources = tuple(dict.fromkeys(item for _, ids in earlier for item in ids))
                    applied = replace(applied, source_ids=tuple(dict.fromkeys((*sources, *applied.source_ids))))
            return applied

        return self.dao._mutate(continuation or operation_id, apply,
                                segment_of=operation_id if continuation else None)
