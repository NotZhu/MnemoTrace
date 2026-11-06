from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from threading import Event

import pytest

from mnemotrace import Config
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.embed import HashingEmbedder
from mnemotrace.extraction import LLMNotConfiguredError
from mnemotrace.llm import FakeLLM
from mnemotrace.postgres_memory import PostgresMemory
from mnemotrace.store.contracts import (
    ChangeSet, CommitConditions, FactQuery, MessageInput, OperationKind, OperationSpec, RecordKind, RecordRef,
    SourceVersion,
)
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import (
    CommitOutcomeUnknown, DatabaseConfigurationError, DatabaseUnavailable, MessageConflict, VersionConflict,
)
from mnemotrace.worker import run_worker


def dialogue(key, content='Synthetic person prefers tea', *, scope='auto'):
    return MessageInput(key, 'user', content, scope=scope, event_time=100)


@pytest.fixture
def processing(runtime, spaces, user):
    space = spaces.create(user, 'Processing tests')
    context = spaces.authorize(user, space.space_id)

    def make(*, database=runtime, embedder=None, config=None, handle=None):
        calls = []

        def respond(prompt, system=''):
            data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
            calls.append(data)
            if handle:
                return json.dumps(handle(data))
            if data['operation'] == 'summary':
                return json.dumps({'summaries': [{'source_id': data['pending'][0]['id'], 'text': 'Recorded preference'}]})
            facts = [dict(subject='Synthetic person', predicate='likes', object='tea', text='Synthetic person likes tea',
                          assertion_kind='user_statement' if item['role'] else 'unspecified',
                          source_ids=[item['id']]) for item in data['pending']]
            return json.dumps(dict(facts=facts, working=[], summaries=[]))

        memory = PostgresMemory(database, context, llm=FakeLLM(handler=respond), config=config,
                                embedder=embedder, embedding_model='fixture' if embedder else None)
        return memory, calls

    return make


def test_three_checkpoints_and_durable_retry(processing, cluster):
    class InterruptedEmbedder(HashingEmbedder):
        def embed_batch(self, texts):
            raise RuntimeError('synthetic embedding failure')

    memory, calls = processing(embedder=InterruptedEmbedder())
    result = memory.remember_messages('chat', (dialogue('one'),))
    operation_id, = result['operation_ids']
    source_id, = result['source_ids']
    assert memory.get_episode(source_id).content == 'Synthetic person prefers tea'
    assert memory.operation(operation_id).phase.value == 'queued'
    with memory.dao.read_snapshot() as view:
        revision = view.snapshot.core_revision

    assert memory._processing.run_one(operation_id, force=True)
    checkpoint = memory.operation(operation_id)
    assert checkpoint.extraction_saved and not checkpoint.core_committed
    assert memory.facts() == ()
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == revision
    assert memory._processing.run_one(operation_id)
    assert memory.operation(operation_id).state.value == 'retry_wait'
    assert len(calls) == 1

    with Database(cluster.runtime) as restarted:
        restored, restored_calls = processing(database=restarted)
        assert restored.retry_operation(operation_id).extraction_saved
        assert restored.flush_processing('chat')['ok']
        assert restored_calls == []
        fact, = restored.facts()
        assert fact.provenance == [source_id]
        assert restored.operation(operation_id).core_committed
        assert restored.dao.receipt(operation_id).fact_ids == (fact.id,)
        assert not restored._processing.run_one(operation_id)
        replay = restored.remember_messages('chat', (dialogue('one'),))
        assert replay['accepted'] == 0 and replay['operation_ids'] == (operation_id,)


