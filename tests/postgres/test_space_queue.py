from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from mnemotrace.store.postgres.errors import MessageConflict
from tests.postgres.test_processing import dialogue, recorded_digests


def test_remember_accepts_fixed_batch_and_replays_result(processing):
    memory, calls = processing(handle=lambda data: dict(facts=[], digests=recorded_digests(data)))
    result = memory.remember('Tea' * 900, 'chat', message_id='first', wait=True, timeout=0)
    operation_id = result['task'].operation_id
    assert result['status'] == 'timed_out'
    assert result['messages'][0]['sequence'] == 0
    assert calls == []
    assert memory._processing.run_next_stage(operation_id)
    replay = memory.remember((dialogue('first', 'Tea' * 900, event_time=None),), 'chat')
    assert replay['status'] == 'completed' and replay['operation_id'] == operation_id
    assert replay['source_ids'] == result['source_ids']
    assert memory.get_episode(result['source_ids'][0]).digest
    with pytest.raises(MessageConflict):
        memory.remember((dialogue('first', 'Tea' * 900, event_time=None), dialogue('extra')), 'chat')
    assert len(memory.list_sources()) == 1 and len(calls) == 1


def test_space_serializes_cross_session_work_and_releases_during_backoff(processing):
    started = Event()
    release = Event()

    def extract(data):
        if data['pending'][0]['message_id'] == 'first':
            started.set()
            assert release.wait(5)
            return dict(facts=[{}], digests=[])
        return dict(facts=[], digests=[])

    memory, calls = processing(handle=extract)
    first = memory.remember('Tea', 'chat', message_id='first')['operation_id']
    second = memory.remember('Water', 'other', message_id='second')['operation_id']
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(memory._processing.run_next_stage, first)
        try:
            assert started.wait(5)
            assert not memory._processing.run_next_stage(second)
        finally:
            release.set()
        assert running.result()
    assert memory.operation(first).state.value == 'retry_wait'
    assert memory._processing.run_next_stage(second)
    assert memory.operation(second).state.value == 'completed'
    assert len(calls) == 2


def test_context_is_the_most_recent_ten_original_messages(processing):
    def extract(data):
        if data['pending'][0]['message_id'] == '0':
            return dict(facts=[{}], digests=[])
        return dict(facts=[], digests=recorded_digests(data))

    memory, calls = processing(handle=extract)
    old = [dialogue(str(i), f'Saved message {i}') for i in range(12)]
    earlier = memory.remember(old, 'chat')
    memory._store_originals([dialogue('archive')], 'chat')
    later = memory.remember('New message', 'chat', message_id='last')
    assert memory._processing.run_next_stage(earlier['operation_id'])
    assert memory.operation(earlier['operation_id']).state.value == 'retry_wait'
    assert memory._processing.run_next_stage(later['operation_id'])
    assert [item['message_id'] for item in calls[-1]['context']] == [str(i) for i in range(2, 12)]
    assert all(item['representation'] == 'raw' for item in calls[-1]['context'])


def test_worker_publishes_two_spaces_in_parallel(processing, cluster, runtime, spaces, user):
    from threading import Barrier

    from mnemotrace.config import Config
    from mnemotrace.memory import Memory
    from mnemotrace.store.postgres import Database
    from mnemotrace.worker import run_worker

    both_spaces = Barrier(2)

    def extract(data):
        both_spaces.wait(5)
        return dict(facts=[], digests=recorded_digests(data))

    first, calls = processing(handle=extract)
    second_space = spaces.authorize(user, spaces.create(user, 'Another space').space_id)
    model = first._processing.llm
    second = Memory(runtime, second_space, llm=model)
    first_id = first.remember('First', 'chat')['operation_id']
    second_id = second.remember('Second', 'chat')['operation_id']
    with Database(cluster.discovery, purpose='discovery') as discovery:
        run_worker(discovery, runtime, Config(), model, None, None, once=True)
    assert first.operation(first_id).core_committed and second.operation(second_id).core_committed
    assert len(calls) == 2


