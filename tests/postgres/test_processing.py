from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from threading import Event

import pytest

from mnemotrace import Config
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from tests.embedder_fixtures import MockEmbedder
from mnemotrace.extraction import LLMNotConfiguredError
from mnemotrace.llm import FakeLLM
from mnemotrace.memory import Memory
from mnemotrace.store.contracts import (
    FactQuery, MessageInput, OperationKind, RecordKind, RecordRef,
)
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import (
    CommitOutcomeUnknown, DatabaseConfigurationError, DatabaseUnavailable, MessageConflict, VersionConflict,
)
from mnemotrace.worker import run_worker
from tests.postgres.fact_fixtures import checkpoint_before_publication, remember_and_publish
from tests.postgres.ontology_fixtures import mention
from tests.test_message_pipeline import PROPOSAL, REPORT, dialogue_evidence
from tests.test_pipeline_sources import boundary_facts


def dialogue(key, content='Synthetic person prefers tea', *, event_time=100):
    return MessageInput(key, 'user', content, event_time=event_time)


def recorded_digests(data, text='Recorded preference'):
    return [dict(source_id=item['id'], text=text) for item in data['pending'] if item['needs_digest']]


def test_assistant_evidence_stays_in_session_until_user_confirmation(processing):
    def adjudicate(prompt):
        return json.dumps(dict(decisions=[dict(id=pair['id'], decision='conflict')
                                          for pair in json.loads(prompt)['items']]))

    def extract(data):
        result = dialogue_evidence(data)
        for fact in result['facts']:
            fact['ontology'] = dict(subject=mention('person'), object=mention(),
                                    meaning='Preferred default package manager', direction='forward')
        return result

    memory, _ = processing(handle=extract, adjudicate=adjudicate)

    def send(key, role, content, event_time):
        result = remember_and_publish(memory, [MessageInput(key, role, content, event_time)], 'chat')
        assert result['status'] == 'completed'
        source_id, = result['source_ids']
        return source_id

    send('task', 'user', 'Prepare my release checklist.', 100)
    send('before', 'user', 'My default package manager is npm.', 110)
    proposal = send('proposal', 'assistant', PROPOSAL, 200)
    send('report', 'assistant', REPORT, 250)
    old, = memory.facts()
    assert old.object == 'npm'
    assert memory.get_episode(proposal).role == 'assistant'
    context = memory.context_for(PROPOSAL, top_k=0).context
    assert PROPOSAL not in context
    assert memory.profile()["updated_at"] is None

    confirmation = send('confirmation', 'user', 'Yes, use that by default.', 300)
    new, = memory.facts()
    assert (new.object, new.valid_at, new.supersedes) == ('pnpm', 300, [old.id])
    assert set(new.provenance) == {proposal, confirmation}
    assert [memory.get_episode(key).role for key in new.provenance] == ['assistant', 'user']


def first_task(memory, session_id='chat'):
    operations = memory.list_operations(session_id=session_id)
    assert len(operations) == 1
    return operations[0].operation_id


def test_three_checkpoints_and_durable_retry(processing, cluster):
    class InterruptedEmbedder(MockEmbedder):
        def embed_batch(self, texts):
            raise RuntimeError('synthetic embedding failure')

    memory, calls = processing(embedder=InterruptedEmbedder())
    result = memory.remember([dialogue('one')], 'chat')
    operation_id = result['operation_id']
    source_id, = result['source_ids']
    assert memory.get_episode(source_id).content == 'Synthetic person prefers tea'
    assert memory.operation(operation_id).phase.value == 'queued'
    with memory.dao.read_snapshot() as view:
        revision = view.snapshot.core_revision

    assert memory._processing.run_next_stage(operation_id)
    checkpoint = memory.operation(operation_id)
    assert checkpoint.extraction_saved and not checkpoint.core_committed
    assert memory.facts() == ()
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == revision
    assert memory.operation(operation_id).state.value == 'retry_wait'
    assert len(calls) == 1

    with Database(cluster.runtime) as restarted:
        restored, restored_calls = processing(database=restarted)
        assert restored.retry_operation(operation_id).extraction_saved
        assert restored._processing.run_next_stage(operation_id)
        assert restored_calls == []
        fact, = restored.facts()
        assert fact.provenance == [source_id]
        assert restored.operation(operation_id).core_committed
        assert restored.dao.receipt(operation_id).fact_ids == (fact.id,)
        assert not restored._processing.run_next_stage(operation_id)
        replay = restored.remember([dialogue('one')], 'chat')
        assert replay['status'] == 'completed' and replay['messages'] == result['messages']