def test_duplicate_concurrency_and_atomic_rejection(processing, cluster, runtime):
    memory, _ = processing()
    offline = PostgresMemory(runtime, memory.context)
    with pytest.raises(LLMNotConfiguredError):
        offline.remember_messages('chat', (dialogue('raw', scope='raw'), dialogue('auto')))
    assert offline.list_sources() == ()
    with Database(cluster.runtime) as pool, ThreadPoolExecutor(2) as executor:
        other, _ = processing(database=pool)
        batch = (dialogue('same'),)
        first, second = [future.result() for future in (
            executor.submit(memory.remember_messages, 'chat', batch),
            executor.submit(other.remember_messages, 'chat', batch),
        )]
    assert first['source_ids'] == second['source_ids']
    assert first['operation_ids'] == second['operation_ids']
    assert sorted([first['accepted'], second['accepted']]) == [0, 1]
    with pytest.raises(MessageConflict):
        memory.remember_messages('chat', (dialogue('new'), dialogue('same', 'different content')))
    assert len(memory.list_sources()) == 1


def test_leases_order_sessions_and_fence_old_executors(processing, cluster):
    memory, _ = processing()
    first, = memory.remember_messages('chat', (dialogue('first'),))['operation_ids']
    assert memory.flush_processing('chat', timeout=0)['status'] == 'timed_out'
    second, = memory.remember_messages('chat', (dialogue('second'),))['operation_ids']
    parallel, = memory.remember_messages('other', (dialogue('other'),))['operation_ids']
    lease = memory._processing.tasks.claim(first)
    with Database(cluster.runtime) as pool:
        other, _ = processing(database=pool)
        assert other._processing.tasks.claim(second, force=True) is None
        assert other._processing.tasks.claim(parallel, force=True) is not None
        with memory.dao._transaction() as sql:
            sql.run('UPDATE mnemotrace.operations SET lease_until = clock_timestamp() '
                    'WHERE user_id = %s AND space_id = %s AND operation_id = %s', (*sql.scope, first))
        replacement = other._processing.tasks.claim(first)
        assert replacement.token != lease.token
        with pytest.raises(VersionConflict):
            memory._processing.tasks.renew(lease)
        other.clear_working('chat')
        assert other.operation(first).state.value == 'cancelled'
        assert other.operation(parallel).state.value == 'running'
        with pytest.raises(VersionConflict):
            other._processing.tasks.renew(replacement)


def test_discovery_credentials_only_and_worker_runs_both_stages(processing, cluster, runtime):
    memory, _ = processing()
    operation_id, = memory.remember_messages('chat', (dialogue('worker'),))['operation_ids']
    with pytest.raises(DatabaseConfigurationError):
        runtime.discover_processing()
    with Database(cluster.discovery, purpose='discovery') as discovery:
        assert discovery.discover_processing() == ()
        memory.flush_processing('chat', timeout=0)
        assert discovery.discover_processing() == ((memory.context, operation_id),)
        with pytest.raises(DatabaseConfigurationError):
            with discovery.space_transaction(memory.context.user_id, memory.context.space_id):
                pytest.fail('Discovery credentials can write business data')
        pipeline = memory._processing
        for _ in range(2):
            run_worker(discovery, runtime, pipeline.config, pipeline.llm, pipeline.embedder, pipeline.model, once=True)
    assert memory.operation(operation_id).core_committed


def test_segmented_document_keeps_operation_id_and_summary_is_independent(processing):
    config = Config(extraction_max_input_bytes=7000, summary_min_chars=300, summary_max_chars=100)
    memory, calls = processing(config=config)
    text = 'Synthetic document with preference details. ' * 300
    accepted = memory.remember(text, 'docs', source_kind='document', processing='buffered')
    operation_id, = accepted['operation_ids']
    source_id, = accepted['source_ids']
    assert memory._processing.run_one(operation_id)
    assert memory._processing.run_one(operation_id)
    assert not memory.operation(operation_id).core_committed
    assert memory.get_episode(source_id).coverage['partial']
    result = memory.flush_processing('docs', include_summaries=True, timeout=15)
    assert result['ok'], result
    assert memory.operation(operation_id).core_committed
    episode = memory.get_episode(source_id)
    assert episode.coverage['ranges'] == [[0, len(text)]]
    assert episode.summary_coverage['ranges'] == [[0, len(text)]]
    extracts = [call['pending'][0]['ranges'][0] for call in calls if call['operation'] == 'extract']
    assert len(extracts) > 1 and all(a[1] == b[0] for a, b in zip(extracts, extracts[1:]))