def test_expired_worker_cannot_publish_and_restart_reuses_valid_bundle(processing, cluster, monkeypatch):
    from mnemotrace.store.postgres import Database
    from mnemotrace.store.postgres.errors import VersionConflict

    class WorkerInterrupted(BaseException):
        pass

    memory, calls = processing()
    accepted = memory.remember('Synthetic tea preference ' * 100, 'chat')
    task_id = accepted['operation_id']
    source_id, = accepted['source_ids']
    captured = []

    def interrupt(conditions, *_):
        captured.append(conditions.lease)
        raise WorkerInterrupted()

    monkeypatch.setattr(memory._processing, '_commit_core', interrupt)
    with pytest.raises(WorkerInterrupted):
        memory._processing.run_next_stage(task_id)
    assert memory.operation(task_id).extraction_saved
    assert not memory.operation(task_id).core_committed
    assert len(calls) == 1 and not memory.facts()
    assert not memory.get_episode(source_id).digest
    with memory.dao._transaction() as sql:
        sql.run("UPDATE mnemotrace.operations SET lease_until = clock_timestamp() - interval '1 second' "
                "WHERE operation_id = %s", (task_id,))
    with Database(cluster.runtime) as reopened:
        restored, resume_calls = processing(database=reopened)
        assert restored._processing.run_next_stage(task_id)
        assert resume_calls == []
        assert restored.operation(task_id).core_committed
        assert restored.get_episode(source_id).digest
        assert len(restored.facts()) == 1
    with pytest.raises(VersionConflict):
        memory._processing.tasks.renew(captured[0])


def test_http_wait_timeout_and_exact_replay_of_published_task(product_client, product_service, product_scope):
    scope, _ = product_scope('space-queue-http')
    headers = {'X-Synthetic-Scope': 'space-queue-http'}
    messages = [dict(message_id='stable', role='user', content='I live in Shenzhen.')]
    body = dict(session_id='chat', messages=messages, wait=True, timeout=0)
    response = product_client.post('/v1/remember', headers=headers, json=body)
    assert response.status_code == 202
    accepted = response.json()
    assert accepted['status'] == 'timed_out' and accepted['messages'][0]['sequence'] == 0
    operation_id = accepted['task']['operation_id']
    endpoint = f'/v1/operations/{operation_id}'
    assert product_client.get(endpoint, headers=headers).json()['source_ids'] == accepted['source_ids']
    assert product_service.memory(scope)._processing.run_next_stage(operation_id)
    replay = product_client.post('/v1/remember', headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()['source_ids'] == accepted['source_ids']
    assert replay.json()['task']['operation_id'] == operation_id
    assert product_client.get(endpoint, headers=headers).json()['core_committed']


def test_earlier_backoff_task_precedes_a_later_expired_lease(processing, cluster):
    from mnemotrace.store.postgres import Database
    from mnemotrace.store.postgres.errors import VersionConflict

    def extract(data):
        if len(calls) == 1:
            return dict(facts=[{}], digests=[])
        return dict(facts=[], digests=[])

    memory, calls = processing(handle=extract)
    first = memory.remember('First', 'chat')['operation_id']
    later = memory.remember('Later', 'other')['operation_id']
    pipeline = memory._processing
    assert pipeline.run_next_stage(first)
    old_lease, _ = pipeline.tasks.claim(later)
    with memory.dao._transaction() as sql:
        past = sql.run("SELECT clock_timestamp() - interval '1 second'").fetchone()[0]
        sql.update('operations', {'next_attempt_at': past}, 'operation_id = %s', (first,))
        sql.update('operations', {'lease_until': past}, 'operation_id = %s', (later,))
    with Database(cluster.discovery, purpose='discovery') as discovery:
        candidates = discovery.discover_processing()
        assert (memory.context, first) in candidates and (memory.context, later) not in candidates
        assert pipeline.run_next_stage(first)
        assert (memory.context, later) in discovery.discover_processing()
    assert memory.operation(first).core_committed
    with pytest.raises(VersionConflict):
        pipeline.tasks.renew(old_lease)
    assert pipeline.run_next_stage(later)
    assert memory.operation(later).core_committed
def test_auxiliary_budget_failure_keeps_bundle_for_retry(processing):
    memory, calls = processing()
    pipeline = memory._processing
    pipeline.llm.context_window = 223
    accepted = memory.remember("Synthetic tea preference. " * 100, "chat")
    task_id = accepted["operation_id"]
    source_id, = accepted["source_ids"]

    assert pipeline.run_next_stage(task_id)
    failed = memory.operation(task_id)
    assert (failed.state.value, failed.error_code) == ("failed", "model_budget_exceeded")
    assert failed.extraction_saved and not failed.core_committed
    source = memory.get_episode(source_id)
    assert not source.consolidated and source.digest == "" and memory.facts() == ()

    pipeline.llm.context_window = 8192
    memory.retry_operation(task_id)
    assert pipeline.run_next_stage(task_id)
    assert memory.operation(task_id).core_committed
    assert memory.get_episode(source_id).digest == "Recorded preference"
    assert len(memory.facts()) == len(calls) == 1