def test_duplicate_concurrency_and_atomic_rejection(processing, cluster, runtime):
    memory, _ = processing()
    offline = Memory(runtime, memory.context)
    with pytest.raises(LLMNotConfiguredError):
        offline.remember([dialogue('one'), dialogue('two')], 'chat')
    assert offline.list_sources() == ()
    with Database(cluster.runtime) as pool, ThreadPoolExecutor(2) as executor:
        other, _ = processing(database=pool)
        batch = (dialogue('same'),)
        first, second = [future.result() for future in (
            executor.submit(memory.remember, batch, 'chat'),
            executor.submit(other.remember, batch, 'chat'),
        )]
    assert first['source_ids'] == second['source_ids']
    assert first['operation_id'] == second['operation_id']
    with pytest.raises(MessageConflict):
        memory.remember([dialogue('new'), dialogue('same')], 'chat')
    assert len(memory.list_sources()) == 1


def test_batch_preserves_message_order_and_rejects_duplicate_ids(processing):
    memory, calls = processing()
    batch = [dialogue('first'), MessageInput('middle', 'assistant', 'Synthetic response'), dialogue('last')]
    with pytest.raises(ValueError, match='unique'):
        memory.remember([*batch, batch[0]], 'chat')
    assert not memory.list_sources()
    result = remember_and_publish(memory, batch, 'chat')
    assert result['status'] == 'completed'
    assert [item['sequence'] for item in result['messages']] == [0, 1, 2]
    assert [item['id'] for item in calls[0]['pending']] == list(result['source_ids'])


def test_lease_survives_other_requests_in_same_space_and_fences_old_worker(processing, cluster):
    memory, _ = processing()
    first = memory.remember([dialogue('first')], 'chat')['operation_id']
    parallel = memory.remember([dialogue('other')], 'other')['operation_id']
    lease, kind = memory._processing.tasks.claim(first)
    assert kind is OperationKind.EXTRACT
    with Database(cluster.runtime) as pool:
        other, _ = processing(database=pool)
        assert other._processing.tasks.claim(first) is None
        assert other._processing.tasks.claim(parallel) is None
        with memory.dao._transaction() as sql:
            sql.run('UPDATE mnemotrace.operations SET lease_until = clock_timestamp() '
                    'WHERE user_id = %s AND space_id = %s AND operation_id = %s', (*sql.scope, first))
        replacement, _ = other._processing.tasks.claim(first)
        assert replacement.token != lease.token
        with pytest.raises(VersionConflict):
            memory._processing.tasks.renew(lease)
        other._processing.tasks.renew(replacement)
        assert other.operation(parallel).state.value == 'pending'


def test_discovery_credentials_only_and_worker_publishes_a_task(processing, cluster, runtime):
    memory, _ = processing()
    operation_id = memory.remember([dialogue('worker')], 'chat')['operation_id']
    with pytest.raises(DatabaseConfigurationError):
        runtime.discover_processing()
    with Database(cluster.discovery, purpose='discovery') as discovery:
        assert discovery.discover_processing() == ((memory.context, operation_id),)
        with pytest.raises(DatabaseConfigurationError):
            with discovery.space_transaction(memory.context.user_id, memory.context.space_id):
                pytest.fail('Discovery credentials can write business data')
        pipeline = memory._processing
        run_worker(discovery, runtime, pipeline.config, pipeline.llm, pipeline.embedder, pipeline.model, once=True)
    assert memory.operation(operation_id).core_committed