def test_timeout_and_erase_while_model_runs(processing):
    entered, release = Event(), Event()

    def block(_):
        entered.set()
        assert release.wait(10)
        return dict(facts=[], working=[], summaries=[])

    memory, _ = processing(handle=block)
    try:
        result = memory.remember_messages('chat', (dialogue('erase'),), processing='immediate', timeout=.5)
        operation_id, = result['operation_ids']
        source_id, = result['source_ids']
        assert entered.is_set() and result['status'] == 'timed_out'
        memory.erase(RecordRef(memory.context, RecordKind.SOURCE, source_id))
    finally:
        release.set()
    assert memory.operation(operation_id) is None
    assert memory.get_episode(source_id) is None
    assert memory.facts() == ()


@pytest.mark.parametrize('stage', ['accept', 'product', 'core'])
@pytest.mark.parametrize('committed', [False, True])
def test_interrupted_transaction_resumes_from_actual_checkpoint(processing, monkeypatch, stage, committed):
    memory, calls = processing()
    message = dialogue('interrupted')
    original = memory.dao._transaction
    interrupted = False
    checkpoint = {'accept': 'record_commit_receipts', 'product': 'operation_products',
                  'core': 'operation_receipts'}[stage]
    operation_id = None
    if stage != 'accept':
        operation_id, = memory.remember_messages('chat', (message,))['operation_ids']
        if stage == 'core':
            assert memory._processing.run_one(operation_id, force=True)

    @contextmanager
    def interrupt(**kwargs):
        nonlocal interrupted
        with original(**kwargs) as sql:
            yield sql
            if interrupted or kwargs.get('read_only'):
                return
            condition = '' if operation_id is None else ' AND operation_id = %s'
            args = sql.scope if operation_id is None else (*sql.scope, operation_id)
            if not sql.run(f'SELECT 1 FROM mnemotrace.{checkpoint} '
                           'WHERE user_id = %s AND space_id = %s' + condition, args).fetchone():
                return
            interrupted = True
            if not committed:
                raise DatabaseUnavailable('Synthetic disconnect before commit')
        raise DatabaseUnavailable('Synthetic disconnect after commit')

    with monkeypatch.context() as patch:
        patch.setattr(memory.dao, '_transaction', interrupt)
        if stage == 'accept':
            if committed:
                operation_id, = memory.remember_messages('chat', (message,))['operation_ids']
            else:
                with pytest.raises(CommitOutcomeUnknown):
                    memory.remember_messages('chat', (message,))
        else:
            assert memory._processing.run_one(operation_id, force=True)
    assert interrupted
    if stage == 'accept':
        replay = memory.remember_messages('chat', (message,))
        assert replay['accepted'] == int(not committed)
        if committed:
            assert replay['operation_ids'] == (operation_id,)
    elif stage == 'product':
        assert memory.operation(operation_id).extraction_saved is committed
        assert not memory.facts()
        if not committed:
            memory.retry_operation(operation_id)
    else:
        assert bool(memory.facts()) is committed
        if not committed:
            with original() as sql:
                sql.run('UPDATE mnemotrace.operations SET lease_until = clock_timestamp() '
                        'WHERE user_id = %s AND space_id = %s AND operation_id = %s', (*sql.scope, operation_id))
    assert memory.flush_processing('chat')['ok']
    assert len(memory.facts()) == 1
    assert len(calls) == (2 if stage == 'product' and not committed else 1)


