"""Crash and race tests using independent clients of the disposable Postgres instance."""
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import replace
import json
import multiprocessing
import time
from unittest.mock import patch

import pytest

from mnemotrace import Config
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.embed import HashingEmbedder
from mnemotrace.llm import FakeLLM
from mnemotrace.postgres_memory import PostgresMemory
from mnemotrace.store.postgres import Accounts, Database
from mnemotrace.store.postgres.dao import PostgresDAO
from mnemotrace.store.postgres.errors import (
    AccountNotFound, AuthenticationError, DatabaseUnavailable, MessageConflict, SpaceNotFound,
)
from mnemotrace.store.postgres.tasks import ATTEMPT_LIMIT, Tasks
from mnemotrace.worker import run_worker
from tests.postgres.test_processing import dialogue


WAIT = 15
Child = namedtuple('Child', 'process ready resume receive')


def _pause(ready, resume):
    ready.set()
    assert resume.wait(WAIT), 'Parent did not release the checkpoint'


def _child(send, action, args, ready, resume):
    with send:
        send.send(action(*args, ready=ready, resume=resume))


@contextmanager
def _process(action, *args):
    spawn = multiprocessing.get_context('spawn')
    ready, resume = spawn.Event(), spawn.Event()
    receive, send = spawn.Pipe(duplex=False)
    process = spawn.Process(target=_child, args=(send, action, args, ready, resume))
    with receive, send:
        process.start()
        send.close()
        try:
            yield Child(process, ready, resume, receive)
        finally:
            if process.is_alive():
                process.kill()
            process.join(WAIT)
            process.close()


def _result(child):
    assert child.receive.poll(WAIT), 'Child did not return a result'
    try:
        result = child.receive.recv()
    except EOFError:
        child.process.join(WAIT)
        pytest.fail(f'Child exited without a result (exit code {child.process.exitcode})')
    child.process.join(WAIT)
    assert child.process.exitcode == 0
    return result


def _kill_at_checkpoint(child):
    assert child.ready.wait(WAIT), 'Child did not reach the checkpoint'
    child.process.kill()
    child.process.join(WAIT)
    assert child.process.exitcode not in (0, None)


def _model(calls, pause=None):
    def respond(prompt, system=None):
        request = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        calls.append(request['operation'])
        if pause:
            pause()
        if request['operation'] == 'summary':
            return json.dumps({'summaries': [{'source_id': request['pending'][0]['id'], 'text': 'Synthetic summary'}]})
        facts = []
        for item in request['pending']:
            subject, _, employer = item['content'].rpartition(' at ')
            subject = subject or 'Synthetic person'
            facts.append(dict(subject=subject, predicate='works_at', object=employer,
                              text=f'{subject} works at {employer}', assertion_kind='user_statement',
                              source_ids=[item['id']]))
        return json.dumps(dict(facts=facts, working=[], summaries=[]))

    return FakeLLM(handler=respond)


@pytest.fixture
def memory(runtime, spaces, user):
    space = spaces.create(user, 'Process recovery')
    return PostgresMemory(runtime, spaces.authorize(user, space.space_id), llm=_model([]))


def _accept(memory, key='one', content='Synthetic lab', session='chat'):
    result = memory.remember_messages(session, (dialogue(key, content),))
    operation_id, = result['operation_ids']
    return operation_id


def _expire(memory, operation_id):
    with memory.dao._transaction() as sql:
        assert sql.run('UPDATE mnemotrace.operations SET lease_until = clock_timestamp() '
                       'WHERE user_id = %s AND space_id = %s AND operation_id = %s AND state = %s',
                       (*sql.scope, operation_id, 'running')).rowcount == 1


CHECKPOINTS = {
    'accept': 'record_commit_receipts',
    'product': 'operation_products',
    'core': 'operation_receipts',
}


