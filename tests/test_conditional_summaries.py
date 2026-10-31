"""Fixed-model summary generation, checkpoints, concurrency and failure contracts."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from mnemotrace import Memory
from mnemotrace._buffered import BufferedExtraction
from mnemotrace._summaries import run_one
from mnemotrace.consolidate.summaries import SummaryTasks, validate_state
from mnemotrace.consolidate.windows import WINDOW_PREFIX
from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.service import MemoryService

LONG = 'Synthetic original record. ' * 100
SUMMARY = 'A synthetic event was recorded with its source preserved.'


@pytest.fixture
def services(tmp_path, monkeypatch):
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setattr(BufferedExtraction, 'consume', lambda _: None)
    created = []
    def make(handler=None, buffered=False, path=None):
        monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1' if buffered else '0')
        def respond(prompt, system='', **kwargs):
            if WINDOW_PREFIX in prompt:
                return '{"facts":[],"working":[]}'
            return handler(prompt, system) if handler else SUMMARY
        model = FakeLLM(handler=respond)
        monkeypatch.setattr(providers, 'make_llm', lambda *a, **k: model)
        svc = MemoryService(data_dir=str(path or tmp_path), embedder_name='hashing', llm_name='synthetic')
        if svc._buffered:
            svc._buffered.clock = lambda: 1000
        created.append(svc)
        return svc, model
    yield make
    for svc in created:
        svc.close()


@pytest.mark.parametrize('length,expected', [(1999, 0), (2000, 1)])
def test_length_boundary_and_repeat_do_not_call_model(services, length, expected):
    svc, model = services()
    result = svc.remember('u', 'x' * length)
    summaries = [c for c in model.calls if c['prompt'].endswith('摘要：')]
    assert len(summaries) == expected
    assert result['summarization']['status'] == ('completed' if expected else 'not_needed')
    before = len(model.calls)
    svc.close_session('u', 'default', outcomes=False)
    assert len(model.calls) == before
    ep = svc.get('u').episodes_doc.get(result['episode_id'])
    assert (svc.get('u').episodes_vec.get(ep.id) is None) == bool(expected)


@pytest.mark.parametrize('output', ['', 'x' * 801, Completion('valid short text', 'length')])
def test_invalid_summary_never_replaces_original(services, output):
    svc, _ = services(lambda *a: output)
    result = svc.remember('u', LONG)
    assert result['ok']
    assert result['summarization']['status'] == 'failed'
    mem = svc.get('u')
    assert not mem.episodes_doc.get(result['episode_id']).summary
    assert mem.episodes_vec.get(result['episode_id']) is not None


def test_provider_failure_is_separate_from_successful_extraction(services):
    def fail(*a):
        raise TimeoutError('synthetic provider error containing private content')
    svc, _ = services(fail)
    result = svc.remember('u', LONG)
    assert result['ok'] and result['extracted'] == 0
    assert result['summarization'] == {'status': 'retry_wait', 'error_code': 'summary_failed'}
    assert 'private' not in json.dumps(svc.processing_status('u'))
    assert svc.get('u').episodes_doc.get(result['episode_id']).consolidated


def test_buffered_summary_is_registered_only_after_complete_extraction(services):
    svc, model = services(buffered=True)
    result = svc.remember('u', LONG, processing='buffered')
    assert result['summarization']['status'] == 'pending'
    assert svc.processing_status('u')['summarization']['jobs'] == []
    svc.flush('u', timeout=0)
    assert svc._buffered.run_one('u')
    assert svc.processing_status('u')['counts']['completed'] == 1
    assert svc.processing_status('u')['summarization']['counts']['pending'] == 1
    assert svc._buffered.run_one('u')
    assert svc._buffered.run_one('u')
    assert svc.processing_status('u')['summarization']['counts']['completed'] == 1
    assert len([c for c in model.calls if c['prompt'].endswith('摘要：')]) == 1


def test_summary_model_never_holds_namespace_lock(services):
    entered, release = threading.Event(), threading.Event()
    def blocked(*a):
        entered.set()
        assert release.wait(10)
        return SUMMARY
    svc, _ = services(blocked, buffered=True)
    first = svc.remember('u', LONG, processing='buffered')
    svc.flush('u', timeout=0)
    assert svc._buffered.run_one('u')
    with ThreadPoolExecutor() as pool:
        work = pool.submit(run_one, svc, 'u')
        assert entered.wait(3)
        assert svc.stats('u')['counts']['episodes'] == 1
        second = svc.remember('u', 'later message', processing='buffered')
        release.set()
        assert work.result(timeout=5)
    assert run_one(svc, 'u')
    mem = svc.get('u')
    assert mem.episodes_doc.get(first['episode_id']).summary == SUMMARY
    assert mem.episodes_doc.get(second['episode_id']) is not None


@pytest.mark.parametrize('invalidate', ['forget', 'policy', 'stop'])
def test_late_summary_does_not_restore_invalidated_content(services, invalidate):
    entered, release = threading.Event(), threading.Event()
    def blocked(*a):
        entered.set()
        assert release.wait(10)
        return SUMMARY
    svc, _ = services(blocked, buffered=True)
    result = svc.remember('u', LONG, processing='buffered')
    svc.flush('u', timeout=0)
    svc._buffered.run_one('u')
    with ThreadPoolExecutor() as pool:
        work = pool.submit(run_one, svc, 'u')
        assert entered.wait(3)
        if invalidate == 'forget':
            svc.forget('u')
        elif invalidate == 'policy':
            svc.set_policy('u', summary_system='changed synthetic policy')
        else:
            svc._buffered.close(timeout=0)
        release.set()
        assert not work.result(timeout=5)
    if invalidate != 'forget':
        assert not svc.get('u').episodes_doc.get(result['episode_id']).summary
    else:
        assert svc._store_version('u') is None


def test_multistep_summary_resumes_saved_steps_after_restart(services, tmp_path):
    calls = []
    def respond(prompt, system):
        calls.append(prompt)
        return SUMMARY
    svc, _ = services(respond, buffered=True)
    svc.config.summary_max_input_bytes = 4000
    source = LONG * 5
    result = svc.remember('u', source, scope='working', processing='buffered')
    assert run_one(svc, 'u')
    job = svc.processing_status('u')['summarization']['jobs'][0]
    assert job['state'] == 'pending' and job['completed_through'] > 0
    saved_first = calls[0]
    svc.close()
    reopened, _ = services(respond, buffered=True, path=tmp_path)
    reopened.config.summary_max_input_bytes = 4000
    while run_one(reopened, 'u'):
        pass
    ep = reopened.get('u').episodes_doc.get(result['episode_id'])
    assert ep.content == source and ep.summary
    assert calls.count(saved_first) == 1
    assert all(len(p.encode()) + len(s.encode()) <= 4000 for p, s in [(c['prompt'], c['system']) for c in reopened.llm.calls])


def test_close_summary_failure_preserves_working_memory_then_recovers(services):
    fail = [True]
    def respond(*a):
        return '' if fail[0] else SUMMARY
    svc, _ = services(respond)
    result = svc.remember('u', LONG, scope='working')
    from mnemotrace.extraction import ProcessingIncompleteError
    with pytest.raises(ProcessingIncompleteError):
        svc.close_session('u', 'default', outcomes=False)
    assert result['id'] in svc.get('u').working_mem
    fail[0] = False
    assert svc.close_session('u', 'default', outcomes=False)['working_cleared'] == 1


def test_native_multiple_users_and_present_state_validation(tmp_path):
    mem = Memory(llm=FakeLLM(handler=lambda *a: SUMMARY))
    episodes = [mem.add(LONG, user_id=u) for u in ('a', 'b')]
    assert mem.summarize_episodes(episodes) == 2
    mem.save(str(tmp_path))
    reopened = Memory.open(str(tmp_path))
    assert all(ep.summary for ep in reopened.episodes_doc.values())
    bad = deepcopy(reopened._summary_state)
    del bad['a']['jobs'][episodes[0].id]['config']
    with pytest.raises(ValueError):
        validate_state(bad, reopened.episodes_doc.values())
    assert SummaryTasks(reopened, 'a').snapshot()['counts']['completed'] == 1


def test_summary_checkpoint_save_failure_does_not_fail_accepted_remember(services, monkeypatch):
    from mnemotrace.store import PersistenceError
    svc, _ = services()
    original = svc._save
    def failed_save(user, mem):
        if any(ep.summary for ep in mem.episodes_doc.values()):
            raise PersistenceError('synthetic summary commit error')
        return original(user, mem)
    monkeypatch.setattr(svc, '_save', failed_save)
    result = svc.remember('u', LONG)
    assert result['ok'] and result['summarization'] == {'status': 'failed', 'error_code': 'storage_failed'}
    assert not svc.get('u').episodes_doc.get(result['episode_id']).summary
    assert svc.get('u').episodes_doc.get(result['episode_id']).consolidated
    monkeypatch.setattr(svc, '_save', original)
    assert svc.close_session('u', 'default', outcomes=False)['summaries'] == 1


def test_native_embed_failure_is_recorded_and_retryable():
    from mnemotrace.embed import HashingEmbedder
    class FailingEmbedder(HashingEmbedder):
        def embed(self, text):
            if text == SUMMARY:
                raise OSError('synthetic embed failure')
            return super().embed(text)
    mem = Memory(embedder=FailingEmbedder(), llm=FakeLLM(handler=lambda *a: SUMMARY))
    ep = mem.add(LONG, user_id='u')
    assert mem.summarize_episodes([ep]) == 0
    assert SummaryTasks(mem, 'u').result(ep.id)['status'] == 'failed'
    calls = len(mem.llm.calls)
    mem.embedder = HashingEmbedder()
    assert mem.summarize_episodes([ep]) == 1
    assert len(mem.llm.calls) == calls


def test_background_summary_has_three_attempts_with_bounded_backoff(services):
    def fail(*a):
        raise TimeoutError('synthetic')
    svc, model = services(fail, buffered=True)
    stamp = [1000]
    svc._buffered.clock = lambda: stamp[0]
    svc.remember('u', LONG, scope='working', processing='buffered')
    for delay in (30, 120):
        assert run_one(svc, 'u')
        assert svc.processing_status('u')['summarization']['counts']['retry_wait'] == 1
        assert not run_one(svc, 'u')
        stamp[0] += delay
    assert run_one(svc, 'u')
    assert svc.processing_status('u')['summarization']['counts']['failed'] == 1
    assert len(model.calls) == 3


def test_flush_default_does_not_wait_for_summary_and_opt_in_observes_it(services):
    svc, _ = services(buffered=True)
    result = svc.remember('u', LONG, processing='buffered')
    svc.flush('u', timeout=0)
    assert svc._buffered.run_one('u')
    assert svc.flush('u', timeout=0)['ok']
    assert svc.flush('u', timeout=0, include_summaries=True)['status'] == 'timed_out'
    assert svc._buffered.run_one('u')
    assert svc._buffered.run_one('u')
    completed = svc.flush('u', timeout=0, include_summaries=True)
    assert completed['ok'] and completed['summarization']['counts']['completed'] == 1
    assert completed['summarization']['jobs'][0]['episode_id'] == result['episode_id']


def test_close_keeps_events_and_working_added_during_summary(services):
    entered, release = threading.Event(), threading.Event()
    def blocked(*a):
        entered.set()
        assert release.wait(10)
        return SUMMARY
    svc, _ = services(blocked, buffered=True)
    first = svc.remember('u', LONG, scope='working', session_id='s', processing='buffered')
    with ThreadPoolExecutor() as pool:
        closing = pool.submit(svc.close_session, 'u', 's', outcomes=False)
        assert entered.wait(3)
        later = svc.remember('u', 'later task', scope='working', session_id='s', processing='buffered')
        release.set()
        assert closing.result(timeout=5)['working_cleared'] == 1
    assert first['id'] not in svc.get('u').working_mem
    assert later['id'] in svc.get('u').working_mem


def test_native_summary_index_failure_restores_original_and_retries_only_embedding(monkeypatch):
    mem = Memory(llm=FakeLLM(handler=lambda *a: SUMMARY))
    ep = mem.add(LONG, user_id='u')
    original = mem.summary_vec.upsert
    def fail(*a, **k):
        raise OSError('synthetic index failure')
    monkeypatch.setattr(mem.summary_vec, 'upsert', fail)
    assert mem.summarize_episodes([ep]) == 0
    assert not ep.summary and mem.episodes_vec.get(ep.id) is not None
    assert mem.summary_vec.get(ep.id) is None
    assert SummaryTasks(mem, 'u').result(ep.id)['status'] == 'failed'
    calls = len(mem.llm.calls)
    monkeypatch.setattr(mem.summary_vec, 'upsert', original)
    assert mem.summarize_episodes([ep]) == 1
    assert len(mem.llm.calls) == calls
    assert mem.episodes_vec.get(ep.id) is None


def test_embedding_checkpoint_survives_restart_without_regenerating(services, tmp_path):
    svc, model = services(buffered=True)
    result = svc.remember('u', LONG, scope='working', processing='buffered')
    assert run_one(svc, 'u')
    assert len(model.calls) == 1
    assert not svc.get('u').episodes_doc.get(result['episode_id']).summary
    svc.close()
    reopened, model2 = services(buffered=True, path=tmp_path)
    assert run_one(reopened, 'u')
    assert model2.calls == []
    assert reopened.get('u').episodes_doc.get(result['episode_id']).summary == SUMMARY


@pytest.mark.parametrize('phase', ['source', 'reduce', 'source_partial', 'reduce_partial', 'completed'])
def test_corrupt_summary_frontier_is_rejected_before_existing_indices_are_changed(tmp_path, phase):
    from mnemotrace.store.persist import load_memory, StoreFormatError
    mem = Memory(llm=FakeLLM(handler=lambda *a: SUMMARY))
    ep = mem.add(LONG, user_id='u')
    tasks = SummaryTasks(mem, 'u')
    tasks.register([ep], 1000)
    job = tasks.rows()[ep.id]
    if phase.startswith('source'):
        job.update(position=100, pieces=[SUMMARY, SUMMARY])
    elif phase.startswith('reduce'):
        job.update(phase='reduce', position=2, pieces=[SUMMARY] * 3, outputs=[SUMMARY] * 2, **{'pass': 1})
    else:
        assert mem.summarize_episodes([ep]) == 1
    from mnemotrace.consolidate.summaries import seal_progress
    seal_progress(job)
    mem.save(str(tmp_path))
    path = tmp_path/'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    stored = data['state']['summarization']['u']['jobs'][ep.id]
    if phase.startswith('source'):
        stored['pieces'] = [SUMMARY] if phase.endswith('partial') else []
    elif phase.startswith('reduce'):
        stored['outputs'] = [SUMMARY] if phase.endswith('partial') else []
    else:
        stored['position'] = 1
    path.write_text(json.dumps(data), encoding='utf-8')
    target = Memory()
    existing = target.add('existing synthetic record', user_id='u')
    with pytest.raises(StoreFormatError):
        load_memory(target, str(tmp_path))
    assert target.episodes_doc.get(existing.id).content == existing.content
    assert target.episodes_vec.get(existing.id) is not None


def test_failed_summary_flush_reports_only_captured_boundary(services, monkeypatch):
    svc, _ = services(buffered=True)
    first = svc.remember('u', LONG, scope='working', session_id='s', processing='buffered')
    added = []
    def failed(*a, **k):
        added.append(svc.remember('u', LONG, scope='working', session_id='s', processing='buffered'))
        return {'ok': False, 'status': 'timed_out'}
    monkeypatch.setattr(svc._buffered, 'flush', failed)
    result = svc.flush('u', 's', timeout=0, include_summaries=True)
    assert [j['episode_id'] for j in result['summarization']['jobs']] == [first['episode_id']]
    assert len(svc.processing_status('u', 's')['summarization']['jobs']) == 2