def test_semantic_reconciliation_and_manual_authority(processing):
    class SemanticEmbedder:
        dim = 2

        def embed(self, text):
            return [1.0, 0.0]

        def embed_batch(self, texts):
            return [self.embed(text) for text in texts]

    def extract(data):
        item = data['pending'][0]
        predicate, value = item['content'].split()
        fact = dict(subject='Synthetic person', predicate=predicate, object=value,
                    text=f'Synthetic person {predicate} {value}', assertion_kind='user_statement',
                    source_ids=[item['id']])
        return dict(facts=[fact], working=[], summaries=[])

    memory, _ = processing(embedder=SemanticEmbedder(), handle=extract)
    old = MessageInput('old', 'user', 'lives_in Beijing', event_time=100)
    new = MessageInput('new', 'user', 'based_in Shanghai', event_time=200)
    assert memory.remember_messages('chat', (old,), processing='immediate')['ok']
    historical, = memory.facts()
    assert memory.remember_messages('chat', (new,), processing='immediate')['ok']
    current, = memory.facts()
    assert current.supersedes == historical.id
    assert memory.get_fact(historical.id).invalid_at == 200
    assert memory.facts(FactQuery(valid_at=150, known_at=historical.created_at))[0].id == historical.id

    pinned = memory.add_fact('Synthetic person', 'based_in', 'Nanjing', valid_at=300)
    attempt = MessageInput('pinned', 'user', 'based_in Shenzhen', event_time=400)
    assert memory.remember_messages('chat', (attempt,), processing='immediate')['ok']
    assert tuple(fact.id for fact in memory.facts()) == (pinned.id,)


def test_context_erasure_purges_cached_summary_without_erasing_its_source(processing):
    def extract(data):
        if data['operation'] == 'summary':
            return dict(summaries=[dict(source_id=data['pending'][0]['id'], text='Rebuilt detail')])
        return dict(facts=[], working=[], summaries=[
            dict(source_id=item['id'], text='Recorded detail')
            for item in data['pending'] if item['needs_summary']
        ])

    memory, calls = processing(config=Config(summary_min_chars=35, summary_max_chars=20), handle=extract)
    first = memory.remember_messages('chat', (dialogue('context', 'Short background'),), processing='immediate')
    second = memory.remember_messages('chat', (dialogue('target', 'A long synthetic record with details'),),
                                      processing='immediate')
    prior, = first['source_ids']
    target, = second['source_ids']
    assert calls[-1]['context'][0]['id'] == prior
    assert memory.flush_processing('chat', include_summaries=True)['ok']
    assert len(calls) == 2
    assert memory.get_episode(target).summary == 'Recorded detail'

    def lexical_fields():
        with memory.dao._transaction(read_only=True) as sql:
            return {field for field, in sql.run("""SELECT DISTINCT d.field FROM mnemotrace.lexical_documents d
                JOIN mnemotrace.lexical_terms t ON (t.user_id,t.space_id,t.document_id) = (d.user_id,d.space_id,d.id)
                WHERE d.user_id = %s AND d.space_id = %s AND d.source_id = %s""", (*sql.scope, target))}

    assert lexical_fields() == {'text', 'summary'}
    memory.erase(RecordRef(memory.context, RecordKind.SOURCE, prior))
    surviving = memory.get_episode(target)
    assert surviving.consolidated and surviving.content and surviving.summary == ''
    assert lexical_fields() == {'text'}
    replay = memory.remember_messages('chat', (dialogue('target', 'A long synthetic record with details'),))
    assert replay['operation_ids'] == second['operation_ids']
    with memory.dao.read_snapshot('chat') as view:
        conditions = CommitConditions(view.snapshot)
    summary = OperationSpec('summary-rebuild', OperationKind.SUMMARY, 'chat', (SourceVersion(target, 1),))
    memory.dao.commit(ChangeSet(conditions, 'enqueue-rebuild', operations=(summary,)))
    assert memory._processing.run_one(summary.operation_id)
    assert memory._processing.run_one(summary.operation_id)
    assert memory.operation(summary.operation_id).core_committed
    assert memory.get_episode(target).summary == 'Rebuilt detail'
    with memory.dao._transaction(read_only=True) as sql:
        assert sql.run("""SELECT content_version FROM mnemotrace.embeddings
            WHERE user_id = %s AND space_id = %s AND source_id = %s AND field = 'summary'""",
            (*sql.scope, target)).fetchall() == [(2,)]