@pytest.mark.parametrize('shorthand', [True, False])
def test_full_source_publishes_fact_and_digest_together(processing, shorthand):
    config = Config(memory_max_input_tokens=4_000, digest_min_chars=300, digest_max_chars=100)
    def record_excerpt(data):
        return dict(facts=boundary_facts(data), digests=recorded_digests(data, text='HEAD-41, MIDDLE-42 and TAIL-43'))

    memory, calls = processing(config=config, handle=record_excerpt)
    head, middle, tail = 'HEAD-41', 'MIDDLE-42', 'TAIL-43'
    text = (f'{head} is in Synthetic opening. ' + '长原文' * 4000
            + f' Synthetic middle contains {middle}. ' + '长原文' * 4000
            + f' Synthetic closing contains {tail}')
    value = text if shorthand else [dialogue('long', text)]
    accepted = memory.remember(value, 'docs')
    operation_id = accepted['operation_id']
    source_id, = accepted['source_ids']
    checkpoint_before_publication(memory, operation_id)
    assert not memory.facts() and not memory.get_episode(source_id).digest
    memory.retry_operation(operation_id)
    assert memory._processing.run_next_stage(operation_id)
    assert memory.operation(operation_id).core_committed
    call, = calls
    view, = call['pending']
    assert view['content'].startswith(head) and view['content'].endswith(tail)
    assert middle in view['content']
    episode = memory.get_episode(source_id)
    assert episode.content == text and episode.consolidated
    assert episode.coverage == episode.digest_coverage
    assert episode.coverage['ranges'] == view['ranges'] and not episode.coverage['partial']
    assert episode.digest == 'HEAD-41, MIDDLE-42 and TAIL-43'
    facts = memory.facts()
    assert {fact.object for fact in facts} == {head, middle, tail}
    assert all(fact.provenance == [source_id] for fact in facts)
    assert set(memory.dao.receipt(operation_id).fact_ids) == {fact.id for fact in facts}


def test_timeout_and_clear_space_while_model_runs(processing):
    entered, release = Event(), Event()

    def block(_):
        entered.set()
        assert release.wait(10)
        return dict(facts=[], digests=[])

    memory, _ = processing(handle=block)
    messages = [dialogue('erase')]
    accepted = memory.remember(messages, 'chat')
    task_id = accepted['operation_id']
    with ThreadPoolExecutor(1) as pool:
        active = pool.submit(memory._processing.run_next_stage, task_id)
        try:
            assert entered.wait(5)
            waited = memory.remember(messages, 'chat', wait=True, timeout=0)
            assert waited['status'] == 'timed_out'
            assert waited['task'].state.value == 'running'
            memory.clear_space()
        finally:
            release.set()
        assert active.result()
    assert memory.operation(task_id) is None
    assert memory.get_episode(accepted['source_ids'][0]) is None
    assert memory.facts() == ()


