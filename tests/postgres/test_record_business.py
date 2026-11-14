from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import contextmanager

import pytest

from mnemotrace.memory import Memory
from tests.postgres.fact_fixtures import relation_writes, seed_fact, seed_note
from mnemotrace.store.contracts import (
    ChangeSet, CommitConditions, CommitReceipt, EmbeddingWrite, FactQuery, FactState, Lease, MessageInput,
    OperationKind, OperationSpec, ProcessingState, RecordKind, RecordRef, RecordWrite, SourceVersion,
    SpaceContext, StoredRecord,
)
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import (
    DatabaseConstraintError, DatabaseUnavailable, MessageConflict, SpaceNotFound,
    VersionConflict,
)
from mnemotrace.store.postgres.records import domain
from mnemotrace.types import Entity, Fact


@pytest.fixture
def memory(runtime, spaces, user):
    space = spaces.create(user, 'Record tests')
    from tests.embedder_fixtures import product_embedder

    return Memory(runtime, spaces.authorize(user, space.space_id), embedder=product_embedder(),
                  embedding_model='test-vectors-v1')


def message(key, content='Synthetic content', **fields):
    return MessageInput(key, 'user', content, extract=False, **fields)


def commit(memory, operation_id, records=(), **changes):
    with memory.dao.read_snapshot() as view:
        conditions = CommitConditions(view.snapshot)
        relations = relation_writes(view, records)
    writes = tuple(RecordWrite(StoredRecord.from_record(memory.context, item)) for item in records)
    return memory.dao.commit(ChangeSet(conditions, operation_id, writes=(*relations, *writes), **changes))


def test_record_business_roundtrip_and_selective_updates(memory, cluster):
    accepted = memory.remember_messages('chat', (message('m', agent_id='agent-1'),))
    first = seed_fact(memory, 'Synthetic person', 'lives_in', 'Beijing', valid_at=1, invalid_at=2, expired_at=100, linked=True, text='Synthetic person lives in Beijing')
    current = seed_fact(memory, 'Synthetic person', 'lives_in', 'Shanghai', valid_at=2,
                              supersedes=[first.id], linked=True, text='Synthetic person lives in Shanghai')
    unrelated = seed_fact(memory, 'Someone else', 'likes', 'tea', text='Someone else likes tea')
    memory.dao._database.close()

    with Database(cluster.runtime) as reopened:
        fresh = Memory(reopened, memory.context)
        assert fresh.get_episode(accepted['source_ids'][0]).content == 'Synthetic content'
        assert [fact.id for fact in fresh.history('Synthetic person', 'lives_in')] == [first.id, current.id]
        assert fresh.get_fact(current.id).supersedes == [first.id]
        with fresh.dao.read_snapshot() as view:
            edge = view.relations(first.id)[0]
            assert view.get(RecordKind.SOURCE, accepted['source_ids'][0]).agent_id == 'agent-1'
            untouched = view.get(RecordKind.FACT, unrelated.id)
        assert fresh.facts(FactQuery(subject='Synthetic person', valid_at=1))[0].id == first.id
        edited = fresh.set_fact_sensitive(first.id, True)
        assert edited.expired_at and edited.source == 'extracted'
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
        first = workers.submit(memory.remember_messages, 'chat', batch)
        other = workers.submit(second.remember_messages, 'chat', batch)
        left, right = first.result(), other.result()
    assert left['source_ids'] == right['source_ids']
    assert sorted([left['accepted'], right['accepted']]) == [0, 1]
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == 1
    with pytest.raises(MessageConflict):
        memory.remember_messages('chat', (message('new'), message('exact', 'Different content')))
    assert [source.message_id for source in memory.list_sources(session_id='chat')] == ['exact']
    memory.remember_messages('other', (message('exact'),))
    memory.remember_messages('chat', (message('another'),))
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == 3
    assert sorted(ep.sequence for ep in memory.list_sources(session_id='chat')) == [0, 1]


def test_read_view_is_consistent_and_stale_commit_rejected(memory, cluster):
    with memory.dao.read_snapshot('chat') as view:
        assert view.facts() == ()
        with Database(cluster.runtime) as writer:
            Memory(writer, memory.context).remember('New input', 'chat', extract=False)
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
    memory.remember_messages('chat', (message('m'),))
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == 1


@pytest.mark.parametrize('kind', ['message', 'note', 'manual'])
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
        result = memory.remember_messages('chat', (message('m'),))
    elif kind == 'note':
        result = memory.remember('Synthetic note', 'chat', extract=False)
    else:
        fact = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, source='user', text='Synthetic person likes tea')
        receipt = commit(memory, 'manual-op', (fact,))
        assert receipt.fact_ids == (memory.facts()[0].id,)
        assert memory.dao.receipt(receipt.operation_id) == receipt
    if kind != 'manual':
        source, = memory.list_sources()
        assert tuple(result['source_ids']) == (source.id,)
        assert not result['operation_ids']
    assert dropped
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == 1