def _interrupt_transaction(config, scope, stage, committed, operation_id, *, ready, resume):
    original = PostgresDAO._transaction
    checkpoint_committed = False

    @contextmanager
    def interrupt(dao, *, read_only=False):
        nonlocal checkpoint_committed
        with original(dao, read_only=read_only) as sql:
            yield sql
            if read_only:
                return
            condition = '' if operation_id is None else ' AND operation_id = %s'
            params = sql.scope if operation_id is None else (*sql.scope, operation_id)
            written = sql.run(f'SELECT 1 FROM mnemotrace.{CHECKPOINTS[stage]} '
                              'WHERE user_id = %s AND space_id = %s' + condition, params).fetchone()
            if written and not committed:
                _pause(ready, resume)
        if written:
            checkpoint_committed = True
            if committed:
                _pause(ready, resume)

    with Database(config) as runtime, patch.object(PostgresDAO, '_transaction', interrupt):
        memory = PostgresMemory(runtime, scope, llm=_model([]))
        result = (_accept(memory) if stage == 'accept'
                  else memory._processing.run_one(operation_id, force=True))
        assert checkpoint_committed
        return result


def _recover(runtime_config, discovery_config, scope, operation_id, *, ready, resume):
    calls = []
    llm = _model(calls)
    with (Database(runtime_config) as runtime,
          Database(discovery_config, purpose='discovery') as discovery):
        memory = PostgresMemory(runtime, scope, llm=llm)
        if operation_id is None:
            operation_id = _accept(memory)
        memory.flush_processing('chat', timeout=0)
        for _ in range(3):
            if memory.operation(operation_id).core_committed:
                break
            run_worker(discovery, runtime, Config(), llm, HashingEmbedder(), 'hashing-v1', once=True)
        assert memory.operation(operation_id).core_committed
        return operation_id, calls


@pytest.mark.parametrize('stage', CHECKPOINTS)
@pytest.mark.parametrize('committed', [False, True], ids=['before-commit', 'after-commit'])
def test_process_death_at_each_durable_checkpoint(memory, cluster, stage, committed):
    operation_id = None
    if stage != 'accept':
        operation_id = _accept(memory)
    if stage == 'core':
        assert memory._processing.run_one(operation_id, force=True)

    with _process(_interrupt_transaction, cluster.runtime, memory.context, stage, committed, operation_id) as child:
        _kill_at_checkpoint(child)

    sources = memory.list_sources()
    if stage == 'accept':
        assert len(sources) == int(committed)
        operations = memory.list_operations()
        assert sum(item.kind.value == 'extract' for item in operations) == int(committed)
        if not committed:
            assert operations == ()
        if committed:
            operation_id, = memory.flush_processing('chat', timeout=0)['operation_ids']
    else:
        assert len(sources) == 1
        status = memory.operation(operation_id)
        assert status.extraction_saved is (stage == 'core' or committed)
        assert status.core_committed is (stage == 'core' and committed)
        assert (memory.dao.receipt(operation_id) is not None) is status.core_committed
        if status.state.value == 'running':
            _expire(memory, operation_id)
    assert len(memory.facts()) == int(stage == 'core' and committed)

    # Always replay acceptance, including when the original acceptance committed without a response.
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context,
                  None if stage == 'accept' else operation_id) as child:
        recovered, calls = _result(child)
    if operation_id is not None:
        assert recovered == operation_id
    assert calls == (['extract'] if stage == 'accept' or (stage == 'product' and not committed) else [])
    source, = memory.list_sources()
    fact, = memory.facts()
    assert fact.provenance == [source.id]
    assert memory.graph_relations(fact.id)
    assert memory.dao.receipt(recovered).fact_ids == (fact.id,)
    assert _accept(memory) == recovered


class _PausedEmbedder(HashingEmbedder):
    def __init__(self, pause):
        super().__init__()
        self.pause = pause

    def embed_batch(self, texts):
        self.pause()
        return super().embed_batch(texts)


def _run_stage(config, scope, operation_id, pause_at, *, ready, resume):
    calls = []

    def pause():
        _pause(ready, resume)

    def fail_renewal(tasks, lease):
        ready.set()
        raise DatabaseUnavailable('Synthetic heartbeat interruption')

    embedder = _PausedEmbedder(pause) if pause_at == 'embedding' else None
    gates = {'model': pause, 'heartbeat': lambda: _wait_for_resume(resume)}
    with Database(config) as runtime:
        memory = PostgresMemory(runtime, scope, llm=_model(calls, gates.get(pause_at)),
                                embedder=embedder, embedding_model='hashing-v1')
        if pause_at == 'heartbeat':
            with (patch.object(Tasks, 'renew', fail_renewal),
                  patch('mnemotrace._postgres_pipeline.RENEW_SECONDS', 0.01)):
                memory._processing.run_one(operation_id, force=True)
        else:
            memory._processing.run_one(operation_id, force=True)
    return calls


