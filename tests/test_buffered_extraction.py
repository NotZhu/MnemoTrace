"""Native buffered service contracts; controlled time and bounded thread synchronization."""
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemotrace import ExtractionResponseError
from mnemotrace._buffered import BufferedExtraction
from mnemotrace.consolidate.windows import WINDOW_PREFIX
from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.service import MemoryService


def reply(prompt, system):
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
    return json.dumps({'facts': [{'subject': 'user', 'predicate': 'owns', 'object': s['content'],
                                 'source_ids': [s['id']]} for s in data['pending']], 'working': []})


@pytest.fixture
def setup(monkeypatch, tmp_path):
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setenv('MNEMOTRACE_LLM', 'synthetic')
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1')
    original = BufferedExtraction.consume
    services = []
    def make(handler=reply, *, threaded=False, path=None):
        model = FakeLLM(handler=handler)
        monkeypatch.setattr(providers, 'make_llm', lambda *a, **k: model)
        monkeypatch.setattr(BufferedExtraction, 'consume', original if threaded else lambda _: None)
        svc = MemoryService(data_dir=str(path or tmp_path), embedder_name='hashing', llm_name='synthetic')
        stamp = [1000.0]
        if svc._buffered:
            svc._buffered.clock = lambda: stamp[0]
        services.append(svc)
        return svc, model, stamp
    yield make
    for svc in services:
        svc.close()


def force(svc, user='u', session=None):
    return svc.flush(user, session, timeout=0)


def _child_window(path, entered, release, crash):
    from mnemotrace.llm import providers
    for key in list(os.environ):
        if key.startswith('MNEMOTRACE_'):
            os.environ.pop(key)
    os.environ['MNEMOTRACE_BUFFERED_EXTRACTION'] = '1'
    providers.load_dotenv = lambda *a, **k: None
    def blocked(prompt, system):
        entered.set()
        if crash:
            os._exit(0)
        assert release.wait(10)
        return reply(prompt, system)
    providers.make_llm = lambda *a, **k: FakeLLM(handler=blocked)
    BufferedExtraction.consume = lambda _: None
    svc = MemoryService(data_dir=path, embedder_name='hashing', llm_name='synthetic')
    svc._buffered.clock = lambda: 1000
    assert svc._buffered.run_one('u')
    svc.close()


def test_idle_and_saved_raw_then_window_completion(setup):
    svc, model, clock = setup()
    first = svc.remember('u', 'alpha', session_id='s', processing='buffered')
    clock[0] += 1
    svc.remember('u', 'beta', session_id='s', processing='buffered')
    assert first['queued'] and first['processing'] == 'pending'
    assert not svc._buffered.run_one('u') and not model.calls
    clock[0] += 5
    assert svc._buffered.run_one('u')
    assert len(model.calls) == 1
    assert svc.processing_status('u')['counts']['completed'] == 2
    assert len(svc.get('u').fact_store.values()) == 2
    assert all(not ep.summary for ep in svc.get('u').episodes_doc.values())


def test_max_wait_is_not_postponed_by_new_events(setup):
    svc, model, clock = setup()
    for n in range(9):
        svc.remember('u', str(n), processing='buffered')
        clock[0] += 4
    assert svc._buffered.run_one('u')
    assert len(model.calls) == 1


def test_restart_recovers_only_registered_jobs(setup):
    svc, _, _ = setup()
    svc.remember('u', 'alpha', processing='buffered')
    with svc.write_lock('u'):
        mem = svc.get('u')
        mem.add('historical unregistered', user_id='u')
        svc._save('u', mem)
    svc.close()
    restored, model, _ = setup()
    assert 'u' in restored._buffered.users
    force(restored)
    assert restored._buffered.run_one('u')
    assert len(model.calls) == 1
    assert [f.object for f in restored.get('u').fact_store.values()] == ['alpha']


def test_invalid_response_requires_explicit_retry(setup):
    fail = [True]
    def handler(prompt, system):
        return '{}' if fail[0] else reply(prompt, system)
    svc, model, _ = setup(handler)
    svc.remember('u', 'alpha', processing='buffered')
    force(svc)
    assert not svc._buffered.run_one('u')
    status = svc.processing_status('u')
    assert status['counts']['failed'] == 1
    assert status['jobs'][0]['error_code'] == ExtractionResponseError.code
    assert not svc._buffered.run_one('u') and len(model.calls) == 1
    fail[0] = False
    svc.flush('u', timeout=0, retry_failed=True)
    assert svc._buffered.run_one('u')
    assert svc.flush('u', timeout=0)['status'] == 'completed'


