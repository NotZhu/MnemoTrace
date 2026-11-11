from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from mnemotrace.memory import Memory
from mnemotrace.store.contracts import (
    ChangeSet, CommitConditions, CommitReceipt, EmbeddingWrite, FactQuery, FactState, Lease, MessageInput,
    OperationKind, OperationSpec, ProcessingState, RecordKind, RecordRef, RecordWrite, SourceLink, SourceVersion,
    SpaceContext, StoredRecord,
)
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import (
    DatabaseConstraintError, DatabaseUnavailable, MessageConflict, SpaceNotFound,
    VersionConflict,
)
from mnemotrace.types import Conflict, Fact


@pytest.fixture
def memory(runtime, spaces, user):
    space = spaces.create(user, 'Record tests')
    from tests.embedder_fixtures import product_embedder

    return Memory(runtime, spaces.authorize(user, space.space_id), embedder=product_embedder(),
                  embedding_model='test-vectors-v1')


def message(key, content='Synthetic content', **fields):
    return MessageInput(key, 'user', content, scope='raw', **fields)


def commit(memory, operation_id, records=(), **changes):
    with memory.dao.read_snapshot() as view:
        conditions = CommitConditions(view.snapshot)
    writes = tuple(RecordWrite(StoredRecord.from_record(memory.context, item)) for item in records)
    links = tuple(SourceLink(write.ref, RecordRef(memory.context, RecordKind.SOURCE, source_id))
                  for item, write in zip(records, writes, strict=True) if isinstance(item, Fact)
                  for source_id in item.provenance)
    return memory.dao.commit(ChangeSet(conditions, operation_id, writes=writes, source_links=links, **changes))


def test_record_business_roundtrip_and_selective_updates(memory, cluster):
    accepted = memory.store_messages('chat', (message('m', agent_id='agent-1'),))
    first = memory.add_fact('Synthetic person', 'lives_in', 'Beijing', valid_at=1, object_kind='entity', text='Synthetic person lives in Beijing')
    current = memory.add_fact('Synthetic person', 'lives_in', 'Shanghai', valid_at=2,
                              cardinality='single', object_kind='entity', text='Synthetic person lives in Shanghai')
    unrelated = memory.add_fact('Someone else', 'likes', 'tea', text='Someone else likes tea')
    memory.dao._database.close()

    with Database(cluster.runtime) as reopened:
        fresh = Memory(reopened, memory.context)
        assert fresh.get_episode(accepted.source_ids[0]).content == 'Synthetic content'
        assert [fact.id for fact in fresh.history('Synthetic person', 'lives_in')] == [first.id, current.id]
        assert fresh.get_fact(current.id).supersedes == first.id
        with fresh.dao.read_snapshot() as view:
            edge = view.relations(first.id)[0]
            assert view.get(RecordKind.SOURCE, accepted.source_ids[0]).agent_id == 'agent-1'
            untouched = view.get(RecordKind.FACT, unrelated.id)
            transaction_time = view.at.timestamp()
        assert fresh.facts(FactQuery(subject='Synthetic person', valid_at=1))[0].id == first.id
        assert fresh.facts(FactQuery(subject='Synthetic person', valid_at=1, known_at=1)) == ()
        assert fresh.facts(FactQuery(subject='Synthetic person', valid_at=1, known_at=transaction_time)) == ()
        edited = fresh.update_fact(first.id, sensitive=True)
        assert edited.expired_at and edited.source == 'user'
        assert [fact.id for fact in fresh.facts(FactQuery(state=FactState.HISTORY))] == [first.id]
        with fresh.dao.read_snapshot() as view:
            assert view.relations(first.id)[0] == edge
            assert view.get(RecordKind.FACT, unrelated.id) == untouched
        with reopened.space_transaction(memory.context.user_id, memory.context.space_id,
                                        read_only=True) as (conn, _):
            assert conn.execute('SELECT count(*) FROM mnemotrace.embeddings WHERE space_id=%s AND fact_id IS NOT NULL',
                                (memory.context.space_id,)).fetchone() == (3,)
        page = fresh.facts(FactQuery(limit=1))
        tail = fresh.facts(FactQuery(after_id=page[-1].id))
        assert {fact.id for fact in page + tail} == {current.id, unrelated.id}