def _wait_for_resume(resume):
    assert resume.wait(WAIT), 'Parent did not release the model call'


def test_interrupted_heartbeat_fences_late_worker(memory, cluster):
    operation_id = _accept(memory)
    with _process(_run_stage, cluster.runtime, memory.context, operation_id, 'heartbeat') as old:
        assert old.ready.wait(WAIT)
        assert memory.operation(operation_id).state.value == 'running'
        _expire(memory, operation_id)
        with _process(_run_stage, cluster.runtime, memory.context, operation_id, 'model') as replacement:
            assert replacement.ready.wait(WAIT)
            old.resume.set()
            assert _result(old) == ['extract']
            status = memory.operation(operation_id)
            assert (status.state.value, status.attempts) == ('running', 2)
            assert not status.extraction_saved
            replacement.resume.set()
            assert _result(replacement) == ['extract']
    assert memory.operation(operation_id).extraction_saved
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context, operation_id) as recovering:
        assert _result(recovering) == (operation_id, [])
    fact, = memory.facts()
    assert memory.dao.receipt(operation_id).fact_ids == (fact.id,)


def test_repeated_process_death_exhausts_attempt_budget(memory, cluster):
    operation_id = _accept(memory)
    for _ in range(ATTEMPT_LIMIT):
        with _process(_run_stage, cluster.runtime, memory.context, operation_id, 'model') as child:
            _kill_at_checkpoint(child)
        _expire(memory, operation_id)
    with _process(_run_stage, cluster.runtime, memory.context, operation_id, None) as child:
        assert _result(child) == []
    status = memory.operation(operation_id)
    assert (status.state.value, status.error_code, status.attempts) == ('failed', 'attempts_exhausted', ATTEMPT_LIMIT)
    assert status.stored_raw and not status.core_committed
    assert len(memory.list_sources()) == 1
    assert memory.facts() == ()


def _accept_in_process(config, scope, *, ready, resume):
    with Database(config) as runtime:
        memory = PostgresMemory(runtime, scope, llm=_model([]))
        _pause(ready, resume)
        return memory.remember_messages('chat', (dialogue('one', 'Synthetic lab'),))


def test_parallel_acceptance_reuses_message_and_operation(memory, cluster):
    args = (cluster.runtime, memory.context)
    with _process(_accept_in_process, *args) as first, _process(_accept_in_process, *args) as second:
        for child in (first, second):
            assert child.ready.wait(WAIT)
        for child in (first, second):
            child.resume.set()
        a, b = _result(first), _result(second)
    assert sorted([a['accepted'], b['accepted']]) == [0, 1]
    assert a['source_ids'] == b['source_ids']
    assert a['operation_ids'] == b['operation_ids']
    with pytest.raises(MessageConflict):
        _accept(memory, content='Different synthetic lab')
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context, a['operation_ids'][0]) as child:
        assert _result(child) == (a['operation_ids'][0], ['extract'])
    assert len(memory.list_sources()) == len(memory.facts()) == 1


def test_space_write_lock_does_not_block_sibling_space(memory, cluster, spaces):
    sibling = spaces.create(memory.context.user_id, 'Sibling space')
    context = spaces.authorize(memory.context.user_id, sibling.space_id)
    with _process(_interrupt_transaction, cluster.runtime, memory.context, 'accept', False, None) as blocked:
        assert blocked.ready.wait(WAIT)
        with _process(_accept_in_process, cluster.runtime, context) as other:
            assert other.ready.wait(WAIT)
            other.resume.set()
            accepted = _result(other)
        assert accepted['accepted'] == 1
        assert memory.get_episode(accepted['source_ids'][0]) is None
        assert memory.operation(accepted['operation_ids'][0]) is None
        blocked.resume.set()
        assert _result(blocked) != accepted['operation_ids'][0]