def test_space_isolation_and_unauthorized_context(memory, runtime, accounts, spaces):
    accepted = memory.remember_messages('chat', (message('m'),))
    sibling = Memory(runtime, SpaceContext(
        memory.context.user_id, spaces.create(memory.context.user_id, 'Sibling').space_id))
    assert sibling.get_episode(accepted['source_ids'][0]) is None
    stored = commit(memory, 'private-receipt')
    assert sibling.dao.receipt(stored.operation_id) is None
    outsider = accounts.create_user()
    try:
        other = Memory(runtime, SpaceContext(outsider, memory.context.space_id))
        with pytest.raises(SpaceNotFound):
            other.remember_messages('chat', (message('m'),))
        with pytest.raises(SpaceNotFound):
            other.dao.receipt(stored.operation_id)
    finally:
        accounts.delete_user(outsider)


def test_mixed_erasure_is_atomic_and_supported_erasure_preserves_sources(memory):
    a = seed_note(memory, 'Synthetic source A', 'chat')
    b = seed_note(memory, 'Synthetic source B', 'chat')
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
    assert memory.get_fact(fact.id).provenance == [a.id, b.id]
    assert memory.erase(fact_ref).affected_count == 1
    assert memory.dao.operation(task.operation_id).stored_raw
    assert memory.get_episode(a.id) is not None
    assert memory.get_episode(b.id) is not None
    assert memory.facts() == ()


def test_fact_provenance_cannot_reference_a_source_from_another_space(memory, spaces):
    sibling_space = spaces.create(memory.context.user_id, 'Sibling')
    sibling = Memory(memory.dao._database, SpaceContext(memory.context.user_id, sibling_space.space_id))
    source = seed_note(sibling, 'Synthetic source in another space', 'chat')
    fact = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id,
                provenance=[source.id], text='Synthetic person likes tea')

    with pytest.raises(DatabaseConstraintError):
        commit(memory, 'cross-space-provenance', (fact,))
    assert memory.get_fact(fact.id) is None


def test_stale_vector_rejected_and_previous_embedding_removed(memory):
    fact = seed_fact(memory, 'Synthetic person', 'lives_in', 'Beijing', text='Synthetic person lives in Beijing')
    with memory.dao.read_snapshot() as view:
        snapshot, previous = view.snapshot, view.get(RecordKind.FACT, fact.id)
    updated = replace(fact, object='Shanghai', text='Synthetic person lives in Shanghai')
    write = memory._write(updated, previous, text_changed=True)
    memory._commit(snapshot, writes=(write,), embeddings=memory._embeddings(write, updated.text))
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


@pytest.mark.parametrize('claim', [
    ('x' * 513, 'likes', 'tea'),
    ('Synthetic person', 'likes', 'x' * 513),
    ('Synthetic person', 'likes', '\u0130' * 257),
    ('Synthetic person', 'likes', 'multi\nline'),
    ('Synthetic person', 'likes', 'tab\tvalue'),
])
def test_fact_persists_without_an_unrepresentable_graph_edge(memory, claim):
    fact = seed_fact(memory, *claim, text=' '.join(claim))
    stored = memory.get_fact(fact.id)
    assert (stored.subject, stored.predicate, stored.object) == claim
    with memory.dao.read_snapshot() as view:
        assert view.relations(fact.id) == ()


def test_raw_ingest_never_calls_embedder_or_rereads_new_messages(memory, runtime, monkeypatch):
    from mnemotrace.store.postgres.records import ScopedSQL

    class UnavailableEmbedder:
        dim = 2

        def embed(self, text):
            raise RuntimeError('Synthetic embedder unavailable')

    offline = Memory(runtime, memory.context, embedder=UnavailableEmbedder(), embedding_model='fake')
    original = ScopedSQL.select

    def select(sql, kind, *args, **kwargs):
        assert kind != RecordKind.SOURCE
        return original(sql, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ScopedSQL, 'select', select)
        batch = offline.remember_messages('chat', (message('m'),))
    note = offline.remember('Synthetic raw input', 'chat', extract=False, agent_id='test-writer')
    assert batch['ok'] and note['ok'] and not batch['operation_ids'] and not note['operation_ids']
    assert not offline.list_operations(session_id='chat')
    with offline.dao.read_snapshot() as view:
        dialogue = domain(view.get(RecordKind.SOURCE, batch['source_ids'][0]))
        record = view.get(RecordKind.SOURCE, note['source_ids'][0])
        stored = domain(record)
        assert (dialogue.source_kind, dialogue.sequence) == ('dialogue', 0)
        assert (stored.source_kind, stored.sequence, record.agent_id) == ('note', 1, 'test-writer')