@pytest.mark.parametrize('stage', ['accept', 'product', 'core'])
@pytest.mark.parametrize('committed', [False, True])
def test_interrupted_transaction_resumes_from_actual_checkpoint(processing, monkeypatch, stage, committed):
    memory, calls = processing()
    message = dialogue('interrupted')
    original = memory.dao._transaction
    interrupted = False
    checkpoint = {'accept': 'commit_receipts', 'product': 'operation_products',
                  'core': 'commit_receipts'}[stage]
    operation_id = None
    if stage != 'accept':
        operation_id = memory.remember([message], 'chat')['operation_id']
        if stage == 'core':
            checkpoint_before_publication(memory, operation_id)
            memory.retry_operation(operation_id)
            calls.clear()

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
                accepted = memory.remember([message], 'chat')
            else:
                with pytest.raises(CommitOutcomeUnknown):
                    memory.remember([message], 'chat')
        else:
            assert memory._processing.run_next_stage(operation_id)
    assert interrupted
    if stage == 'accept':
        replay = memory.remember([message], 'chat')
        operation_id = replay['operation_id']
        if committed:
            assert replay['messages'] == accepted['messages']
    elif stage == 'product':
        assert memory.operation(operation_id).extraction_saved is committed
        assert not memory.facts()
        memory.retry_operation(operation_id)
    else:
        assert bool(memory.facts()) is committed
        if not committed:
            with original() as sql:
                sql.run('UPDATE mnemotrace.operations SET lease_until = clock_timestamp() '
                        'WHERE user_id = %s AND space_id = %s AND operation_id = %s', (*sql.scope, operation_id))
    while memory._processing.run_next_stage(operation_id):
        pass
    assert memory.operation(operation_id).core_committed
    fact, = memory.facts()
    assert fact.categories == ['food']
    assert len(calls) == (0 if stage == 'core' else 2 if stage == 'product' and not committed else 1)


def test_semantic_reconciliation_and_unordered_candidate(processing):
    class SemanticEmbedder:
        dim = 2

        def embed(self, text):
            return [1.0, 0.0]

        def embed_batch(self, texts):
            return [self.embed(text) for text in texts]

    def extract(data):
        item = data['pending'][0]
        predicate, value = item['content'].split()[-2:]
        fact = dict(categories=[], sensitive=False, time_kind='current', effective_date=None, retention='unknown', subject='Synthetic person', predicate=predicate, object=value,
                    text=f'Synthetic person {predicate} {value}',
                    source_ids=[item['id']], ontology=dict(subject=mention('person'), object=mention('location'),
                                                          meaning='Primary residence', direction='forward'))
        return dict(facts=[fact], digests=[])

    from tests.test_fact_adjudication import decisions
    memory, _ = processing(embedder=SemanticEmbedder(), handle=extract, adjudicate=decisions)
    old = MessageInput('old', 'user', 'Synthetic person lives_in Beijing', event_time=100)
    new = MessageInput('new', 'user', 'Synthetic person based_in Shanghai', event_time=200)
    assert remember_and_publish(memory, [old], 'chat')['ok']
    historical, = memory.facts()
    assert remember_and_publish(memory, [new], 'chat')['ok']
    current, = memory.facts()
    assert current.supersedes == [historical.id]
    assert memory.get_fact(historical.id).invalid_at == 200
    assert memory.facts(FactQuery(valid_at=150))[0].id == historical.id

    attempt = MessageInput('unordered', 'user', 'Synthetic person based_in Shenzhen', event_time=200)
    assert remember_and_publish(memory, [attempt], 'chat')['ok']
    assert tuple(fact.id for fact in memory.facts()) == (current.id,)
    assert memory.entity('Shenzhen') is None