def test_summary_requires_one_source_and_rejects_old_multi_source_tasks(processing):
    memory, calls = processing()
    first = memory.remember_raw('First synthetic source', 'chat')
    second = memory.remember_raw('Second synthetic source', 'chat')
    sources = (SourceVersion(first.id, 1), SourceVersion(second.id, 1))
    with pytest.raises(ValueError, match='exactly one source'):
        OperationSpec('multi-summary', OperationKind.SUMMARY, 'chat', sources)
    with memory.dao.read_snapshot('chat') as view:
        conditions = CommitConditions(view.snapshot)
    summary = OperationSpec('old-summary', OperationKind.SUMMARY, 'chat', sources[:1])
    memory.dao.commit(ChangeSet(conditions, 'enqueue-old-summary', operations=(summary,)))
    with memory.dao._transaction() as sql:
        sql.insert('operation_sources', dict(operation_id=summary.operation_id, source_id=second.id,
                   content_version=1, input_start=0, input_end=len(second.content), input_order=1))
    assert memory._processing.run_one(summary.operation_id)
    status = memory.operation(summary.operation_id)
    assert status.state.value == 'failed'
    assert status.error_code == 'data_corruption'
    assert calls == []


def test_policy_change_reextracts_and_summary_failure_preserves_core(processing):
    def extract(data):
        if data['operation'] == 'summary':
            return dict(summaries=[])
        return dict(facts=[], working=[], summaries=[])

    memory, calls = processing(config=Config(summary_min_chars=35, summary_max_chars=20), handle=extract)
    accepted = memory.remember_messages('chat', (dialogue('one', 'A long synthetic record with details'),))
    operation_id, = accepted['operation_ids']
    assert memory._processing.run_one(operation_id, force=True)
    memory.set_settings(extract_instruction='Changed synthetic extraction rule')
    assert memory.flush_processing('chat')['ok']
    assert len(calls) == 2
    summary_id, = memory.operation(operation_id).auxiliary_operation_ids
    assert memory._processing.run_one(summary_id)
    assert memory.operation(summary_id).error_code == 'extraction_invalid_response'
    assert memory.operation(operation_id).core_committed


def test_repeated_config_changes_exhaust_generation_budget(processing):
    memory, calls = processing()
    operation_id, = memory.remember_messages('chat', (dialogue('one'),))['operation_ids']
    for index in range(3):
        assert memory._processing.run_one(operation_id, force=True)
        assert memory.operation(operation_id).extraction_saved
        memory.set_settings(extract_instruction=f'Synthetic version {index}')
        assert memory._processing.run_one(operation_id)
    assert not memory._processing.run_one(operation_id)
    assert memory.operation(operation_id).error_code == 'attempts_exhausted'
    assert len(calls) == 3 and not memory.facts()


def test_dao_enqueued_extract_is_discoverable_before_later_session_work(processing, cluster):
    memory, _ = processing()
    source = memory.remember_raw('Synthetic explicit extraction input', 'chat')
    with memory.dao.read_snapshot('chat') as view:
        conditions = CommitConditions(view.snapshot)
    queued = OperationSpec('queued-by-dao', OperationKind.EXTRACT, 'chat', (SourceVersion(source.id, 1),))
    memory.dao.commit(ChangeSet(conditions, 'enqueue-extract', operations=(queued,)))
    later, = memory.remember_messages('chat', (dialogue('later'),))['operation_ids']
    memory._processing.tasks.freeze([later], 'chat', False)
    with Database(cluster.discovery, purpose='discovery') as discovery:
        # Making later work ready must neither trigger nor bypass the DAO operation.
        assert discovery.discover_processing() == ((memory.context, queued.operation_id),)
        assert memory._processing.run_one(queued.operation_id)
        assert memory._processing.run_one(queued.operation_id)
        assert memory.operation(queued.operation_id).core_committed
        assert discovery.discover_processing() == ((memory.context, later),)