def test_transient_failure_has_bounded_backoff(setup):
    def error(*args):
        raise TimeoutError('synthetic')
    svc, model, clock = setup(error)
    svc.remember('u', 'alpha', processing='buffered')
    force(svc)
    svc._buffered.run_one('u')
    assert svc.processing_status('u')['counts']['retry_wait'] == 1
    assert not svc._buffered.run_one('u')
    clock[0] += 30
    svc._buffered.run_one('u')
    clock[0] += 119
    assert not svc._buffered.run_one('u')
    clock[0] += 1
    svc._buffered.run_one('u')
    assert len(model.calls) == 3
    assert svc.processing_status('u')['counts']['failed'] == 1


def test_rejected_completion_usage_is_not_reported_as_free(setup):
    svc, _, _ = setup(lambda *args: Completion('{"facts":[],"working":[]}', 'length', {'total_tokens': 13}))
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    assert not svc._buffered.run_one('u')
    assert svc.processing_status('u')['jobs'][0]['error_code'] == 'extraction_truncated'
    metrics = svc.metrics_snapshot()
    assert metrics['counts']['buffered_total_tokens'] == 13
    assert metrics['ops']['buffered_model_call']['n'] == 1


@pytest.mark.parametrize('failure', [ValueError, TypeError, ImportError])
def test_adapter_configuration_errors_do_not_schedule_retries(setup, failure):
    def broken(*args):
        raise failure('synthetic adapter configuration')
    svc, model, _ = setup(broken)
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    assert not svc._buffered.run_one('u')
    assert svc.processing_status('u')['counts']['failed'] == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize('crash', [False, True])
def test_cross_process_lease_and_crash_recovery(setup, crash):
    svc, _, _ = setup()
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    context = multiprocessing.get_context('spawn')
    entered, release = context.Event(), context.Event()
    child = context.Process(target=_child_window, args=(svc.data_dir, entered, release, crash))
    child.start()
    try:
        assert entered.wait(10)
        if not crash:
            assert svc.processing_status('u')['counts']['running'] == 1
            assert not svc._buffered.run_one('u')
            svc.remember('u', 'later', processing='buffered')
        release.set()
        child.join(15)
        assert child.exitcode == 0
        if crash:
            assert svc.processing_status('u')['counts']['running'] == 1
            assert svc._buffered.run_one('u')
        assert svc.processing_status('u')['counts']['completed'] == 1
        assert [f.object for f in svc.get('u').fact_store.values()] == ['first']
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_failed_route_blocks_later_events_until_explicit_retry(setup):
    invalid = [True]
    def handler(prompt, system):
        return '{}' if invalid[0] else reply(prompt, system)
    svc, model, _ = setup(handler)
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    svc._buffered.run_one('u')
    invalid[0] = False
    svc.remember('u', 'later', processing='buffered')
    force(svc)
    assert not svc._buffered.run_one('u') and len(model.calls) == 1
    svc.flush('u', timeout=0, retry_failed=True)
    assert svc._buffered.run_one('u')
    assert svc.processing_status('u')['counts']['completed'] == 2


def test_failed_save_cannot_acknowledge_buffered_acceptance(setup, monkeypatch):
    svc, _, _ = setup()
    def broken(*args):
        raise OSError('synthetic storage interruption')
    monkeypatch.setattr(svc, '_save', broken)
    with pytest.raises(OSError):
        svc.remember('u', 'first', processing='buffered')
    assert not svc.get('u').episodes_doc.values()


def test_cold_manual_authority_is_preserved_during_buffered_commit(setup):
    def handler(prompt, system):
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        return json.dumps({'facts': [{'subject': 'user', 'predicate': 'lives_in', 'object': 'old place',
                                     'source_ids': [data['pending'][0]['id']]}], 'working': []})
    svc, _, _ = setup(handler)
    authoritative = svc.add_fact('u', 'u', 'lives_in', 'manual place')
    with svc.write_lock('u'):
        mem = svc.get('u')
        fact = mem.fact_store.get(authoritative['id'])
        mem.cold_store.upsert(fact.id, fact.embedding, fact)
        mem.fact_store.delete(fact.id)
        svc._save('u', mem)
    svc.remember('u', 'synthetic', processing='buffered')
    force(svc)
    assert svc._buffered.run_one('u')
    assert [(f.object, f.source) for f in svc.get('u')._all_facts() if f.is_live()] == [('manual place', 'user')]