def test_sensitivity_edit_skips_unchanged_fact_links(memory, monkeypatch):
    from mnemotrace.store.postgres.records import ScopedSQL

    original = seed_note(memory, 'Synthetic evidence', 'chat')
    predecessor = seed_fact(memory, 'Synthetic person', 'likes', 'tea',
                            text='Synthetic person likes tea', valid_at=1, invalid_at=2, expired_at=2)
    fact = seed_fact(memory, 'Synthetic person', 'likes', 'coffee',
                     text='Synthetic person likes coffee', valid_at=2,
                     provenance=[original.id], supersedes=[predecessor.id])
    delete = ScopedSQL.delete
    insert = ScopedSQL.insert

    def skip_link_deletes(sql, table, *args, **kwargs):
        assert table not in {'fact_sources', 'fact_supersedes'}
        return delete(sql, table, *args, **kwargs)

    def skip_link_inserts(sql, table, *args, **kwargs):
        assert table not in {'fact_sources', 'fact_supersedes'}
        return insert(sql, table, *args, **kwargs)

    monkeypatch.setattr(ScopedSQL, 'delete', skip_link_deletes)
    monkeypatch.setattr(ScopedSQL, 'insert', skip_link_inserts)
    memory.set_fact_sensitive(fact.id, True)
    stored = memory.get_fact(fact.id)
    assert stored.sensitive
    assert stored.provenance == [original.id] and stored.supersedes == [predecessor.id]


def test_clear_slot_checks_count_and_erases_live_facts_of_any_source(memory):
    extracted = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, text='Synthetic person likes tea')
    commit(memory, 'seed-extracted', (extracted,))
    manual = seed_fact(memory, 'Synthetic person', 'likes', 'coffee', source='user', text='Synthetic person likes coffee')
    ended = seed_fact(memory, 'Synthetic person', 'likes', 'cocoa', source='user', text='Synthetic person liked cocoa',
                      valid_at=10, invalid_at=20, expired_at=20)
    with pytest.raises(VersionConflict):
        memory.clear_slot('Synthetic person', 'likes', expect_count=1)
    assert {fact.id for fact in memory.facts()} == {extracted.id, manual.id}
    assert memory.clear_slot(' synthetic person ', 'likes', expect_count=2).affected_count == 2
    assert not memory.facts()
    assert memory.get_fact(ended.id).id == ended.id


def test_erasing_literal_facts_removes_only_unreferenced_entity(memory):
    subject = Entity('Synthetic person', 'person', memory.context.user_id)
    commit(memory, 'literal-entity', (subject,))
    facts = [seed_fact(memory, subject.name, 'likes', drink, subject_id=subject.id,
                       text=f'{subject.name} likes {drink}') for drink in ('tea', 'coffee')]
    assert all(memory.graph_relations(fact.id) == () for fact in facts)
    assert memory.delete_fact(facts[0].id)
    with memory.dao.read_snapshot() as view:
        assert view.get(RecordKind.ENTITY, subject.id) is not None
    assert memory.delete_fact(facts[1].id)
    with memory.dao.read_snapshot() as view:
        assert view.get(RecordKind.ENTITY, subject.id) is None


@pytest.mark.parametrize('business_change', [True, False])
def test_task_commit_checks_lease_and_its_persisted_source_references(memory, runtime, business_change):
    source = seed_note(memory, 'Synthetic task input', 'chat')
    other = seed_note(memory, 'Another source in the same session', 'chat')
    queued = OperationSpec('task', OperationKind.DIGEST, 'chat', (SourceVersion(source.id, 1),))
    commit(memory, 'queue', operations=(queued,))
    with runtime.space_transaction(memory.context.user_id, memory.context.space_id) as (conn, _):
        conn.execute("""UPDATE mnemotrace.operations SET state='running', phase='generate',
            extraction_config='test-model', lease_token='lease', lease_until=now() + interval '1 minute'
            WHERE user_id=%s AND space_id=%s AND operation_id='task'""",
                     (memory.context.user_id, memory.context.space_id))
    fact = Fact('Synthetic person', 'likes', 'tea', user_id=memory.context.user_id, text='Synthetic person likes tea')
    with memory.dao.read_snapshot('chat') as view:
        snapshot = view.snapshot
        writes = (*relation_writes(view, (fact,)), memory._write(fact)) if business_change else ()
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
