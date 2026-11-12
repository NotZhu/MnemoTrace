"""Space-scoped PostgreSQL record transactions and durable commit results."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from uuid import uuid4

from ..contracts import CommitReceipt, MessageInput, OperationKind, OperationResult, ProcessingPhase
from ..contracts import ProcessingState, ReadSnapshot, RecordRef, SpaceContext
from .database import identifier
from .erasures import clear_space, erase
from .errors import (
    AccountNotFound, CommitOutcomeUnknown, DatabaseConstraintError, DatabaseUnavailable,
    SpaceNotFound, VersionConflict,
)
from .projection import GraphProjection
from .reads import ReadView
from .records import CONFLICT, ENTITY, FACT, RELATION, RELATION_TYPE, SOURCE, SCOPE, ScopedSQL, domain
from .writes import check_lease, check_version, commit_task, enqueue, invalidate
from .writes import save_embedding, store_messages, write


@dataclass(frozen=True)
class Applied:
    changed: bool
    fact_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    count: int = 0


def new_operation_id():
    return 'op_' + uuid4().hex


def _space_row(sql):
    return sql.run(f'SELECT core_revision,space_generation,settings,transaction_timestamp() '
                   f'FROM mnemotrace.memory_spaces WHERE {SCOPE}', sql.scope).fetchone()


def _receipt(sql, operation_id):
    row = sql.run(f'SELECT core_revision,fact_ids,source_ids,affected_count '
                  f'FROM mnemotrace.record_commit_receipts WHERE {SCOPE} AND operation_id = %s',
                  (*sql.scope, operation_id)).fetchone()
    if row is not None:
        revision, facts, sources, count = row
        return CommitReceipt(operation_id, revision, tuple(facts), tuple(sources), count)
    row = sql.run(f'SELECT core_revision,fact_ids FROM mnemotrace.operation_receipts '
                  f'WHERE {SCOPE} AND operation_id = %s', (*sql.scope, operation_id)).fetchone()
    if row is not None:
        return CommitReceipt(operation_id, row[0], tuple(row[1]))
    return None


def _apply_changes(sql, changes, revision, generation, settings, *, checkpoint=None):
    conditions = changes.conditions
    snapshot = conditions.snapshot
    if snapshot.core_revision != revision or snapshot.space_generation != generation:
        raise VersionConflict('Space version changed')
    sessions = {spec.session_id for spec in changes.operations if spec.session_id is not None}
    sessions.update(domain(item.record).session_id for item in changes.writes
                    if item.ref.kind == SOURCE)
    for session_id in sorted(sessions):
        sql.ensure_session(session_id)
    if snapshot.session_id is not None:
        sessions.add(snapshot.session_id)
    if sessions:
        sql.run(f'SELECT session_id FROM mnemotrace.sessions WHERE {SCOPE} AND session_id = ANY(%s) '
                'ORDER BY session_id FOR UPDATE', (*sql.scope, sorted(sessions))).fetchall()
    if conditions.lease is not None:
        check_lease(sql, conditions)
    for source in conditions.sources:
        check_version(sql, RecordRef(sql.context, SOURCE, source.source_id), source.content_version)
    for target in conditions.targets:
        check_version(sql, target.record, target.content_version)

    graph = GraphProjection(sql)
    fact_ids = {item.record_id for item in changes.erasures if item.kind == FACT}
    erased = erase(sql, graph, changes.erasures)
    changed = bool(erased)
    if {link.fact for link in changes.source_links} - {item.ref for item in changes.writes}:
        raise DatabaseConstraintError('Source links require an explicit fact write')
    for conflict in changes.resolved_conflicts:
        removed = sql.delete('conflicts', 'id = %s', (conflict.record_id,))
        if removed.rowcount != 1:
            raise VersionConflict('Pending conflict changed')
        changed = True
    rank = {kind: index for index, kind in enumerate((SOURCE, RELATION_TYPE, ENTITY, FACT, CONFLICT, RELATION))}
    for item in sorted(changes.writes, key=lambda item: (rank[item.ref.kind], item.ref.record_id)):
        written, profile_changed = write(sql, item, graph)
        changed |= written
        if profile_changed:
            fact_ids.add(item.ref.record_id)
    for item in changes.invalidations:
        if invalidate(sql, item, graph):
            fact_ids.add(item.ref.record_id)
            changed = True
    graph.prune()
    for item in changes.embeddings:
        save_embedding(sql, item)
    if changes.settings is not None:
        new_settings = {key: list(value) if isinstance(value, tuple) else value
                        for key, value in asdict(changes.settings).items()}
        if settings != new_settings:
            sql.run(f'UPDATE mnemotrace.memory_spaces SET record_version = record_version + 1 WHERE {SCOPE}',
                    sql.scope)
            sql.update('memory_spaces', {'settings': new_settings}, 'true')
            changed = True
    for spec in changes.operations:
        enqueue(sql, spec, generation)
    if conditions.lease is not None:
        if changes.receipt is None:
            raise DatabaseConstraintError('Task completion requires a receipt')
        if checkpoint is None:
            commit_task(sql, conditions.lease, changes.receipt)
        else:
            checkpoint(sql)
    from .profile import synchronize
    synchronize(sql, fact_ids, settings_changed=changes.settings is not None)
    refs = [item.ref for item in changes.writes]
    supplied = changes.receipt
    if supplied is not None and supplied.core_revision != revision + int(changed):
        raise VersionConflict('Receipt revision differs from committed business revision')
    return Applied(changed,
                   supplied.fact_ids if supplied else tuple(ref.record_id for ref in refs if ref.kind == FACT),
                   tuple(ref.record_id for ref in refs if ref.kind == SOURCE), erased)


class PostgresDAO:
    """Record access through the runtime role, with authorization on every transaction."""

    def __init__(self, database, context: SpaceContext):
        if not isinstance(context, SpaceContext):
            raise ValueError('Expected a trusted SpaceContext')
        self._database = database
        self._context = context

    @property
    def context(self):
        return self._context

    @contextmanager
    def _transaction(self, *, read_only=False):
        with self._database.space_transaction(
            self.context.user_id, self.context.space_id, read_only=read_only,
        ) as (conn, scope):
            sql = ScopedSQL(conn, scope)
            try:
                yield sql
            finally:
                sql.close()

    @contextmanager
    def read_snapshot(self, session_id=None):
        if session_id is not None:
            identifier(session_id)
        with self._transaction(read_only=True) as sql:
            revision, generation, settings, at = _space_row(sql)
            snapshot = ReadSnapshot(self.context, revision, generation, session_id)
            yield ReadView(sql, snapshot, at, settings)

    def receipt(self, operation_id):
        identifier(operation_id)
        with self._transaction() as sql:
            return _receipt(sql, operation_id)

    def _mutate(self, operation_id, apply, *, segment_of=None):
        identifier(operation_id)
        entered = False
        try:
            with self._transaction() as sql:
                entered = True
                existing = _receipt(sql, operation_id)
                if existing is not None:
                    return existing
                revision, generation, settings, _ = _space_row(sql)
                result = apply(sql, revision, generation, settings)
                revision += int(result.changed)
                if result.changed:
                    sql.update('memory_spaces', {'core_revision': revision}, 'true')
                sql.insert('record_commit_receipts', dict(
                    operation_id=operation_id, core_revision=revision, fact_ids=list(result.fact_ids),
                    source_ids=list(result.source_ids),
                    affected_count=result.count, **({'segment_of': segment_of} if segment_of else {}),
                ))
                return CommitReceipt(operation_id, revision, result.fact_ids,
                                     result.source_ids, result.count)
        except DatabaseUnavailable as exc:
            if not entered:
                raise
            # The next writer waits for the original transaction to commit or roll back.
            try:
                receipt = self.receipt(operation_id)
            except (DatabaseUnavailable, AccountNotFound, SpaceNotFound) as recovery_error:
                raise CommitOutcomeUnknown(operation_id) from recovery_error
            if receipt is None:
                # An intervening erase may have removed a successfully committed receipt.
                raise CommitOutcomeUnknown(operation_id) from exc
            return receipt

    def commit(self, changes):
        if changes.conditions.snapshot.context != self.context:
            raise ValueError('Change set belongs to a different space')

        def apply(sql, revision, generation, settings):
            return _apply_changes(sql, changes, revision, generation, settings)

        return self._mutate(changes.operation_id, apply)

    def accept_raw_messages(self, session_id, messages):
        identifier(session_id)
        items = tuple(messages)
        if not items or any(not isinstance(item, MessageInput) or item.extract for item in items):
            raise ValueError('Expected a non-empty batch of raw MessageInput records')

        def apply(sql, revision, generation, settings):
            ids, fresh = store_messages(sql, session_id, items)
            return Applied(bool(fresh), source_ids=ids, count=len(fresh))

        return self._mutate(new_operation_id(), apply)


    def clear_space(self):
        def apply(sql, *_):
            clear_space(sql)
            return Applied(True)

        return self._mutate(new_operation_id(), apply)

    def delete_space(self):
        authorized = False
        try:
            with self._transaction() as sql:
                authorized = True
                sql.delete('memory_spaces')
        except DatabaseUnavailable:
            if authorized:
                try:
                    with self._transaction():
                        pass
                except SpaceNotFound:
                    return
            raise

    def operation(self, operation_id):
        identifier(operation_id)
        with self._transaction(read_only=True) as sql:
            row = sql.run(f'SELECT kind,state,phase,stored_raw,extraction_saved,core_committed,attempts,'
                          f'next_attempt_at,error_code FROM mnemotrace.operations WHERE {SCOPE}'
                          ' AND operation_id = %s', (*sql.scope, operation_id)).fetchone()
            if row is None:
                return None
            kind, state, phase, raw, extracted, committed, attempts, next_at, error = row
            return OperationResult(operation_id, OperationKind(kind), ProcessingState(state),
                                   ProcessingPhase(phase), raw, extracted, committed, attempts,
                                   next_at.timestamp() if next_at is not None else None, error)
