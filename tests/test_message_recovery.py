"""Cross-stage invalidation, standalone concurrency, explicit retry and crash checkpoints."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from mnemotrace import Memory
from mnemotrace._messages import run_one
from mnemotrace.consolidate.messages import MessageTasks, validate_state
from mnemotrace.llm import Completion, FakeLLM
from tests.test_message_pipeline import drain, force, message, reply, services as services


def test_native_concurrent_receive_is_ordered_and_isolated():
    mem = Memory()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: mem.remember_messages([message(str(n))], 's', 'u'), range(12)))
    assert sorted(r['messages'][0]['sequence'] for r in results) == list(range(1, 13))
    assert mem.remember_messages([message('0')], 's', 'other')['messages'][0]['sequence'] == 1
    validate_state(mem._message_state, mem.episodes_doc.values())


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
    assert svc.message_status('u')['jobs'][0]['state'] == 'cancelled'
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert svc.message_status('u')['jobs'][0]['state'] == 'completed'
    assert len(model.calls) == 2


def test_explicit_retry_after_configuration_change(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    svc.config.joint_max_output_tokens += 1
    drain(svc)
    assert svc.message_status('u')['jobs'][0]['state'] == 'failed'
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert svc.message_status('u')['jobs'][0]['state'] == 'completed'


def test_disabled_worker_flush_drives_recovered_messages(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    svc._buffered.close()
    svc._buffered = None
    assert svc.flush('u', 's')['ok']
    assert svc.message_status('u')['jobs'][0]['core'] == 'completed'


def test_core_and_summary_evidence_are_strict_on_restore():
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    assert mem.flush_messages('u')['ok']
    ep = mem.episodes_doc.values()[0]
    del ep.coverage['unit']
    with pytest.raises(ValueError):
        validate_state(mem._message_state, mem.episodes_doc.values())


def test_provider_truncation_does_not_commit_legal_looking_json(services):
    model = FakeLLM(handler=reply)
    def truncated(prompt, system):
        return Completion(reply(prompt, system), finish_reason='length')
    svc, _, _ = services(truncated)
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    assert not svc.get('u').fact_store.values()
    assert svc.message_status('u')['jobs'][0]['error_code'] == 'extraction_truncated'
    assert not model.calls


def test_native_save_failure_recovers_checkpoint_without_regeneration(tmp_path, monkeypatch):
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', 'u')
    mem.save(str(tmp_path / 'store'))
    original = mem.save
    def save(*args, **kwargs):
        row = next(iter(MessageTasks(mem, 'u').state['windows'].values()), None)
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
    validate_state(deepcopy(mem._message_state), mem.episodes_doc.values())


def test_missing_identity_record_cannot_restore_as_a_new_message():
    mem = Memory()
    accepted = mem.remember_messages([message('a')], 's', 'u')
    state = deepcopy(mem._message_state)
    del state['namespaces']['u']['messages'][accepted['messages'][0]['episode_id']]
    with pytest.raises(ValueError):
        validate_state(state, mem.episodes_doc.values())


def test_restore_rejects_model_counter_that_disagrees_with_ledger(services):
    svc, _, _ = services(lambda *args: 'invalid JSON')
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    state = deepcopy(svc.get('u')._message_state)
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
        windows = MessageTasks(mem, 'u').state['windows'].values()
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


def test_joint_backlog_yields_to_ready_original_summary(services):
    from mnemotrace.consolidate.summaries import SummaryTasks
    def respond(prompt, system):
        return 'Synthetic summary of the original' if 'MNEMOTRACE_WINDOW' not in prompt else reply(prompt, system)
    svc, model, _ = services(respond)
    with svc.write_lock('u'):
        mem = svc.get('u')
        ep = mem.add('Synthetic old original. ' * 200, 'u')
        ep.consolidated = True
        mem._sync_episode(ep)
        SummaryTasks(mem, 'u').register([ep], 1000)
        svc._save('u', mem)
    svc.remember_messages('u', [message(str(i), str(i) * 2500) for i in range(6)], 's')
    force(svc)
    for _ in range(3):
        assert svc._buffered.run_one('u')
    assert any('MNEMOTRACE_WINDOW' not in call['prompt'] for call in model.calls)
    assert any(job['core'] == 'pending' for job in svc.message_status('u')['jobs'])


@pytest.mark.parametrize('persisted', [False, True])
def test_native_backend_commit_failure_never_exposes_partial_results(tmp_path, monkeypatch, persisted):
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a', 'Synthetic A'), message('b', 'Synthetic B')], 's', 'u')
    if persisted:
        mem.save(str(tmp_path / 'store'))
    original = mem.engine._write_fact
    written = []
    def partial(fact, stats):
        if written:
            raise OSError('synthetic second backend write failed')
        written.append(fact.id)
        return original(fact, stats)
    monkeypatch.setattr(mem.engine, '_write_fact', partial)
    with pytest.raises(OSError):
        mem.flush_messages('u')
    assert len(written) == 1
    if persisted:
        assert not mem.fact_store.values()
        assert mem.flush_messages('u')['ok']
        assert len(mem.fact_store.values()) == 2 and len(mem.llm.calls) == 1
    else:
        with pytest.raises(RuntimeError, match='checkpoint failed'):
            mem.search('Synthetic A', user_id='u')
        with pytest.raises(RuntimeError, match='checkpoint failed'):
            mem.save(str(tmp_path / 'must-not-save-dirty-state'))
        assert not (tmp_path / 'must-not-save-dirty-state').exists()


@pytest.mark.parametrize('delta', [-1, 1])
def test_bad_embedding_batch_is_rejected_before_any_message_write(monkeypatch, delta):
    mem = Memory()
    original = mem.embedder.embed_batch
    monkeypatch.setattr(mem.embedder, 'embed_batch', lambda texts: original(texts)[:1] if delta < 0
                        else original(texts) + [original(texts)[0]])
    with pytest.raises(ValueError, match='batch length'):
        mem.remember_messages([message('a', 'A'), message('b', 'B')], 's', 'u')
    assert not mem.episodes_doc.values() and mem._message_state is None


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
    validate_state(mem._message_state, mem.episodes_doc.values())


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
    assert [job['core'] for job in restored.message_status('u')['jobs']] == ['completed', 'completed', 'pending']
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
    assert [j['core'] for j in svc.message_status('u')['jobs']] == ['completed', 'pending']


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
    job, = svc.message_status('u')['jobs']
    assert len(model.calls) == 3 and job['core'] == 'completed' and job['summarization'] == 'failed'
    model._handler = reply
    svc.flush('u', 's', timeout=0, retry_failed=True)
    drain(svc)
    assert len(model.calls) == 4 and '本次仅补' in model.calls[-1]['system']
    assert [f.id for f in svc.get('u').fact_store.values()] == [fact_id]
    assert svc.message_status('u')['jobs'][0]['state'] == 'completed'


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
    mem.engine.conflict = ConflictResolver(embedder=object(), sim_threshold=.8)
    for predicate, value, stamp in [('attends_yoga', 'twice weekly', 100), ('does_yoga', 'three times weekly', 200)]:
        fact = Fact(subject='user', predicate=predicate, object=value, user_id='u', valid_at=stamp,
                    created_at=stamp, embedding=[1., 0.], assertion_kind='user_statement', source_roles=['user'])
        mem.engine._write_fact(fact, mem.engine.empty_stats())
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
    from mnemotrace.consolidate.messages import config_identity
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
