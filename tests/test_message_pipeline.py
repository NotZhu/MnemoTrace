"""Synthetic end-to-end dialogue acceptance, checkpoints and bounded model calls."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from mnemotrace import Memory
from mnemotrace._buffered import BufferedExtraction
from mnemotrace._messages import run_one
from mnemotrace.consolidate.messages import MessageConflictError, validate_state
from mnemotrace.consolidate.windows import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM
from mnemotrace.service import MemoryService


def message(mid, content='Synthetic item', role='user', event_time=100):
    return dict(message_id=mid, content=content, role=role, event_time=event_time)


def reply(prompt, system):
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
    if 'rules' not in data:
        return json.dumps(dict(facts=[], working=[]))
    result = dict(facts=[], working=[], summaries=[dict(message_id=s['message_id'], text='Synthetic summary')
        for s in data['pending'] if s['message_id'] in data['rules']['summary_required']])
    if '本次仅补' in system:
        return json.dumps({'summaries': result['summaries']})
    for item in data['pending']:
        nature = 'user_statement' if item['role'] == 'user' else 'assistant_suggestion'
        result['facts'].append(dict(subject='user', predicate='owns', object=item['content'][:30],
            text=item['content'][:30], assertion_kind=nature, source_ids=[item['id']]))
    return json.dumps(result)


@pytest.fixture
def services(tmp_path, monkeypatch):
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setattr(BufferedExtraction, 'consume', lambda _: None)
    monkeypatch.delenv('MNEMOTRACE_BUFFERED_EXTRACTION', raising=False)
    made = []
    def make(handler=reply, path=None):
        model = FakeLLM(handler=handler)
        monkeypatch.setattr(providers, 'make_llm', lambda *a, **k: model)
        svc = MemoryService(data_dir=str(path or tmp_path), embedder_name='hashing', llm_name='synthetic')
        stamp = [1000.0]
        svc._buffered.clock = lambda: stamp[0]
        made.append(svc)
        return svc, model, stamp
    yield make
    for svc in made:
        svc.close()


def drain(svc):
    for _ in range(100):
        if not run_one(svc, 'u'):
            return
    pytest.fail('pipeline did not yield')


def force(svc, session='s'):
    return svc.flush('u', session, timeout=0)


def test_accept_batch_dedup_conflict_and_legal_repetition(services):
    svc, model, stamp = services()
    result = svc.remember_messages('u', [message('a'), message('b'), message('a')], 's')
    assert result['queued'] and result['messages'][0] == result['messages'][2]
    assert [r['sequence'] for r in result['messages']] == [1, 2, 1]
    stamp[0] += 40
    assert svc.remember_messages('u', [message('a')], 's')['messages'][0] == result['messages'][0]
    state = svc.get('u')._message_state
    assert next(iter(state['namespaces']['u']['sessions'].values()))['last_activity'] == 1000
    with pytest.raises(MessageConflictError):
        svc.remember_messages('u', [message('c'), message('a', 'conflict')], 's')
    with pytest.raises(MessageConflictError):
        svc.remember_messages('u', [message('c'), message('c', 'conflict')], 's')
    assert len(svc.get('u').episodes_doc.values()) == 2 and not model.calls


def test_idle_60_and_single_joint_call_then_restart_idempotence(services, tmp_path):
    svc, model, stamp = services()
    accepted = svc.remember_messages('u', [message('a', 'x' * 2500), message('b', 'y' * 2500)], 's')
    # Two summary reservations reach the processing budget, before idle expiry.
    drain(svc)
    assert len(model.calls) == 1
    assert all(j['state'] == 'completed' for j in svc.message_status('u')['jobs'])
    mem = svc.get('u')
    assert all(e.summary == 'Synthetic summary' for e in mem.episodes_doc.values())
    assert mem.episodes_vec.values() == []
    svc.close()
    restored, model2, _ = services(path=tmp_path)
    replay = restored.remember_messages('u', [message('a', 'x' * 2500)], 's')
    assert replay['messages'][0]['episode_id'] == accepted['messages'][0]['episode_id']
    assert replay['messages'][0]['state'] == 'completed' and not model2.calls


def test_offline_raw_tasks_unknown_time_and_strict_corruption(tmp_path):
    mem = Memory()
    result = mem.remember_messages([message('a', event_time=None)], 's', user_id='u', mode='historical')
    mem.save(str(tmp_path / 'store'))
    restored = Memory.open(str(tmp_path / 'store'))
    ep = restored.episodes_doc.get(result['messages'][0]['episode_id'])
    assert ep.event_time is None and ep.mode == 'historical'
    broken = deepcopy(restored._message_state)
    del next(iter(broken['namespaces']['u']['messages'].values()))['source']
    with pytest.raises(ValueError):
        validate_state(broken)


def test_concurrent_sequence_allocation(services):
    svc, _, _ = services()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: svc.remember_messages('u', [message(str(i))], 's'), range(12)))
    assert sorted(r['messages'][0]['sequence'] for r in results) == list(range(1, 13))


def test_bad_json_three_total_calls_and_later_window_continues(services):
    svc, model, stamp = services(lambda *args: '{')
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    drain(svc)
    stamp[0] += 30
    drain(svc)
    stamp[0] += 120
    drain(svc)
    assert len(model.calls) == 3
    assert svc.message_status('u')['jobs'][0]['core'] == 'failed'
    stamp[0] += 1000
    drain(svc)
    assert len(model.calls) == 3


def test_partial_summary_failure_preserves_core_and_compensates_only(services):
    calls = []
    def handler(prompt, system):
        result = json.loads(reply(prompt, system))
        calls.append(system)
        if len(calls) == 1:
            result['summaries'] = []
        return json.dumps(result)
    svc, model, stamp = services(handler)
    svc.remember_messages('u', [message('a', 'x' * 2500)], 's')
    force(svc)
    drain(svc)
    assert len(svc.get('u').fact_store.values()) == 1
    assert svc.message_status('u')['jobs'][0]['core'] == 'completed'
    stamp[0] += 30
    drain(svc)
    assert len(model.calls) == 2 and '本次仅补' in calls[-1]
    assert len(svc.get('u').fact_store.values()) == 1
    assert svc.message_status('u')['jobs'][0]['state'] == 'completed'


def test_model_outside_lock_new_message_outside_frozen_batch(services):
    entered, release = threading.Event(), threading.Event()
    def handler(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, model, _ = services(handler)
    first = svc.remember_messages('u', [message('a')], 's')
    force(svc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(run_one, svc, 'u')
        assert entered.wait(5)
        svc.remember_messages('u', [message('b')], 's')
        assert svc.get_episode('u', first['messages'][0]['episode_id'])['content'] == 'Synthetic item'
        release.set()
        assert future.result(5)
    drain(svc)
    assert len(model.calls) == 1
    assert [j['core'] for j in svc.message_status('u')['jobs']] == ['completed', 'pending']


def test_idle_has_no_oldest_wait_and_retransmission_does_not_reset(services):
    svc, model, stamp = services()
    for index in range(5):
        svc.remember_messages('u', [message(str(index))], 's')
        stamp[0] += 40
        assert not run_one(svc, 'u')
    svc.remember_messages('u', [message('4')], 's')
    stamp[0] += 20
    drain(svc)
    assert len(model.calls) == 1


def test_unknown_source_time_survives_generation_retrieval_and_export(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a', event_time=None)], 's', mode='historical')
    force(svc)
    drain(svc)
    fact = svc.get('u').fact_store.values()[0]
    assert fact.valid_at is None and fact.created_at == 1000
    assert svc.get('u').search('Synthetic', user_id='u').facts
    svc.get('u').lean_context('Synthetic', user_id='u')
    assert svc.export('u', include_sensitive=True)['facts'][0]['valid_at'] is None


def test_live_working_historical_and_assistant_role_validation(services):
    def respond(prompt, system):
        data = json.loads(prompt.split(WINDOW_PREFIX, 1)[1])
        return json.dumps(dict(facts=[], summaries=[], working=[] if data['mode'] == 'historical' else [
            dict(content='Authorized task', kind='task', assertion_kind='user_statement',
                 source_ids=[data['pending'][0]['id']])]))
    svc, _, _ = services(respond)
    svc.remember_messages('u', [message('a')], 'live', processing='immediate')
    svc.remember_messages('u', [message('b')], 'old', mode='historical', processing='immediate')
    assert [w.session_id for w in svc.get('u').working_mem.values()] == ['live']
    svc.remember_messages('u', [message('c', role='assistant')], 'forged')
    force(svc, 'forged')
    drain(svc)
    assert svc.message_status('u', 'forged')['jobs'][0]['core'] == 'pending'
    assert len(svc.get('u').working_mem) == 1


def test_head_tail_coverage_fixed_after_restart_and_summary_partial(services, tmp_path):
    svc, model, _ = services()
    text = '开始' * 5000 + '不可见中间' * 5000 + '结尾' * 5000
    accepted = svc.remember_messages('u', [message('a', text)], 's')
    assert run_one(svc, 'u')  # generation and saved model response
    saved = deepcopy(next(iter(svc.get('u')._message_state['namespaces']['u']['windows'].values()))['input'])
    assert saved['pending'][0]['partial'] and len(saved['pending'][0]['ranges']) == 2
    assert '已省略' in saved['pending'][0]['content']
    svc.close()
    restored, model2, _ = services(path=tmp_path)
    drain(restored)
    ep = restored.get('u').episodes_doc.get(accepted['messages'][0]['episode_id'])
    assert ep.content == text and ep.coverage['partial'] and ep.coverage['strategy_completed']
    assert sum(b - a for a, b in ep.coverage['ranges']) < len(text)
    from mnemotrace.retrieve.events import event_evidence
    assert event_evidence(ep).derived and event_evidence(ep).partial
    assert len(model.calls) == 1 and len(model2.calls) == 0


def test_completed_flag_corruption_and_saved_result_sources_are_rejected(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    broken = deepcopy(svc.get('u')._message_state)
    win = next(iter(broken['namespaces']['u']['windows'].values()))
    win.update(state='completed', phase='done', result=None, core_saved=False)
    with pytest.raises(ValueError):
        validate_state(broken)
    broken = deepcopy(svc.get('u')._message_state)
    next(iter(broken['namespaces']['u']['windows'].values()))['result']['facts'][0]['source_ids'] = ['unknown']
    with pytest.raises(ValueError):
        validate_state(broken)


def test_embedding_failure_reuses_saved_generation(services, monkeypatch):
    svc, model, stamp = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    original = svc.embedder.embed_batch
    monkeypatch.setattr(svc.embedder, 'embed_batch', lambda _: (_ for _ in ()).throw(RuntimeError('synthetic')))
    assert run_one(svc, 'u')
    monkeypatch.setattr(svc.embedder, 'embed_batch', original)
    stamp[0] += 30
    drain(svc)
    assert len(model.calls) == 1 and svc.message_status('u')['jobs'][0]['state'] == 'completed'


def test_manual_change_and_configuration_between_generation_and_commit(services):
    svc, model, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    force(svc)
    assert run_one(svc, 'u')
    assert run_one(svc, 'u')
    svc.add_fact('u', 'user', 'owns', 'manual synthetic')
    drain(svc)
    assert len(model.calls) == 1
    assert len(svc.get('u').fact_store.values()) == 1
    assert svc.message_status('u')['jobs'][0]['state'] == 'cancelled'


def test_erasure_removes_hidden_window_copies(services):
    svc, _, _ = services()
    result = svc.remember_messages('u', [message('a', 'unique erased synthetic text')], 's')
    force(svc)
    assert run_one(svc, 'u')
    with svc.write_lock('u'):
        mem = svc.get('u')
        mem.episodes_doc.delete(result['messages'][0]['episode_id'])
        svc._save('u', mem)
    assert 'unique erased synthetic text' not in json.dumps(svc.get('u')._message_state)
    assert not run_one(svc, 'u')


def test_large_batch_rotates_sessions_and_stops_at_frozen_cutoff(services):
    svc, model, _ = services()
    svc.remember_messages('u', [message(str(n), str(n) * 2500) for n in range(6)], 'large')
    svc.remember_messages('u', [message('other', 'other' * 500)], 'small')
    force(svc, 'large')
    force(svc, 'small')
    drain(svc)
    pending = [json.loads(c['prompt'].split(WINDOW_PREFIX, 1)[1])['pending'] for c in model.calls]
    assert len(pending) == 4 and len(pending[0]) == 2
    assert pending[1][0]['message_id'] == 'other'
    assert all(j['state'] == 'completed' for j in svc.message_status('u')['jobs'])


def test_no_idle_snapshot_rewrites_and_raw_entry_cannot_consume_dialogue(services):
    svc, _, _ = services()
    svc.remember_messages('u', [message('a')], 's')
    before = svc.get('u')._snapshot_version
    for _ in range(4):
        assert not run_one(svc, 'u')
    assert svc.get('u')._snapshot_version == before
    svc.remember('u', 'manual synthetic raw', processing='immediate')
    assert svc.message_status('u')['jobs'][0]['core'] == 'pending'


def test_standalone_python_uses_same_joint_processor():
    mem = Memory(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', user_id='u')
    result = mem.flush_messages('u', 's')
    assert result['ok'] and len(mem.fact_store.values()) == 1


def test_explicit_snapshot_upgrade_preserves_source_and_does_not_guess_messages(tmp_path):
    from mnemotrace.store.upgrade import upgrade_snapshot
    mem = Memory()
    mem.add('synthetic old merged session', role='session')
    source_path = tmp_path / 'old'
    mem.save(str(source_path))
    manifest = json.loads((source_path / 'manifest.json').read_text())
    del manifest['format_version']
    (source_path / 'manifest.json').write_text(json.dumps(manifest))
    rows = [json.loads(line) for line in (source_path / 'episodes.jsonl').read_text().splitlines()]
    for row in rows:
        row['speaker'] = row.pop('role')
        for field in ('message_id', 'sequence', 'mode', 'coverage'):
            del row[field]
    original = '\n'.join(json.dumps(row) for row in rows) + '\n'
    (source_path / 'episodes.jsonl').write_text(original)
    destination = tmp_path / 'upgraded'
    upgrade_snapshot(str(source_path), str(destination))
    assert (source_path / 'episodes.jsonl').read_text() == original
    restored = Memory.open(str(destination))
    ep = restored.episodes_doc.values()[0]
    assert ep.role == 'session' and ep.message_id is None and ep.sequence is None