def test_discovered_tasks_commit_only_in_their_own_spaces(memory, cluster, runtime, product_scope):
    other_context, _ = product_scope('Other account')
    other = PostgresMemory(runtime, other_context, llm=_model([]))
    first = _accept(memory)
    second = _accept(other, content='Another person at Another lab')
    for scoped in (memory, other):
        scoped.flush_processing('chat', timeout=0)
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context, first) as child:
        recovered, calls = _result(child)
    assert recovered == first
    assert calls == ['extract', 'extract']
    for scoped, own, foreign, employer in ((memory, first, second, 'Synthetic lab'),
                                           (other, second, first, 'Another lab')):
        assert scoped.operation(own).core_committed
        assert scoped.operation(foreign) is None
        source, = scoped.list_sources()
        fact, = scoped.facts()
        assert fact.object == employer and fact.provenance == [source.id]


def test_same_space_stale_merge_retries_without_reextracting(memory, cluster):
    first = _accept(memory)
    second = _accept(memory, key='two', content='Another person at Another lab', session='other')
    for operation_id in (first, second):
        assert memory._processing.run_one(operation_id, force=True)
    with _process(_run_stage, cluster.runtime, memory.context, first, 'embedding') as old:
        assert old.ready.wait(WAIT)
        with _process(_run_stage, cluster.runtime, memory.context, second, None) as concurrent:
            assert _result(concurrent) == []
        assert memory.operation(second).core_committed
        old.resume.set()
        assert _result(old) == []
    assert memory.operation(first).error_code == 'version_conflict'
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context, first) as retry:
        assert _result(retry) == (first, [])
    assert {fact.object for fact in memory.facts()} == {'Synthetic lab', 'Another lab'}


@pytest.mark.parametrize('change', ['manual', 'policy'])
def test_model_call_respects_concurrent_business_change(memory, cluster, change):
    pinned = memory.add_fact('Synthetic person', 'works_at', 'Old lab', valid_at=50)
    operation_id = _accept(memory)
    with _process(_run_stage, cluster.runtime, memory.context, operation_id, 'model') as child:
        assert child.ready.wait(WAIT)
        if change == 'manual':
            memory.update_fact(pinned.id, object='Pinned lab')
        else:
            memory.set_settings(extract_instruction='Updated synthetic extraction policy')
        child.resume.set()
        assert _result(child) == ['extract']
    assert memory.operation(operation_id).extraction_saved is (change == 'manual')
    with _process(_recover, cluster.runtime, cluster.discovery, memory.context, operation_id) as retry:
        assert _result(retry) == (operation_id, ['extract'] if change == 'policy' else [])
    current, = memory.facts()
    assert current.id == pinned.id
    assert current.object == ('Pinned lab' if change == 'manual' else 'Old lab')


def test_session_clear_cancels_only_its_inflight_work(memory, cluster):
    pinned = memory.add_fact('Synthetic person', 'works_at', 'Pinned lab')
    memory.remember_working('Clear this item', 'chat')
    retained = memory.remember_working('Retain this item', 'other')
    first = _accept(memory)
    with _process(_run_stage, cluster.runtime, memory.context, first, 'model') as active:
        assert active.ready.wait(WAIT)
        queued = _accept(memory, key='two')
        other = _accept(memory, key='three', content='Another person at Another lab', session='other')
        with _process(_run_stage, cluster.runtime, memory.context, queued, None) as successor:
            assert _result(successor) == []
        assert memory.operation(queued).attempts == 0
        with _process(_run_stage, cluster.runtime, memory.context, other, None) as independent:
            assert _result(independent) == ['extract']
        assert memory.clear_working('chat') == 1
        active.resume.set()
        assert _result(active) == ['extract']
    for operation_id in (first, queued):
        status = memory.operation(operation_id)
        assert (status.state.value, status.error_code) == ('cancelled', 'session_invalidated')
        assert status.stored_raw and not status.extraction_saved and not status.core_committed
    assert memory.operation(other).extraction_saved
    assert memory.flush_processing('other')['ok']
    assert {item.id for item in memory.working_memory()} == {retained.id}
    assert len(memory.list_sources()) == 3
    assert memory.get_fact(pinned.id) is not None


def _pending_write(memory, kind):
    if kind == 'vector':
        source = memory.remember_raw('Synthetic input for a source vector', 'chat')
        return memory.source_vector_operation(source.id).operation_id
    if kind == 'summary':
        result = memory.import_document('Synthetic summary input. ' * 100, 'chat', extract=False)
        operation_id, = result['operation_ids']
    else:
        operation_id = _accept(memory)
    if kind != 'extract':
        assert memory._processing.run_one(operation_id, force=True)
        assert memory.operation(operation_id).extraction_saved
    return operation_id