def test_concurrent_message_reuse_conflict_and_batch_rollback(memory, cluster):
    with Database(cluster.runtime) as second_db, ThreadPoolExecutor(max_workers=2) as workers:
        second = Memory(second_db, memory.context)
        batch = (message('exact', event_time=1),)
        first = workers.submit(memory.store_messages, 'chat', batch)
        other = workers.submit(second.store_messages, 'chat', batch)
        left, right = first.result(), other.result()
    assert left.source_ids == right.source_ids
    assert left.core_revision == right.core_revision == 1
    assert sorted([left.affected_count, right.affected_count]) == [0, 1]
    with pytest.raises(MessageConflict):
        memory.store_messages('chat', (message('new'), message('exact', 'Different content')))
    assert [source.message_id for source in memory.list_sources(session_id='chat')] == ['exact']
    assert memory.store_messages('other', (message('exact'),)).core_revision == 2
    assert memory.store_messages('chat', (message('another'),)).core_revision == 3
    assert sorted(ep.sequence for ep in memory.list_sources(session_id='chat')) == [0, 1]


def test_read_view_is_consistent_and_stale_commit_rejected(memory, cluster):
    with memory.dao.read_snapshot('chat') as view:
        assert view.facts() == ()
        with Database(cluster.runtime) as writer:
            Memory(writer, memory.context).remember_raw('New input', 'chat')
        assert view.list_sources() == ()
        assert view.counts()[RecordKind.SOURCE] == 0
        snapshot = view.snapshot
    with pytest.raises(RuntimeError, match='transaction has ended'):
        view.list_sources()
    with pytest.raises(VersionConflict):
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), 'stale'))
    assert memory.dao.receipt('stale') is None


def test_commit_failure_rolls_back_records_and_projection(memory):
    first = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, id='a-first', text='Synthetic person likes tea')
    second = Fact('Synthetic person', 'likes', 'coffee', user_id=memory.context.user_id,
                  provenance=['missing-source'], id='z-invalid', text='Synthetic person likes coffee')
    with pytest.raises(DatabaseConstraintError):
        commit(memory, 'broken', (first, second))
    assert memory.facts() == ()
    assert memory.entity('Synthetic person') is None
    assert memory.dao.receipt('broken') is None
    assert memory.store_messages('chat', (message('m'),)).core_revision == 1


@pytest.mark.parametrize('kind', ['message', 'manual'])
def test_response_lost_after_commit_is_recovered_by_receipt(memory, monkeypatch, kind):
    original = memory.dao._transaction
    dropped = False

    @contextmanager
    def interrupt_after_commit(**kwargs):
        nonlocal dropped
        with original(**kwargs) as sql:
            yield sql
        if not dropped and not kwargs.get('read_only', False):
            dropped = True
            raise DatabaseUnavailable('Synthetic connection loss after commit')

    monkeypatch.setattr(memory.dao, '_transaction', interrupt_after_commit)
    if kind == 'message':
        receipt = memory.store_messages('chat', (message('m'),))
        assert receipt.source_ids == (memory.list_sources()[0].id,)
    else:
        fact = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, source='user', text='Synthetic person likes tea')
        receipt = commit(memory, 'manual-op', (fact,))
        assert receipt.fact_ids == (memory.facts()[0].id,)
    assert dropped and receipt.core_revision == 1
    assert memory.dao.receipt(receipt.operation_id) == receipt


def test_space_isolation_and_unauthorized_context(memory, runtime, accounts, spaces):
    receipt = memory.store_messages('chat', (message('m'),))
    sibling = Memory(runtime, SpaceContext(
        memory.context.user_id, spaces.create(memory.context.user_id, 'Sibling').space_id))
    assert sibling.get_episode(receipt.source_ids[0]) is None
    assert sibling.dao.receipt(receipt.operation_id) is None
    outsider = accounts.create_user()
    try:
        other = Memory(runtime, SpaceContext(outsider, memory.context.space_id))
        with pytest.raises(SpaceNotFound):
            other.store_messages('chat', (message('m'),))
        with pytest.raises(SpaceNotFound):
            other.dao.receipt(receipt.operation_id)
    finally:
        accounts.delete_user(outsider)