def test_import_does_not_retry_failed_buffered_sources(setup):
    invalid = [True]
    svc, _, _ = setup(lambda prompt, system: '{}' if invalid[0] else reply(prompt, system))
    first = svc.remember('u', 'failed source', session_id='s', processing='buffered')
    force(svc)
    svc._buffered.run_one('u')
    invalid[0] = False
    svc.import_('u', format='messages', data=[{'role': 'user', 'content': 'new import'}],
                session_id='s', consolidate=True, summarize=False)
    assert not svc.get('u').episodes_doc.get(first['episode_id']).consolidated
    assert svc.processing_status('u')['counts']['failed'] == 1
    assert all('failed source' not in f.object for f in svc.get('u')._all_facts())


def test_disabled_service_does_not_silently_drain_existing_jobs(setup, monkeypatch):
    from mnemotrace.extraction import ProcessingIncompleteError
    svc, _, _ = setup()
    svc.remember('u', 'pending source', session_id='s', processing='buffered')
    svc.close()
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '0')
    reopened, model, _ = setup()
    with pytest.raises(ProcessingIncompleteError):
        reopened.close_session('u', 's', summarize=False, outcomes=False)
    assert reopened.flush()['status'] == 'failed'
    assert not model.calls and reopened.processing_status('u')['counts']['pending'] == 1


def test_cached_read_without_model_does_not_pin_old_extractor_configuration(setup):
    svc, model, _ = setup()
    svc.llm = None
    assert svc.get('u').llm is None
    svc.llm = model
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    assert svc._buffered.run_one('u')
    assert len(model.calls) == 1 and svc.processing_status('u')['counts']['completed'] == 1


def test_budget_trigger_and_ready_sessions_rotate_without_real_sleep(setup):
    from mnemotrace.consolidate.windows import ExtractionWindow, SourceSpan
    svc, _, _ = setup()
    first = svc.remember('u', 'large synthetic ' * 100, session_id='large', processing='buffered')
    with svc.write_lock('u'):
        mem = svc.get('u')
        ep = mem.episodes_doc.get(first['episode_id'])
        mem.config.extraction_max_input_bytes = mem.engine.extractor.window_size(
            ExtractionWindow((SourceSpan(ep, 0, 1),))) + 100
    svc.remember('u', 'other', session_id='short', processing='buffered')
    assert svc._buffered.run_one('u')  # Budget alone triggers the first partial window.
    force(svc)
    assert svc._buffered.run_one('u')
    assert svc.processing_status('u', 'short')['counts']['completed'] == 1
    assert svc.processing_status('u', 'large')['counts']['pending'] == 1


