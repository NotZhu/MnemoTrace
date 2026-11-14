"""空间作用域 PostgreSQL 记录事务和持久化提交结果。"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from uuid import uuid4

from ..contracts import CommitReceipt, ReadSnapshot, RecordRef, SpaceContext
from .database import identifier
from .erasures import clear_space, erase
from .errors import (
    AccountNotFound, CommitOutcomeUnknown, DatabaseConstraintError, DatabaseUnavailable,
    SpaceNotFound, VersionConflict,
)
from .projection import GraphProjection
from .reads import ReadView
from .records import ENTITY, ENTITY_TYPE, FACT, RELATION, RELATION_TYPE, SOURCE, SCOPE, ScopedSQL, domain
from .writes import check_lease, check_version, commit_task, enqueue
from .writes import save_embedding, write


@dataclass(frozen=True)
class Applied:
    changed: bool
    fact_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    count: int = 0


def new_operation_id():
    """生成一个新的操作 ID。"""
    return 'op_' + uuid4().hex


def _space_row(sql):
    """获取当前空间的核心修订号、空间生成号、设置和事务时间戳。"""
    return sql.run(f'SELECT core_revision,space_generation,settings,transaction_timestamp() '
                   f'FROM mnemotrace.memory_spaces WHERE {SCOPE}', sql.scope).fetchone()


def _receipt(sql, operation_id):
    """获取指定操作 ID 的提交收据。

    Args:
        sql: ScopedSQL 对象，用于执行数据库操作。
        operation_id: 操作 ID。
    """
    # 查询 record_commit_receipts 表，获取指定操作 ID 的提交收据。
    row = sql.run(f'SELECT core_revision,fact_ids,source_ids,affected_count '
                  f'FROM mnemotrace.record_commit_receipts WHERE {SCOPE} AND operation_id = %s',
                  (*sql.scope, operation_id)).fetchone()
    if row is not None:
        revision, facts, sources, count = row
        return CommitReceipt(operation_id, revision, tuple(facts), tuple(sources), count)
    # 如果在 record_commit_receipts 表中未找到指定操作 ID 的提交收据，则查询 operation_receipts 表。
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

    graph = GraphProjection(sql)
    fact_ids = {item.record_id for item in changes.erasures if item.kind == FACT}
    erased = erase(sql, graph, changes.erasures)
    changed = bool(erased)
    rank = {kind: index for index, kind in enumerate((SOURCE, ENTITY_TYPE, RELATION_TYPE, ENTITY, FACT, RELATION))}
    for item in sorted(changes.writes, key=lambda item: (rank[item.ref.kind], item.ref.record_id)):
        written, profile_changed = write(sql, item, graph)
        changed |= written
        if profile_changed:
            fact_ids.add(item.ref.record_id)
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
    """PostgreSQL 数据库访问对象。用于执行数据库操作。"""

    def __init__(self, database, context: SpaceContext):
        """初始化 PostgresDAO。

        Args:
            database: 数据库连接。
            context: 空间上下文。
        """
        if not isinstance(context, SpaceContext):
            raise ValueError('Expected a trusted SpaceContext')
        self._database = database
        self._context = context

    @property
    def context(self):
        return self._context

    @contextmanager
    def _transaction(self, *, read_only=False):
        """获取数据库事务上下文管理器。

        Args:
            read_only: 是否为只读事务。
        """
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
        """执行变更操作并返回提交收据。

        Args:
            operation_id: 操作 ID。
            apply: 应用变更的函数。
            segment_of: 可选的分段操作 ID。
        """
        identifier(operation_id)
        entered = False
        try:
            with self._transaction() as sql:
                entered = True
                existing = _receipt(sql, operation_id)
                # 如果已经存在提交收据，则直接返回该收据，避免重复提交。
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
        from .tasks import Tasks

        identifier(operation_id)
        return Tasks(self).operation(operation_id)