def _scope_counts(runtime, scope):
    from psycopg import sql

    tables = ('sessions', 'sources', 'source_summaries', 'facts', 'fact_sources', 'conflicts',
              'working_memories', 'working_sources',
              'entities', 'entity_merges', 'graph_relations', 'embeddings', 'lexical_documents', 'lexical_terms',
              'operations', 'operation_sources', 'operation_targets', 'operation_products', 'operation_summary_parts',
              'operation_receipts', 'import_receipts')
    with runtime._transaction() as conn:
        conn.execute("SELECT set_config('mnemotrace.user_id', %s, true)", (scope.user_id,))
        return {table: conn.execute(sql.SQL('SELECT count(*) FROM mnemotrace.{} WHERE user_id = %s AND space_id = %s')
                                    .format(sql.Identifier(table)), (scope.user_id, scope.space_id)).fetchone()[0]
                for table in tables}


def _delete_scope(memory, accounts, scope):
    if scope == 'clear':
        memory.clear_space()
    elif scope == 'space':
        memory.delete_space()
    else:
        assert accounts.delete_user(memory.context.user_id)


def _delete_in_process(config, admin_config, context, scope, *, ready, resume):
    if scope == 'account':
        with Database(admin_config, purpose='admin') as admin:
            return Accounts(admin).delete_user(context.user_id)
    with Database(config) as runtime:
        _delete_scope(PostgresMemory(runtime, context), None, scope)
    return True


def _wait_for_database_lock(cluster, name):
    import psycopg

    deadline = time.monotonic() + WAIT
    with psycopg.connect(cluster.bootstrap.dsn, autocommit=True) as conn:
        while time.monotonic() < deadline:
            if conn.execute('SELECT 1 FROM pg_stat_activity WHERE application_name = %s '
                            'AND cardinality(pg_blocking_pids(pid)) > 0', (name,)).fetchone():
                return
            time.sleep(0.02)
    pytest.fail('Deletion did not wait on the in-flight write')


@contextmanager
def _blocked_deletion(cluster, context, scope):
    from psycopg.conninfo import make_conninfo

    name = 'stage7-delete-' + context.space_id
    runtime = replace(cluster.runtime, dsn=make_conninfo(cluster.runtime.dsn, application_name=name))
    admin = replace(cluster.admin, dsn=make_conninfo(cluster.admin.dsn, application_name=name))
    with _process(_delete_in_process, runtime, admin, context, scope) as deleting:
        _wait_for_database_lock(cluster, name)
        yield
        assert _result(deleting) is True


@pytest.mark.parametrize('scope', ['clear', 'space', 'account'])
@pytest.mark.parametrize('kind', ['extract', 'core', 'summary', 'vector'])
def test_delete_prevents_late_processing_writes(memory, cluster, runtime, spaces, accounts, scope, kind):
    user_id = memory.context.user_id
    key = accounts.issue_key(user_id)
    sibling = spaces.create(user_id, 'Sibling')
    sibling_memory = PostgresMemory(runtime, spaces.authorize(user_id, sibling.space_id))
    sibling_source = sibling_memory.remember_raw('Sibling synthetic source', 'chat')
    settings = memory.set_settings(extract_instruction='Keep this setting on clear')
    seed = memory.add_fact('Synthetic maintainer', 'works_at', 'Seed lab')
    memory.remember_working('Synthetic state', 'chat')
    assert memory.graph_relations(seed.id)
    operation_id = _pending_write(memory, kind)
    gate = 'model' if kind == 'extract' else 'embedding'

    with _process(_run_stage, cluster.runtime, memory.context, operation_id, gate) as active:
        assert active.ready.wait(WAIT)
        _delete_scope(memory, accounts, scope)
        active.resume.set()
        assert _result(active) == (['extract'] if kind == 'extract' else [])

    assert not any(_scope_counts(runtime, memory.context).values())
    if scope == 'clear':
        assert memory.get_settings() == settings
        assert memory.operation(operation_id) is None
        assert memory.dao.receipt(operation_id) is None
        assert memory.rebuild_graph() is None
        assert memory.graph_relations(seed.id) == ()
    else:
        with pytest.raises(SpaceNotFound if scope == 'space' else AccountNotFound):
            memory.rebuild_graph()
    if scope == 'account':
        with pytest.raises(AuthenticationError):
            spaces.authenticate(key.value)
        assert not any(_scope_counts(runtime, sibling_memory.context).values())
    else:
        assert sibling_memory.get_episode(sibling_source.id) is not None