def test_erase_while_model_waits_cannot_recreate_namespace(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, _, _ = setup(blocked)
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        svc.forget('u')
        release.set()
        assert not work.result(timeout=2)
    assert not os.path.exists(svc._path('u'))


@pytest.mark.parametrize('recreate', [False, True])
def test_flush_reports_erase_or_recreation_as_failed_not_completed(setup, monkeypatch, recreate):
    svc, _, _ = setup()
    svc.remember('u', 'first', processing='buffered')
    original = svc._buffered.status
    called = []
    def erased(*args, **kwargs):
        if not called:
            called.append(True)
            svc.forget('u')
            if recreate:
                svc.remember('u', 'new working', scope='working', processing='buffered')
        return original(*args, **kwargs)
    monkeypatch.setattr(svc._buffered, 'status', erased)
    assert svc.flush('u', timeout=0)['status'] == 'failed'
    if not recreate:
        assert not os.path.exists(svc._path('u')) and svc.hot_count == 0
        assert not svc.processing_status('u')['jobs']
        assert svc.flush('u', timeout=0)['ok']
        assert not os.path.exists(svc._path('u')) and svc.hot_count == 0


def test_failed_close_is_an_http_error_and_preserves_working(setup, monkeypatch):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc, _, _ = setup(lambda *args: '{}')
    working = svc.remember('u', 'active task', session_id='s', scope='working', processing='buffered')
    raw = svc.remember('u', 'invalid extraction', session_id='s', processing='buffered')
    force(svc)
    assert not svc._buffered.run_one('u')
    monkeypatch.setattr(appmod, '_svc', svc)
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', 'u:synthetic-u-key')
    with TestClient(appmod.app) as client:
        result = client.post('/v1/sessions/close', headers={'Authorization': 'Bearer synthetic-u-key'},
                             json={'session_id': 's', 'summarize': False, 'outcomes': False})
        assert result.status_code == 503
        detail = result.json()['detail']
        assert detail['code'] == 'processing_incomplete' and detail['stored_raw']
        assert raw['episode_id'] in detail['episode_ids']
        assert working['id'] in svc.get('u').working_mem


def test_reads_appends_and_deletion_while_model_waits(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, _, _ = setup(blocked)
    fact = svc.add_fact('u', 'user', 'owns', 'old synthetic')
    svc.remember('u', 'alpha', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        running = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        assert pool.submit(svc.processing_status, 'u').result(timeout=2)['counts']['running'] == 1
        later = pool.submit(svc.remember, 'u', 'beta', processing='buffered').result(timeout=2)
        svc.delete_fact('u', fact['id'])
        release.set()
        assert not running.result(timeout=2)
    assert not svc.get('u').fact_store.values()
    assert svc.processing_status('u')['counts']['cancelled'] == 2
    assert svc.get('u').episodes_doc.get(later['episode_id']) is not None


def test_manual_edit_while_model_waits_remains_authoritative(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        return json.dumps({'facts': [{'subject': 'user', 'predicate': 'lives_in', 'object': 'model place',
                                     'source_ids': [data['pending'][0]['id']]}], 'working': []})
    svc, _, _ = setup(blocked)
    fact = svc.add_fact('u', 'u', 'lives_in', 'initial place')
    svc.remember('u', 'synthetic source', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        svc.update_fact('u', fact['id'], object='edited place')
        release.set()
        assert work.result(timeout=2)
    assert [(f.object, f.source) for f in svc.get('u')._all_facts() if f.is_live()] == [('edited place', 'user')]
    assert svc.processing_status('u')['counts']['completed'] == 1


def test_disabled_buffer_rejected_before_write(setup, monkeypatch):
    from mnemotrace.extraction import BufferedProcessingDisabledError
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '0')
    svc, _, _ = setup()
    assert svc._buffered is None
    with pytest.raises(BufferedProcessingDisabledError, match='Enable'):
        svc.remember('u', 'raw', processing='buffered')
    assert not svc.get('u').episodes_doc.values()
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    monkeypatch.setattr(appmod, '_svc', svc)
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', 'u:synthetic-u-key')
    with TestClient(appmod.app) as client:
        result = client.post('/v1/remember', headers={'Authorization': 'Bearer synthetic-u-key'},
                             json={'content': 'not stored', 'processing': 'buffered'})
        assert result.status_code == 503 and result.json()['detail']['code'] == 'buffered_processing_disabled'
        assert not result.json()['detail']['stored_raw']
        assert not svc.get('u').episodes_doc.values()


def test_append_does_not_discard_inflight_result_and_flush_has_a_boundary(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, model, _ = setup(blocked)
    svc.remember('u', 'first', session_id='s', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        later = svc.remember('u', 'second', session_id='s', processing='buffered')
        release.set()
        assert work.result(timeout=2)
    assert len(model.calls) == 1
    assert [f.object for f in svc.get('u').fact_store.values()] == ['first']
    assert svc.processing_status('u')['counts']['pending'] == 1
    assert not svc.get('u').episodes_doc.get(later['episode_id']).consolidated


def test_policy_change_discards_then_replans_inflight_output(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, model, _ = setup(blocked)
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        svc.set_policy('u', extract_instruction='Synthetic instruction')
        release.set()
        assert work.result(timeout=2)
    assert not svc.get('u').fact_store.values()
    assert svc.processing_status('u')['counts']['pending'] == 1
    assert svc._buffered.run_one('u')
    assert 'Synthetic instruction' in model.calls[-1]['system']


def test_provider_option_change_discards_inflight_output(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, model, _ = setup(blocked)
    model.kwargs = {'synthetic_option': 'old'}
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        model.kwargs['synthetic_option'] = 'new'
        release.set()
        assert work.result(timeout=2)
    assert not svc.get('u').fact_store.values()
    assert svc._buffered.run_one('u')


def test_shutdown_timeout_discards_late_result_and_keeps_durable_job(setup):
    entered, release = threading.Event(), threading.Event()
    def blocked(prompt, system):
        entered.set()
        assert release.wait(5)
        return reply(prompt, system)
    svc, _, _ = setup(blocked)
    svc.remember('u', 'first', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        svc._buffered.close(timeout=0)
        release.set()
        assert not work.result(timeout=2)
    assert not svc.get('u').fact_store.values()
    assert svc.processing_status('u')['counts']['running'] == 1


def test_close_only_clears_working_and_events_from_captured_boundary(setup, monkeypatch):
    svc, _, _ = setup()
    first = svc.remember('u', 'first task', session_id='s', scope='working', processing='buffered')
    original_flush = svc._buffered.flush
    new = []
    def append_after_boundary(*args, **kwargs):
        new.append(svc.remember('u', 'later task', session_id='s', scope='working', processing='buffered'))
        new.append(svc.remember('u', 'later raw', session_id='s', processing='buffered'))
        return original_flush(*args, **kwargs)
    monkeypatch.setattr(svc._buffered, 'flush', append_after_boundary)
    result = svc.close_session('u', 's', summarize=False, outcomes=False)
    assert result['ok'] and result['episodes'] == 1 and result['working_cleared'] == 1
    assert svc.get('u').episodes_doc.get(first['episode_id']).consolidated
    assert new[0]['id'] in svc.get('u').working_mem
    assert not svc.get('u').episodes_doc.get(new[1]['episode_id']).consolidated


def test_conflict_model_runs_off_lock_and_is_replayed_at_commit(setup):
    entered, release = threading.Event(), threading.Event()
    def handler(prompt, system):
        if WINDOW_PREFIX in prompt:
            data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
            return json.dumps({'facts': [{'subject': 'user', 'predicate': 'dislikes', 'object': 'tea',
                                         'source_ids': [data['pending'][0]['id']]}], 'working': []})
        entered.set()
        assert release.wait(5)
        return 'CONFLICT'
    svc, model, _ = setup(handler)
    svc.config.conflict_detection = True
    svc.add_fact('u', 'u', 'allergic_to', 'tea')
    svc.remember('u', 'synthetic', processing='buffered')
    force(svc)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(svc._buffered.run_one, 'u')
        assert entered.wait(2)
        assert pool.submit(svc.processing_status, 'u').result(timeout=2)['counts']['running'] == 1
        release.set()
        assert work.result(timeout=2)
    assert len(model.calls) == 2


def test_http_acceptance_status_and_namespace_acl(setup, monkeypatch):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc, _, _ = setup()
    monkeypatch.setattr(appmod, '_svc', svc)
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', 'u:synthetic-u-key,v:synthetic-v-key')
    headers = {'Authorization': 'Bearer synthetic-u-key'}
    with TestClient(appmod.app) as client:
        response = client.post('/v1/remember', headers=headers,
                               json={'content': 'synthetic', 'processing': 'buffered', 'session_id': 's'})
        assert response.status_code == 202 and response.json()['queued']
        assert client.get('/v1/processing?session_id=s', headers=headers).json()['counts']['pending'] == 1
        assert client.get('/v1/processing?space=v', headers=headers).status_code == 403
        assert client.post('/v1/processing/flush', headers=headers,
                           json={'space': 'v', 'timeout': 0}).status_code == 403
        result = client.post('/v1/processing/flush', headers=headers, json={'session_id': 's', 'timeout': 0})
        assert result.json()['status'] == 'timed_out' and not result.json()['ok']
        svc.remember('v', 'foreign synthetic', processing='buffered')
        monkeypatch.setenv('MNEMOTRACE_SPACES', json.dumps({'synthetic-u-key': {
            'home': 'u', 'read': ['u'], 'write': ['u', 'v']}}))
        assert client.get('/v1/processing?space=v', headers=headers).status_code == 403
        write_only = client.post('/v1/processing/flush', headers=headers,
                                 json={'space': 'v', 'timeout': 0}).json()
        assert set(write_only) == {'ok', 'status'}


@pytest.mark.asyncio
async def test_mcp_local_and_remote_processing_interfaces(setup):
    import httpx
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend
    svc, _, _ = setup()
    local = LocalBackend(namespace='u', service=svc)
    assert (await local.remember('synthetic', processing='buffered'))['queued']
    assert (await local.processing_status())['counts']['pending'] == 1
    assert (await local.flush_processing(timeout=0))['status'] == 'timed_out'
    seen = []
    def response(request):
        seen.append(request)
        return httpx.Response(202 if request.url.path == '/v1/remember' else 200,
                              json={'ok': True}, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        remote = RemoteBackend('https://synthetic.invalid', 'synthetic-key', client=client)
        await remote.remember('synthetic', processing='buffered')
        await remote.processing_status('s')
        await remote.flush_processing('s', timeout=0, retry_failed=True)
    assert json.loads(seen[0].content)['processing'] == 'buffered'
    assert seen[1].url.params['session_id'] == 's'
    assert json.loads(seen[2].content)['retry_failed'] is True