def test_rejected_source_erasure_preserves_digest_and_its_context(processing):
    def extract(data):
        return dict(facts=[], digests=[
            dict(source_id=item['id'], text='Recorded detail')
            for item in data['pending'] if item['needs_digest']
        ])

    memory, calls = processing(config=Config(digest_min_chars=35, digest_max_chars=20), handle=extract)
    first = remember_and_publish(memory, [dialogue('context', 'Short background')], 'chat')
    second = remember_and_publish(memory, [dialogue('target', 'A long synthetic record with details')], 'chat')
    prior, = first['source_ids']
    target, = second['source_ids']
    assert calls[-1]['context'][0]['id'] == prior
    operation_id = second['operation_id']
    with memory.dao._transaction(read_only=True) as sql:
        links = sql.run("""SELECT source_id, content_version, is_context FROM mnemotrace.operation_sources
            WHERE user_id = %s AND space_id = %s AND operation_id = %s ORDER BY is_context""",
            (*sql.scope, operation_id)).fetchall()
    assert links == [(target, 1, False), (prior, 1, True)]
    assert memory.operation(operation_id).core_committed
    assert len(calls) == 2
    assert memory.get_episode(target).digest == 'Recorded detail'

    def lexical_fields():
        with memory.dao._transaction(read_only=True) as sql:
            return {field for field, in sql.run("""SELECT DISTINCT d.field FROM mnemotrace.lexical_documents d
                JOIN mnemotrace.lexical_terms t ON (t.user_id,t.space_id,t.document_id) = (d.user_id,d.space_id,d.id)
                WHERE d.user_id = %s AND d.space_id = %s AND d.source_id = %s""", (*sql.scope, target))}

    assert lexical_fields() == {'digest'}
    with pytest.raises(ValueError, match='only supports'):
        memory.erase(RecordRef(memory.context, RecordKind.SOURCE, prior))
    assert memory.get_episode(prior) is not None
    assert memory.get_episode(target).digest == 'Recorded detail'
    assert lexical_fields() == {'digest'}
    replay = memory.remember([dialogue('target', 'A long synthetic record with details')], 'chat')
    assert replay['messages'] == second['messages']


def test_new_baseline_has_no_digest_operation_kind(processing):
    memory, _ = processing()
    with pytest.raises(ValueError):
        OperationKind('digest')
    with memory.dao._transaction(read_only=True) as sql:
        assert sql.run("SELECT to_regclass('mnemotrace.operation_digest_parts')").fetchone() == (None,)


@pytest.mark.parametrize('settings, saves_first_result', [
    ({'track': ('Synthetic topic',)}, True),
    ({'mute': ('Synthetic topic',)}, True),
    ({'profile_system': 'Synthetic profile policy'}, True),
    ({'extract_instruction': 'Synthetic extraction policy'}, False),
])
def test_saving_inflight_extraction_checks_only_extraction_policy(processing, settings, saves_first_result):
    def generate(_):
        if len(calls) == 1:
            memory.set_settings(**settings)
        return dict(facts=[], digests=[])

    memory, calls = processing(handle=generate)
    operation_id = memory.remember([dialogue('one')], 'chat')['operation_id']
    assert memory._processing.run_next_stage(operation_id)
    status = memory.operation(operation_id)
    assert status.extraction_saved is saves_first_result
    assert status.error_code == (None if saves_first_result else 'version_conflict')
    if not saves_first_result:
        assert memory._processing.run_next_stage(operation_id)
    assert memory.operation(operation_id).core_committed
    assert len(calls) == (1 if saves_first_result else 2)


def test_profile_policy_change_does_not_invalidate_raw_context(processing):
    def generate(data):
        if len(calls) == 2:
            assert [item['representation'] for item in data['context']] == ['raw']
            memory.set_settings(profile_system='Synthetic profile policy')
        return dict(facts=[], digests=recorded_digests(data))

    memory, calls = processing(handle=generate)
    assert remember_and_publish(memory, 'Synthetic background ' * 100, 'chat')['status'] == 'completed'
    outcome = remember_and_publish(memory, 'Short follow-up', 'chat')
    assert outcome['status'] == 'completed'
    assert len(calls) == 2


