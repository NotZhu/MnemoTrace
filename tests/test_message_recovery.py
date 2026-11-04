"""Cross-stage invalidation, standalone concurrency, explicit retry and crash checkpoints."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from mnemotrace import Memory
from mnemotrace._pipeline import run_one
from mnemotrace.consolidate.pipeline import PipelineTasks, validate_state
from mnemotrace.llm import Completion, FakeLLM
from tests.test_message_pipeline import drain, force, message, reply, services as services


def test_native_concurrent_receive_is_ordered_and_isolated():
    mem = Memory()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: mem.remember_messages([message(str(n))], 's', 'u'), range(12)))
    assert sorted(r['messages'][0]['sequence'] for r in results) == list(range(1, 13))
    assert mem.remember_messages([message('0')], 's', 'other')['messages'][0]['sequence'] == 1
    validate_state(mem._pipeline_state, mem.episodes_doc.values())


def test_native_model_allows_append_and_close_prevents_late_commit():
    entered, release = threading.Event(), threading.Event()
    def respond(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    mem = Memory(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('a')], 's', 'u')
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(mem.flush_messages, 'u', 's', .2)
        assert entered.wait(5)
        appended = pool.submit(mem.remember_messages, [message('b')], 's', 'u')
        assert appended.result(5)['messages'][0]['sequence'] == 2
        mem.close()
        release.set()
        assert not work.result(5)['ok']
    assert not mem.fact_store.values()


def test_source_time_comes_from_matching_role():
    def respond(prompt, system):
        data = json.loads(prompt.split('MNEMOTRACE_WINDOW\n', 1)[1])
        return json.dumps(dict(facts=[dict(subject='user', predicate='lives_in', object='Synthetic city',
            text='Synthetic city', assertion_kind='user_statement', source_ids=[s['id'] for s in data['pending']])],
            working=[], summaries=[]))
    mem = Memory(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('u', event_time=None), message('a', role='assistant', event_time=200)], 's', 'u')
    assert mem.flush_messages('u')['ok']
    assert mem.fact_store.values()[0].valid_at is None


def test_explicit_retry_reprepares_after_manual_change(services):
    svc, model, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    svc.add_fact('u', 'user', 'owns', 'manual')
    assert svc.processing_status('u')['jobs'][0]['state'] == 'cancelled'
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert svc.processing_status('u')['jobs'][0]['state'] == 'completed'
    assert len(model.calls) == 2


def test_explicit_retry_after_configuration_change(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    svc.config.joint_max_output_tokens += 1
    drain(svc)
    assert svc.processing_status('u')['jobs'][0]['state'] == 'failed'
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert svc.processing_status('u')['jobs'][0]['state'] == 'completed'


def test_reembedding_does_not_authorize_generation_from_an_obsolete_policy():
    from mnemotrace._pipeline import memory_adapter
    from mnemotrace.embed import HashingEmbedder
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    host = memory_adapter(mem, 'u')
    PipelineTasks(mem, 'u').force()
    assert run_one(host, 'u')
    window, = PipelineTasks(mem, 'u').state['windows'].values()
    assert window['phase'] == 'embed'
    mem.policy['extract_instruction'] = 'Synthetic changed extraction policy'
    mem.reembed(HashingEmbedder(32))
    assert run_one(host, 'u')
    assert not mem.fact_store.values() and len(mem.llm.calls) == 1
    assert window['state'] == 'failed' and window['error_code'] == 'configuration_changed'


def test_disabled_worker_flush_drives_recovered_messages(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    svc._pipeline_worker.close()
    svc._pipeline_worker = None
    assert svc.flush('u', 's')['ok']
    assert svc.processing_status('u')['jobs'][0]['core'] == 'completed'


def test_core_and_summary_evidence_are_strict_on_restore():
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    assert mem.flush_messages('u')['ok']
    ep = mem.episodes_doc.values()[0]
    del ep.coverage['unit']
    with pytest.raises(ValueError):
        validate_state(mem._pipeline_state, mem.episodes_doc.values())


def test_provider_truncation_does_not_commit_legal_looking_json(services):
    def truncated(prompt, system):
        return Completion(reply(prompt, system), finish_reason='length')
    # Bind the model the service actually uses; the old test asserted on a detached FakeLLM,
    # so `assert not model.calls` was a tautology that never watched the real call path.
    svc, model, _ = services(truncated)
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    assert not svc.get('u').fact_store.values()
    assert svc.processing_status('u')['jobs'][0]['error_code'] == 'extraction_truncated'
    assert model.calls and model.calls[-1]['prompt']  # truncation was actually produced by the real model


def test_native_save_failure_recovers_checkpoint_without_regeneration(tmp_path, monkeypatch):
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    mem.save(str(tmp_path / 'store'))
    original = mem.save
    def save(*args, **kwargs):
        row = next(iter(PipelineTasks(mem, 'u').state['windows'].values()), None)
        if row and row['core_saved']:
            raise OSError('synthetic failed commit')
        return original(*args, **kwargs)
    monkeypatch.setattr(mem, 'save', save)
    with pytest.raises(OSError):
        mem.flush_messages('u')
    assert not mem.fact_store.values()
    monkeypatch.setattr(mem, 'save', original)
    assert mem.flush_messages('u')['ok']
    assert len(mem.llm.calls) == 1


def test_manual_fact_keeps_authority_over_dialogue_fact():
    mem = Memory(llm=FakeLLM(handler=reply))
    old = mem.add_fact('user', 'lives_in', 'A', 'u')
    old.source, old.assertion_kind, old.valid_at = 'extracted', 'user_statement', 100
    mem.fact_store.upsert(old.id, old.embedding, old)
    new = mem.add_fact('user', 'lives_in', 'B', 'u', valid_at=200)
    assert not mem.fact_store.get(old.id).is_live() and new.source == 'user'


def test_long_history_adapter_keeps_global_positional_ids():
    mem = Memory()
    rows = [dict(role='user', content='Synthetic repeated content') for _ in range(1001)]
    result = mem.import_messages([dict(session_id='s', messages=rows)], consolidate=False)
    assert result['episodes'] == 1001
    assert len({e.message_id for e in mem.episodes_doc.values()}) == 1001
    validate_state(deepcopy(mem._pipeline_state), mem.episodes_doc.values())


def test_missing_identity_record_cannot_restore_as_a_new_message():
    mem = Memory()
    accepted = mem.remember_messages([message('a')], 's', 'u')
    state = deepcopy(mem._pipeline_state)
    del state['namespaces']['u']['sources'][accepted['messages'][0]['episode_id']]
    with pytest.raises(ValueError):
        validate_state(state, mem.episodes_doc.values())


def test_restore_rejects_model_counter_that_disagrees_with_ledger(services):
    svc, _, _ = services(lambda *args: 'invalid JSON')
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    state = deepcopy(svc.get('u')._pipeline_state)
    next(iter(state['namespaces']['u']['windows'].values()))['attempts'] = 0
    with pytest.raises(ValueError):
        validate_state(state)


def test_native_missing_model_fails_without_waiting_for_retry():
    mem = Memory()
    mem.remember_messages([message('a')], 's', 'u')
    result = mem.flush_messages('u', timeout=.1)
    assert result['status'] == 'failed'
    assert result['jobs'][0]['attempts'] == 1
    assert result['jobs'][0]['error_code'] == 'llm_not_configured'


@pytest.mark.parametrize('storage', ['memory', 'durable', 'lancedb', 'qdrant'])
def test_saved_dialogue_reembeds_pending_vectors_and_recovers(tmp_path, monkeypatch, storage):
    from mnemotrace import Config
    from mnemotrace.embed import HashingEmbedder
    if storage in {'lancedb', 'qdrant'}:
        pytest.importorskip('qdrant_client' if storage == 'qdrant' else storage)
    path = str(tmp_path / 'store')
    model = FakeLLM(handler=reply)
    mem = Memory.open(path, config=Config(storage=storage), embedder=HashingEmbedder(16), llm=model)
    mem.remember_messages([message('a', 'long synthetic ' * 200)], 's', 'u')
    mem.save()
    original = mem.save
    def interrupt(*args, **kwargs):
        windows = PipelineTasks(mem, 'u').state['windows'].values()
        if any(w['core_saved'] for w in windows):
            raise OSError('synthetic interruption at commit')
        return original(*args, **kwargs)
    monkeypatch.setattr(mem, 'save', interrupt)
    with pytest.raises(OSError):
        mem.flush_messages('u')
    assert not mem.fact_store.values() and len(model.calls) == 1
    monkeypatch.setattr(mem, 'save', original)
    mem.reembed(HashingEmbedder(32))
    mem.save()
    mem.close()
    with Memory.open(path, config=Config(storage=storage), embedder=HashingEmbedder(32), llm=model) as restored:
        assert restored.flush_messages('u')['ok']
        assert len(model.calls) == 1
        assert all(len(f.embedding) == 32 for f in restored.fact_store.values())
        episode, = restored.episodes_doc.values()
        assert episode.summary and len(episode.summary_embedding) == 32
        assert restored.get_episode(episode.id, 'u')['content'] == 'long synthetic ' * 200



def test_close_uses_joint_results_and_preserves_raw_import_intent(services):
    svc, model, _ = services()
    svc.remember_messages('u', [message('a', 'Synthetic durable record ' * 100)], 's')
    result = svc.close_session('u', 's')
    assert result['ok'] and result['pending_consolidated'] == 1
    assert result['facts_added'] == 1 and result['summaries'] == 1 and result['outcomes'] == 0
    assert len(model.calls) == 1
    svc.import_('u', sessions=[dict(session_id='raw', messages=[message('raw')])], consolidate=False)
    result = svc.close_session('u', 'raw')
    assert result['ok'] and result['pending_consolidated'] == 0 and result['facts_added'] == 0
    assert len(model.calls) == 1


def test_native_receive_backend_failure_restores_before_replay(tmp_path, monkeypatch):
    mem = Memory.open(str(tmp_path / 'store'))
    mem.add_fact('user', 'owns', 'Synthetic existing', user_id='u')
    mem.save()
    original = mem._sync_episode
    written = []
    def partial(episode):
        original(episode)
        written.append(episode.id)
        if len(written) == 2:
            raise OSError('synthetic second original write failed')
    monkeypatch.setattr(mem, '_sync_episode', partial)
    rows = [message('a'), message('b')]
    with pytest.raises(OSError):
        mem.remember_messages(rows, 's', 'u')
    assert not mem.episodes_doc.values() and len(mem.fact_store.values()) == 1
    monkeypatch.setattr(mem, '_sync_episode', original)
    assert mem.remember_messages(rows, 's', 'u')['accepted'] == 2
    validate_state(mem._pipeline_state, mem.episodes_doc.values())


def test_following_trigger_survives_restart_without_capturing_untriggered_message(services, tmp_path):
    svc, first_model, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    svc.remember_messages('u', [message('b')], 's')
    force(svc)
    svc.remember_messages('u', [message('c')], 's')
    svc.close()
    restored, model, _ = services(path=tmp_path)
    drain(restored)
    assert [job['core'] for job in restored.processing_status('u')['jobs']] == ['completed', 'completed', 'pending']
    assert len(first_model.calls) == 1 and len(model.calls) == 1


def test_close_clears_only_its_frozen_working_range(services):
    entered, release = threading.Event(), threading.Event()
    def respond(prompt, system):
        entered.set()
        assert release.wait(5)
        data = json.loads(prompt.split('MNEMOTRACE_WINDOW\n', 1)[1])
        result = json.loads(reply(prompt, system))
        result['working'] = [dict(content='Synthetic frozen task', kind='task', assertion_kind='user_statement',
                                  source_ids=[data['pending'][0]['id']])]
        return json.dumps(result)
    svc, _, _ = services(respond)
    svc.remember_messages('u', [message('a')], 's')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(svc.close_session, 'u', 's')
        assert entered.wait(5)
        svc.remember_messages('u', [message('b')], 's')
        svc.add_working('u', 'Synthetic later working', 's')
        release.set()
        result = future.result(5)
    assert result['ok'] and result['working_cleared'] == 1
    assert [w['content'] for w in svc.working_memory('u', 's')['items']] == ['Synthetic later working']
    assert [j['core'] for j in svc.processing_status('u')['jobs']] == ['completed', 'pending']


def test_three_calls_shared_by_core_and_summary_then_retry_only_missing(services):
    def missing(prompt, system):
        if '本次仅补' in system:
            return 'invalid JSON'
        result = json.loads(reply(prompt, system))
        result['summaries'] = []
        return json.dumps(result)
    svc, model, stamp = services(missing)
    svc.remember_messages('u', [message('a', 'Synthetic long ' * 200)], 's')
    force(svc)
    drain(svc)
    fact_id = svc.get('u').fact_store.values()[0].id
    for delay in (30, 120):
        stamp[0] += delay
        drain(svc)
    job, = svc.processing_status('u')['jobs']
    assert len(model.calls) == 3 and job['core'] == 'completed' and job['summarization'] == 'failed'
    model._handler = reply
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert len(model.calls) == 4 and '本次仅补' in model.calls[-1]['system']
    assert [f.id for f in svc.get('u').fact_store.values()] == [fact_id]
    assert svc.processing_status('u')['jobs'][0]['state'] == 'completed'


def test_late_unknown_and_older_history_cannot_replace_newer_source():
    def respond(prompt, system):
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        return json.dumps(result)
    mem = Memory(llm=FakeLLM(handler=respond))
    for mid, stamp, mode in [('new', 200, 'live'), ('unknown', None, 'historical'), ('old', 100, 'historical')]:
        mem.remember_messages([message(mid, mid, event_time=stamp)], 's', 'u', mode=mode)
        assert mem.flush_messages('u')['ok']
    latest = [f.object for f in mem.fact_store.values() if f.is_live()]
    assert latest == ['new']


def test_native_flush_rejects_snapshot_changed_at_save_boundary(tmp_path, monkeypatch):
    from mnemotrace.store import PersistenceError
    path = str(tmp_path / 'store')
    mem = Memory.open(path, llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    mem.save()
    original, intervened = mem.save, []
    def interleave(*args, **kwargs):
        if not intervened:
            with Memory.open(path) as other:
                other.add_fact('user', 'owns', 'Synthetic concurrent fact', user_id='u')
                other.save()
            intervened.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(mem, 'save', interleave)
    with pytest.raises(PersistenceError):
        mem.flush_messages('u')
    assert [f.object for f in mem.fact_store.values()] == ['Synthetic concurrent fact']
    assert len(mem.episodes_doc.values()) == 1 and not mem.llm.calls


def test_portable_summary_evidence_and_equal_content_events_are_preserved(services):
    svc, _, _ = services()
    content = 'Synthetic repeated original ' * 200
    svc.remember_messages('u', [message('a', content), message('b', content)], 's', processing='immediate')
    snapshot = svc.export('u', include_sensitive=True)
    target = Memory()
    assert target.import_snapshot(snapshot, user_id='u')['episodes_restored'] == 2
    assert len(target.episodes_doc.values()) == 2
    assert all(e.coverage and e.summary for e in target.episodes_doc.values())
    corrupt = deepcopy(snapshot)
    corrupt['episodes'][0]['coverage']['ranges'] = [[0, len(content) + 1]]
    with pytest.raises(ValueError):
        Memory().import_snapshot(corrupt, user_id='u')


@pytest.mark.parametrize('arrivals,expected', [
    ([('A', 100), ('C', 300), ('B', 200), ('Z', 50)], [('Z', 50, 100), ('A', 100, 200), ('B', 200, 300), ('C', 300, None)]),
    ([('A', 300), ('B', 200), ('A', 100)], [('A', 100, 200), ('B', 200, 300), ('A', 300, None)]),
])
def test_out_of_order_history_has_disjoint_intervals_and_a_complete_chain(arrivals, expected):
    def respond(prompt, system):
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        return json.dumps(result)
    mem = Memory(llm=FakeLLM(handler=respond))
    for index, (value, stamp) in enumerate(arrivals):
        mem.remember_messages([message(str(index), value, event_time=stamp)], 's', 'u', mode='historical')
        assert mem.flush_messages('u')['ok']
    facts = sorted(mem.fact_store.values(), key=lambda f: f.valid_at)
    assert [(f.object, f.valid_at, f.invalid_at) for f in facts] == expected
    assert [f.supersedes for f in facts] == [None, *[f.id for f in facts[:-1]]]
    assert [f.object for f in facts if f.is_live(250)] == ['B']
    relations = {r.fact_id: r for r in mem.graph.relations()}
    assert all((relations[f.id].valid_at, relations[f.id].invalid_at) == (f.valid_at, f.invalid_at) for f in facts)


def test_service_save_cas_rejects_native_intervention(services, monkeypatch):
    from mnemotrace.store import PersistenceError
    svc, _, _ = services()
    svc.add_fact('u', 'user', 'owns', 'Synthetic initial')
    mem = svc.get('u')
    original = mem.save
    def interleave(*args, **kwargs):
        with Memory.open(svc._path('u')) as other:
            other.add_fact('user', 'owns', 'Synthetic native intervention', user_id='u')
            other.save()
        return original(*args, **kwargs)
    monkeypatch.setattr(mem, 'save', interleave)
    with pytest.raises(PersistenceError):
        svc.remember_messages('u', [message('a')], 's')
    assert not svc.get('u').episodes_doc.values()
    assert {f.object for f in svc.get('u').fact_store.values()} == {'Synthetic initial', 'Synthetic native intervention'}


def test_new_dialogue_slot_keeps_semantic_reconciliation():
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact
    mem = Memory()
    mem.writer.conflict = ConflictResolver(embedder=object(), sim_threshold=.8)
    for predicate, value, stamp in [('attends_yoga', 'twice weekly', 100), ('does_yoga', 'three times weekly', 200)]:
        fact = Fact(subject='user', predicate=predicate, object=value, user_id='u', valid_at=stamp,
                    created_at=stamp, embedding=[1., 0.], assertion_kind='user_statement', source_roles=['user'])
        mem.writer._write_fact(fact, mem.writer.empty_stats())
    assert [f.object for f in mem.fact_store.values() if f.is_live()] == ['three times weekly']


def test_equal_time_facts_commit_in_source_sequence_not_model_order():
    def respond(prompt, system):
        result = json.loads(reply(prompt, system))
        for fact in result['facts']:
            fact['predicate'] = 'lives_in'
        result['facts'].reverse()
        return json.dumps(result)
    mem = Memory(llm=FakeLLM(handler=respond))
    mem.remember_messages([message('a', 'old', event_time=100), message('b', 'new', event_time=100)], 's', 'u')
    assert mem.flush_messages('u')['ok']
    assert [f.object for f in mem.fact_store.values() if f.is_live()] == ['new']


def test_open_waits_for_the_complete_snapshot_generation(tmp_path, monkeypatch):
    from concurrent.futures import TimeoutError as FutureTimeout
    from mnemotrace.store import persist
    path = str(tmp_path / 'store')
    mem = Memory.open(path)
    mem.add('Synthetic old episode')
    mem.add_fact('user', 'owns', 'Synthetic old fact')
    mem.save()
    mem.add('Synthetic new episode')
    mem.add_fact('user', 'owns', 'Synthetic new fact')
    entered, release = threading.Event(), threading.Event()
    original = persist._write_jsonl
    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == 'episodes':
            entered.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(persist, '_write_jsonl', paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(mem.save)
        assert entered.wait(5)
        reader = pool.submit(Memory.open, path)
        try:
            with pytest.raises(FutureTimeout):
                reader.result(.1)
        finally:
            release.set()
        writer.result(5)
        with reader.result(5) as restored:
            assert len(restored.episodes_doc.values()) == 2
            assert len(restored.fact_store.values()) == 2


def test_cached_model_configuration_identity_tracks_the_inner_provider(tmp_path):
    from mnemotrace.llm.cache import CachedLLM
    from mnemotrace.consolidate.pipeline import config_identity
    model = FakeLLM(handler=reply)
    model.model = 'synthetic-model-A'
    mem = Memory(llm=CachedLLM(model, str(tmp_path / 'cache'), model_tag='synthetic-tag'))
    before = config_identity(mem)
    model.model = 'synthetic-model-B'
    assert config_identity(mem) != before


def test_dialogue_fact_retains_sensitive_classification_and_safe_export(services):
    def respond(prompt, system):
        result = json.loads(reply(prompt, system))
        result['facts'][0].update(predicate='has_disease', object='Synthetic diabetes condition', topic='health')
        return json.dumps(result)
    svc, _, _ = services(respond)
    svc.remember_messages('u', [message('health', 'Synthetic health statement')], 's', processing='immediate')
    fact, = svc.get('u').fact_store.values()
    assert fact.sensitive and fact.category == '健康' and fact.topic == 'health' and fact.topic_source == 'rule'
    assert svc.export('u')['facts'] == []
    assert svc.export('u', include_sensitive=True)['facts'][0]['sensitive']


def test_watcher_history_preserves_reactivated_live_working(services, monkeypatch):
    from mnemotrace.connectors import watch
    from mnemotrace.connectors.base import ImportMessage, ImportSession
    svc, model, _ = services()
    svc.add_working('u', 'Synthetic reactivated live task', 's')
    session = ImportSession(session_id='s', messages=[ImportMessage('Synthetic historical note', message_id='old')])
    monkeypatch.setattr(watch, 'load_sessions', lambda *args, **kwargs: [session])
    def post(base, key, path, body, timeout):
        if path == '/v1/import':
            return svc.import_('u', sessions=body['sessions'], consolidate=body.get('consolidate'))
        return svc.close_session('u', **body)
    monkeypatch.setattr(watch, '_post', post)
    assert watch.ingest('http://synthetic.invalid', 'synthetic-key', ['synthetic-file'])['closed_ok'] == ['s']
    assert [w['content'] for w in svc.working_memory('u', 's')['items']] == ['Synthetic reactivated live task']
    assert len(model.calls) == 1

def test_conflict_cancelled_requires_every_job_to_be_conflict():
    """#16：_conflict_cancelled 的混合失败分支必须有测试。

    过去把内部的 all 改成 any，回归仍全绿；这里直接锁定“只要有一个非冲突失败
    （如 embedding_failed）就不是良性冲突”的行为。"""
    from mnemotrace._pipeline import _conflict_cancelled
    only_conflict = dict(jobs=[dict(state='failed', error_code='manual_change'),
                               dict(state='cancelled', error_code='source_changed')])
    assert _conflict_cancelled(only_conflict) is True
    mixed = dict(jobs=[dict(state='failed', error_code='manual_change'),
                       dict(state='failed', error_code='embedding_failed')])
    assert _conflict_cancelled(mixed) is False
    single_non_conflict = dict(jobs=[dict(state='failed', error_code='extraction_failed')])
    assert _conflict_cancelled(single_non_conflict) is False
    assert _conflict_cancelled(dict(jobs=[])) is False


def test_execute_sources_retries_conflict_only_once(services, monkeypatch):
    """#17：execute_sources 的冲突重试次数必须有断言（防止 range(2) 悄悄变成更多次），
    且持续冲突最终必须抛 ExtractionConflictError 而非普通失败。"""
    from mnemotrace.extraction import ExtractionConflictError
    import mnemotrace._pipeline as pipeline
    svc, _, _ = services()
    attempts = {'n': 0}

    def fake_flush(host, user, *args, **kwargs):
        attempts['n'] += 1
        # 每次都报告为并发写取消，使循环走到上限
        return dict(ok=False, status='failed', jobs=[
            dict(episode_id='e1', state='cancelled', error_code='manual_change')])

    monkeypatch.setattr(pipeline, 'flush', fake_flush)
    with pytest.raises(ExtractionConflictError):
        pipeline.execute_sources(svc, 'u', [], 'extract')
    assert attempts['n'] == 2  # 原始 + 一次冲突重试；改成 range(3) 会变 3


def test_flush_discovers_namespaces_without_user(services, monkeypatch):
    """#19：无参 flush() 的命名空间发现分支过去全仓无测试。

    这里断言：discover_processing_namespaces 返回的每个命名空间都被逐个 flush，
    且聚合状态由子结果推导（全部 completed 才 ok）。"""
    import mnemotrace.store.persist as persist
    from mnemotrace.service import MemoryService
    svc, _, _ = services()
    monkeypatch.setattr(persist, 'discover_processing_namespaces',
                        lambda data_dir, on_error=None: [('u', True), ('v', True)])
    called = []
    def spy(user=None, *args, **kwargs):
        called.append(user)
        return dict(ok=True, status='completed', session_id=None, generation=None,
                    counts={'pending': 0}, jobs=[])
    # 只替换实例方法，再用未绑定的真实实现调用外层，确保真正走命名空间发现分支。
    monkeypatch.setattr(svc, 'flush', spy)
    result = MemoryService.flush(svc, None)
    assert called == ['u', 'v']           # 两个命名空间都被发现并 flush
    assert result['status'] == 'completed' and result['ok'] is True
    assert len(result['results']) == 2


def test_extraction_conflict_importable_and_carries_ids():
    """#19：409 extraction_conflict 的对外契约要有守护——异常可从包根导入、列入 __all__、
    且带 stored_raw/episode_ids。仅断言可导入不足：删掉 __all__ 条目时模块属性仍在，
    契约已经偏离但测试仍绿。"""
    import mnemotrace
    from mnemotrace import ExtractionConflictError
    assert mnemotrace.ExtractionConflictError is ExtractionConflictError
    assert 'ExtractionConflictError' in mnemotrace.__all__
    exc = ExtractionConflictError(stored_raw=True, episode_ids=['e1', 'e2'])
    assert exc.code == 'extraction_conflict' and exc.status_code == 409
    assert exc.detail() == {'code': 'extraction_conflict', 'message': exc.message,
                            'stored_raw': True, 'episode_ids': ['e1', 'e2']}

def test_raise_for_failed_result_tolerates_core_success_aux_failure():
    """核心事实已落库、仅摘要等辅助步骤失败时，不应抛错（与 execute_sources 一致），
    否则 immediate 客户端会误以为整批失败而重试，可能重复写入。"""
    from mnemotrace._pipeline import raise_for_failed_result, extract_partial_success
    # core 完成、摘要失败
    partial = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='completed', summarization='failed', state='failed',
             error_code='summary_invalid_response')])
    assert extract_partial_success(partial, {'a'}) is True
    raise_for_failed_result(partial, {'a'})  # 不抛
    # 核心本身失败 -> 必须抛
    core_failed = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='failed', summarization='pending', state='failed',
             error_code='extraction_failed')])
    assert extract_partial_success(core_failed, {'a'}) is False
    with pytest.raises(Exception):
        raise_for_failed_result(core_failed, {'a'})
    # 辅助失败但核心未全部完成（缺 id）-> 抛
    incomplete = dict(ok=False, status='failed', jobs=[
        dict(episode_id='a', core='completed', summarization='failed', state='failed',
             error_code='summary_invalid_response')])
    assert extract_partial_success(incomplete, {'a', 'b'}) is False
    with pytest.raises(Exception):
        raise_for_failed_result(incomplete, {'a', 'b'})


def test_immediate_messages_core_success_summary_failure_does_not_raise(services, monkeypatch):
    """immediate 且核心事实已落库、仅摘要失败时，/v1/messages 不应抛错（facts 已成功）。"""
    import mnemotrace._pipeline as pipeline
    svc, _, _ = services()
    # 伪造一个“部分成功”的 flush 结果
    def fake_flush(host, user, *args, **kwargs):
        real_ids = list(kwargs.get('boundary') or [])
        return dict(ok=False, status='failed', session_id=None, generation='g',
                    counts={'failed': 1}, jobs=[
                        dict(episode_id=real_ids[0], core='completed', summarization='failed',
                             state='failed', error_code='summary_invalid_response')])
    monkeypatch.setattr(pipeline, 'flush', fake_flush)
    result = svc.remember_messages('u', [message('a')], 's', processing='immediate')
    assert result['ok'] is True  # 不因辅助失败而整批报错
    assert result['processing']['ok'] is False  # 但仍如实反映 processing 失败明细