@pytest.mark.parametrize('scope', ['clear', 'space', 'account'])
def test_delete_waits_for_core_commit_and_removes_its_projection(memory, cluster, runtime, scope):
    operation_id = _pending_write(memory, 'core')
    with _process(_interrupt_transaction, cluster.runtime, memory.context, 'core', False, operation_id) as active:
        assert active.ready.wait(WAIT)
        with _blocked_deletion(cluster, memory.context, scope):
            active.resume.set()
            assert _result(active) is True
    assert not any(_scope_counts(runtime, memory.context).values())


def _rebuild_in_process(config, context, *, ready, resume):
    from mnemotrace.store.postgres.projection import GraphProjection

    prune = GraphProjection.prune

    def pause_after_rebuild(graph):
        prune(graph)
        _pause(ready, resume)

    with Database(config) as runtime, patch.object(GraphProjection, 'prune', pause_after_rebuild):
        return PostgresMemory(runtime, context).rebuild_graph()


def test_clear_waits_for_graph_rebuild_and_removes_rebuilt_edges(memory, cluster, runtime):
    fact = memory.add_fact('Synthetic person', 'works_at', 'Synthetic lab')
    assert memory.graph_relations(fact.id)
    with memory.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = %s', (fact.id,))
    with _process(_rebuild_in_process, cluster.runtime, memory.context) as active:
        assert active.ready.wait(WAIT)
        with _blocked_deletion(cluster, memory.context, 'clear'):
            active.resume.set()
            assert _result(active) is None
    assert not any(_scope_counts(runtime, memory.context).values())


@pytest.mark.parametrize('damage', ['missing', 'invalid-result', 'invalid-reference'])
def test_worker_rejects_corrupt_extraction_checkpoint(memory, cluster, damage):
    from psycopg.types.json import Jsonb
    from mnemotrace.consolidate.pipeline import digest

    operation_id = _pending_write(memory, 'core')
    with memory.dao._transaction() as sql:
        if damage == 'missing':
            sql.delete('operation_products', 'operation_id = %s', (operation_id,))
        elif damage == 'invalid-result':
            result = {'facts': [], 'summaries': {}}
            assert sql.run('UPDATE mnemotrace.operation_products SET result = %s, result_hash = %s '
                           'WHERE user_id = %s AND space_id = %s AND operation_id = %s',
                           (Jsonb(result), digest(result), *sql.scope, operation_id)).rowcount == 1
        else:
            assert sql.run('UPDATE mnemotrace.operation_products SET source_versions = %s '
                           'WHERE user_id = %s AND space_id = %s AND operation_id = %s',
                           (Jsonb([{'source_id': 'unrelated-source', 'content_version': 1}]),
                            *sql.scope, operation_id)).rowcount == 1
    with _process(_run_stage, cluster.runtime, memory.context, operation_id, None) as child:
        assert _result(child) == []
    status = memory.operation(operation_id)
    assert (status.state.value, status.error_code) == ('failed', 'data_corruption')
    assert status.stored_raw and not status.core_committed
    assert len(memory.list_sources()) == 1
    assert memory.facts() == ()
    assert memory.dao.receipt(operation_id) is None


def test_wrong_embedding_dimension_keeps_the_saved_product_for_retry(memory, runtime):
    class InvalidDimension(HashingEmbedder):
        def embed_batch(self, texts):
            return [[1.0] for _ in texts]

    operation_id = _pending_write(memory, 'core')
    invalid = PostgresMemory(runtime, memory.context, embedder=InvalidDimension(), embedding_model='hashing-v1',
                             llm=_model([]))
    assert not invalid.flush_processing('chat')['ok']
    status = memory.operation(operation_id)
    assert (status.state.value, status.error_code) == ('failed', 'embedding_invalid')
    assert status.stored_raw and status.extraction_saved and not status.core_committed
    assert memory.facts() == ()
    assert memory.dao.receipt(operation_id) is None
    assert memory.flush_processing('chat', retry_failed=True)['ok']
    fact, = memory.facts()
    assert memory.graph_relations(fact.id)