def test_missing_digest_retries_entire_product_without_publishing_facts(processing):
    def extract(data):
        item, = data['pending']
        facts = [dict(subject='我', predicate='likes', object='tea', text='Synthetic person likes tea',
                      categories=['food'], sensitive=False, time_kind='current', effective_date=None,
                      retention='unknown', source_ids=[item['id']])]
        return dict(facts=facts, digests=recorded_digests(data) if len(calls) > 1 else [])

    memory, calls = processing(config=Config(digest_min_chars=35, digest_max_chars=20), handle=extract)
    accepted = memory.remember([dialogue('one', 'A long synthetic record with details')], 'chat')
    operation_id = accepted['operation_id']
    source_id, = accepted['source_ids']
    assert memory._processing.run_next_stage(operation_id)
    status = memory.operation(operation_id)
    assert status.state.value == 'retry_wait' and status.error_code == 'extraction_invalid_response'
    assert not status.extraction_saved and not status.core_committed
    assert not memory.facts() and not memory.get_episode(source_id).digest
    with memory.dao._transaction() as sql:
        sql.update('operations', {'next_attempt_at': sql.run('SELECT clock_timestamp()').fetchone()[0]},
                   'operation_id = %s', (operation_id,))
    assert memory._processing.run_next_stage(operation_id)
    assert calls[0] == calls[1] and len(calls) == 2
    assert memory.operation(operation_id).core_committed
    assert memory.get_episode(source_id).digest == 'Recorded preference'
    assert len(memory.facts()) == 1


def test_append_is_immediately_ready_and_never_changes_earlier_task_members(processing, cluster):
    memory, calls = processing(handle=lambda _: dict(facts=[], digests=[]))
    first = memory.remember([dialogue('first')], 'chat')['operation_id']
    second = memory.remember([dialogue('second')], 'chat')['operation_id']
    with Database(cluster.discovery, purpose='discovery') as discovery:
        assert (memory.context, first) in discovery.discover_processing()
        assert (memory.context, second) not in discovery.discover_processing()
        assert memory._processing.run_next_stage(first)
        assert (memory.context, second) in discovery.discover_processing()
        assert memory._processing.run_next_stage(second)
    assert [[item['message_id'] for item in call['pending']] for call in calls] == [['first'], ['second']]


def test_policy_change_regenerates_the_complete_product(processing):
    memory, calls = processing(config=Config(digest_min_chars=35, digest_max_chars=20))
    accepted = memory.remember([dialogue('one', 'A long synthetic record with details')], 'chat')
    operation_id = accepted['operation_id']
    source_id, = accepted['source_ids']
    checkpoint_before_publication(memory, operation_id)
    memory.set_settings(extract_instruction='Changed synthetic extraction rule')
    memory.retry_operation(operation_id)
    assert memory._processing.run_next_stage(operation_id)
    assert memory._processing.run_next_stage(operation_id)
    assert memory.operation(operation_id).core_committed
    assert len(calls) == 2
    assert memory.get_episode(source_id).digest == 'Recorded preference'


@pytest.mark.parametrize("shorthand", [True, False])
def test_assertion_and_profile_input_are_identical_offline_and_in_postgres(processing, shorthand):
    from mnemotrace.memory_engine import MemoryEngine
    from mnemotrace.profile import input_fact

    statement = "When travelling in winter, the user avoids tea unless it is decaffeinated."

    def handle(data):
        return {"facts": [{
            "subject": "我", "predicate": "avoids", "object": "tea", "text": statement,
            "categories": ["food"], "sensitive": False,
            "source_ids": [data["pending"][0]["id"]], "time_kind": "current",
            "effective_date": None, "retention": "unknown",
        }], "digests": []}

    memory, _ = processing(handle=handle)
    offline = MemoryEngine(llm=FakeLLM(handler=lambda prompt, _: json.dumps(
        handle(json.loads(prompt.removeprefix(WINDOW_PREFIX))))))
    if shorthand:
        remember_and_publish(memory, statement, "synthetic", event_time=100)
        offline.remember(statement, "synthetic", event_time=100)
    else:
        batch = [dialogue("synthetic", statement)]
        remember_and_publish(memory, batch, "synthetic")
        offline.remember(batch, "synthetic")
    persistent_fact, = memory.facts()
    offline_fact, = offline._all_facts()
    assert persistent_fact.text == offline_fact.text == statement
    assert input_fact(persistent_fact)["text"] == input_fact(offline_fact)["text"] == statement