@pytest.mark.parametrize('config_change', ['length', 'policy'])
def test_summary_draft_regenerated_after_configuration_change(processing, config_change):
    def draft(data):
        return dict(facts=[], working=[], summaries=[
            dict(source_id=data['pending'][0]['id'], text='d' * 60)])

    config = Config(summary_min_chars=90, summary_max_chars=80)
    memory, _ = processing(config=config, handle=draft)
    result = memory.remember_messages('chat', (dialogue('draft', 'Synthetic summary material. ' * 8),),
                                      processing='immediate')
    assert result['ok']
    source_id, = result['source_ids']
    if config_change == 'length':
        config = Config(summary_min_chars=90, summary_max_chars=40)
    else:
        memory.set_settings(summary_system='Synthetic new summary policy')
    restarted, calls = processing(config=config)
    assert restarted.flush_processing('chat', include_summaries=True, timeout=3)['ok']
    assert [call['operation'] for call in calls] == ['summary']
    assert restarted.get_episode(source_id).summary == 'Recorded preference'


def test_summary_policy_change_can_recommit_the_same_fragment_range(processing):
    config = Config(extraction_max_input_bytes=7000, summary_min_chars=300, summary_max_chars=100)
    memory, old_calls = processing(config=config)
    memory.set_settings(summary_system='Original policy')
    result = memory.remember('Synthetic summary material. ' * 450, 'notes')
    assert result['ok']
    operation_id, = result['operation_ids']
    summary_id, = memory.operation(operation_id).auxiliary_operation_ids
    assert memory._processing.run_one(summary_id)
    assert memory._processing.run_one(summary_id)
    assert not memory.operation(summary_id).core_committed
    old_range = old_calls[-1]['pending'][0]['ranges']

    memory.set_settings(summary_system='Adjusted policy')
    restarted, calls = processing(config=config)
    assert restarted.flush_processing('notes', include_summaries=True, timeout=5)['ok']
    assert calls[0]['pending'][0]['ranges'] == old_range
    assert restarted.operation(summary_id).core_committed


@pytest.mark.parametrize('erase_early_fact', [False, True])
def test_final_receipt_includes_surviving_outputs_from_earlier_segments(processing, erase_early_fact):
    def extract(data):
        item, = data['pending']
        if item['ranges'][0][0]:
            return dict(facts=[], working=[], summaries=[])
        facts = [dict(subject='Synthetic person', predicate='likes', object=value,
                      text=f'Synthetic person likes {value}', assertion_kind='unspecified', source_ids=[item['id']])
                 for value in ('tea', 'coffee')]
        working = [dict(content='Synthetic task', kind='task', assertion_kind='unspecified', source_ids=[item['id']])]
        return dict(facts=facts, working=working, summaries=[])

    config = Config(extraction_max_input_bytes=7000, summary_min_chars=100000)
    memory, _ = processing(config=config, handle=extract)
    accepted = memory.remember('Synthetic source material. ' * 450, 'notes', processing='buffered')
    operation_id, = accepted['operation_ids']
    assert memory._processing.run_one(operation_id)
    assert memory._processing.run_one(operation_id)
    assert not memory.operation(operation_id).core_committed
    assert memory.dao.receipt(operation_id) is None
    early_facts = {fact.id for fact in memory.facts()}
    early_working = {item.id for item in memory.working_memory()}
    assert len(early_facts) == 2 and len(early_working) == 1
    if erase_early_fact:
        memory.erase(RecordRef(memory.context, RecordKind.FACT, early_facts.pop()))
    assert memory.flush_processing('notes', timeout=5)['ok']
    receipt = memory.dao.receipt(operation_id)
    assert set(receipt.fact_ids) == early_facts
    assert set(receipt.working_ids) == early_working