def test_mixed_erasure_is_atomic_and_supported_erasure_preserves_sources(memory):
    a = memory.remember_raw('Synthetic source A', 'chat')
    b = memory.remember_raw('Synthetic source B', 'chat')
    who = memory.context.user_id
    fact = Fact('Synthetic person', 'likes', 'tea', user_id=who, provenance=[a.id, b.id], text='Synthetic person likes tea')
    task = OperationSpec('digest-a', OperationKind.DIGEST, 'chat', (SourceVersion(a.id, 1),))
    commit(memory, 'derived', (fact,), operations=(task,))
    assert memory.dao.operation(task.operation_id).stored_raw
    source = RecordRef(memory.context, RecordKind.SOURCE, a.id)
    fact_ref = RecordRef(memory.context, RecordKind.FACT, fact.id)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    with pytest.raises(ValueError, match='only supports'):
        ChangeSet(CommitConditions(snapshot), 'unsupported', erasures=(fact_ref, source))
    with pytest.raises(ValueError, match='only supports'):
        memory.erase(fact_ref, source)
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == snapshot.core_revision
    assert memory.dao.receipt('unsupported') is None
    assert memory.dao.receipt('derived') is not None
    assert memory.get_episode(a.id) is not None
    assert memory.get_fact(fact.id) is not None
    assert memory.erase(fact_ref).affected_count == 1
    assert memory.dao.operation(task.operation_id).stored_raw
    assert memory.get_episode(a.id) is not None
    assert memory.get_episode(b.id) is not None
    assert memory.facts() == ()


def test_stale_vector_rejected_and_previous_embedding_removed(memory):
    fact = memory.add_fact('Synthetic person', 'lives_in', 'Beijing', text='Synthetic person lives in Beijing')
    assert memory.update_fact(fact.id, object='Shanghai', subject=fact.subject, predicate=fact.predicate, text=' '.join((fact.subject, fact.predicate.replace('_', ' '), 'Shanghai'))).source == 'user'
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
        record = view.get(RecordKind.FACT, fact.id)
        assert record.content_version == 2
    obsolete = EmbeddingWrite(record.ref, 1, 'test-model', (1.0, 2.0))
    with pytest.raises(VersionConflict):
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), 'stale-vector', embeddings=(obsolete,)))
    with memory.dao._database.space_transaction(memory.context.user_id, memory.context.space_id,
                                                read_only=True) as (conn, _):
        assert conn.execute('SELECT content_version FROM mnemotrace.embeddings WHERE fact_id=%s',
                            (fact.id,)).fetchall() == [(2,)]


def test_pending_conflict_resolution_and_default_export(memory):
    a = memory.add_fact('Synthetic person', 'likes', 'tea', sensitive=False, text='Synthetic person likes tea')
    b = memory.add_fact('Synthetic person', 'likes', 'coffee', sensitive=True, text='Synthetic person likes coffee')
    memory.remember_raw('Synthetic private source', 'chat')
    conflict = Conflict(a.id, b.id, user_id=memory.context.user_id)
    commit(memory, 'conflict', (conflict,))
    assert [item.id for item in memory.conflicts()] == [conflict.id]
    assert memory.resolve_conflict(conflict.id, keep='newer')
    assert memory.facts()[0].supersedes == a.id
    assert memory.conflicts() == ()
    with memory.export_records() as records:
        assert [record.ref.record_id for record in records] == [a.id]
    with memory.export_records(include_sensitive=True) as records:
        assert {record.ref.kind for record in records} >= {RecordKind.FACT, RecordKind.SOURCE}


def test_stale_conflict_cannot_invalidate_current_fact(memory):
    current = memory.add_fact('Synthetic person', 'likes', 'tea', text='Synthetic person likes tea')
    old = memory.add_fact('Synthetic person', 'lives_in', 'Beijing', valid_at=1, text='Synthetic person lives in Beijing')
    conflict = Conflict(current.id, old.id, user_id=memory.context.user_id)
    commit(memory, 'stale-conflict', (conflict,))
    assert [item.id for item in memory.conflicts()] == [conflict.id]
    replacement = memory.add_fact('Synthetic person', 'lives_in', 'Shanghai', valid_at=2, cardinality='single', text='Synthetic person lives in Shanghai')
    assert memory.conflicts() == ()
    with pytest.raises(VersionConflict):
        memory.resolve_conflict(conflict.id, keep='newer')
    assert {fact.id for fact in memory.facts()} == {current.id, replacement.id}


@pytest.mark.parametrize('claim', [
    ('x' * 513, 'likes', 'tea'),
    ('Synthetic person', 'x' * 513, 'tea'),
    ('Synthetic person', 'likes', 'x' * 513),
    ('Synthetic person', 'likes', '\u0130' * 257),
    ('Synthetic person', 'likes', 'multi\nline'),
    ('Synthetic person', 'likes', 'tab\tvalue'),
])
def test_fact_persists_without_an_unrepresentable_graph_edge(memory, claim):
    fact = memory.add_fact(*claim, text=' '.join(claim), object_kind="entity")
    stored = memory.get_fact(fact.id)
    assert (stored.subject, stored.predicate, stored.object) == claim
    with memory.dao.read_snapshot() as view:
        assert view.relations(fact.id) == ()


def test_fact_edit_reconciles_graph_when_projection_becomes_possible(memory):
    fact = memory.add_fact('Synthetic person', 'likes', 'x' * 512, object_kind="entity", text=' '.join(('Synthetic person', 'likes', 'x' * 512)))
    with memory.dao.read_snapshot() as view:
        assert len(view.relations(fact.id)) == 1
    memory.update_fact(fact.id, object='x' * 513, object_kind="entity", subject=fact.subject, predicate=fact.predicate, text=' '.join((fact.subject, fact.predicate.replace('_', ' '), 'x' * 513)))
    with memory.dao.read_snapshot() as view:
        assert view.relations(fact.id) == ()
    memory.update_fact(fact.id, object='tea', object_kind="entity", subject=fact.subject, predicate=fact.predicate, text=' '.join((fact.subject, fact.predicate.replace('_', ' '), 'tea')))
    with memory.dao.read_snapshot() as view:
        assert len(view.relations(fact.id)) == 1


def test_raw_ingest_never_calls_embedder(memory, runtime):
    class UnavailableEmbedder:
        dim = 2

        def embed(self, text):
            raise RuntimeError('Synthetic embedder unavailable')

    offline = Memory(runtime, memory.context, embedder=UnavailableEmbedder(), embedding_model='fake')
    assert offline.store_messages('chat', (message('m'),)).source_ids
    assert offline.remember_raw('Synthetic raw input', 'chat').id


def test_clear_slot_checks_count_and_preserves_manual_assertions(memory):
    extracted = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, text='Synthetic person likes tea')
    commit(memory, 'seed-extracted', (extracted,))
    manual = memory.add_fact('Synthetic person', 'likes', 'coffee', text='Synthetic person likes coffee')
    with pytest.raises(VersionConflict):
        memory.clear_slot('Synthetic person', 'likes', expect_count=2)
    assert memory.clear_slot('Synthetic person', 'likes', expect_count=1).affected_count == 1
    assert tuple(fact.id for fact in memory.facts()) == (manual.id,)


@pytest.mark.parametrize('business_change', [True, False])
def test_task_commit_checks_lease_and_its_persisted_source_references(memory, runtime, business_change):
    source = memory.remember_raw('Synthetic task input', 'chat')
    other = memory.remember_raw('Another source in the same session', 'chat')
    queued = OperationSpec('task', OperationKind.DIGEST, 'chat', (SourceVersion(source.id, 1),))
    commit(memory, 'queue', operations=(queued,))
    with runtime.space_transaction(memory.context.user_id, memory.context.space_id) as (conn, _):
        conn.execute("""UPDATE mnemotrace.operations SET state='running', phase='generate',
            extraction_config='test-model', lease_token='lease', lease_until=now() + interval '1 minute'
            WHERE user_id=%s AND space_id=%s AND operation_id='task'""",
                     (memory.context.user_id, memory.context.space_id))
    with memory.dao.read_snapshot('chat') as view:
        snapshot = view.snapshot
    fact = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, text='Synthetic person likes tea')
    write = RecordWrite(StoredRecord.from_record(memory.context, fact))
    writes = (write,) if business_change else ()
    receipt = CommitReceipt('task', snapshot.core_revision + int(business_change),
                            (fact.id,) if business_change else ())

    def complete(source_id, token, result=receipt):
        conditions = CommitConditions(snapshot, (SourceVersion(source_id, 1),),
                                      Lease('task', token), 'test-model')
        return memory.dao.commit(ChangeSet(conditions, 'task', writes=writes, receipt=result))

    with pytest.raises(VersionConflict):
        complete(source.id, 'old-lease')
    with pytest.raises(VersionConflict):
        complete(other.id, 'lease')
    wrong_revision = snapshot.core_revision + int(not business_change)
    with pytest.raises(VersionConflict):
        complete(source.id, 'lease', CommitReceipt('task', wrong_revision))
    assert memory.dao.operation('task').state is ProcessingState.RUNNING
    assert memory.dao.receipt('task') is None
    assert memory.get_fact(fact.id) is None
    assert complete(source.id, 'lease') == receipt
    assert memory.dao.operation('task').state is ProcessingState.COMPLETED
    assert memory.dao.receipt('task') == receipt
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == receipt.core_revision
