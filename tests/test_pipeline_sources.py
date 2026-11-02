"""Public source processing invariants, using synthetic responses and a controlled worker."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from mnemotrace import Config, Memory
from mnemotrace._pipeline import run_one
from mnemotrace._pipeline_worker import PipelineWorker
from mnemotrace.consolidate.pipeline import PipelineTasks, validate_state
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM, Completion
from mnemotrace.service import MemoryService
from mnemotrace.store import StoreFormatError

LONG = 'Synthetic source with a recorded detail. ' * 200
SUMMARY = 'A synthetic detail was recorded.'


def respond(prompt, system=''):
    if '<session>' in prompt:
        return '[]'
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
    result = dict(summaries=[dict(source_id=s['id'], text=SUMMARY) for s in data['pending']
                            if s['id'] in data['rules']['summary_required']])
    if data['operation'] != 'summary' and '本次仅补缺失摘要' not in system:
        result.update(facts=[], working=[])
    return json.dumps(result)


@pytest.fixture
def services(tmp_path, monkeypatch):
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setattr(PipelineWorker, 'consume', lambda _: None)
    made = []
    def make(handler=None, buffered=False, path=None):
        monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1' if buffered else '0')
        model = FakeLLM(handler=handler or respond)
        monkeypatch.setattr(providers, 'make_llm', lambda *a, **k: model)
        svc = MemoryService(data_dir=str(path or tmp_path), embedder_name='hashing', llm_name='synthetic')
        if svc._pipeline_worker:
            svc._pipeline_worker.clock = lambda: 1000
        made.append(svc)
        return svc, model
    yield make
    for svc in made:
        svc.close()


def drain(svc, user='u', limit=200):
    for _ in range(limit):
        if not run_one(svc, user):
            return
    pytest.fail('pipeline failed to yield')


@pytest.mark.parametrize('kind', ['raw', 'document'])
def test_full_source_segments_are_contiguous_and_summary_is_bounded_merge(services, kind):
    svc, model = services(buffered=True)
    svc.config.extraction_max_input_bytes = 6500
    svc.config.summary_max_input_bytes = 6500
    text = LONG * 3
    if kind == 'raw':
        result = svc.remember('u', text, scope='long', processing='buffered')
        source_id = result['episode_id']
        svc.flush('u', timeout=0)
        drain(svc)
    else:
        svc.import_document('u', text, filename='synthetic.txt')
        source_id = next(iter(svc.get('u').episodes_doc.values())).id
        text = svc.get('u').episodes_doc.get(source_id).content
    mem = svc.get('u')
    ep = mem.episodes_doc.get(source_id)
    assert ep.source_kind == kind and ep.message_id is None and ep.sequence is None
    assert ep.event_time is None and ep.content == text and ep.consolidated
    assert ep.summary == SUMMARY and ep.coverage['ranges'] == [[0, len(text)]]
    assert ep.coverage['strategy_completed'] and not ep.coverage['partial']
    tasks = PipelineTasks(mem, 'u')
    windows = list(tasks.state['windows'].values())
    extracts = [w for w in windows if w['operation'] == 'extract']
    assert len(extracts) > 1
    spans = [w['input']['pending'][0]['ranges'][0] for w in extracts]
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert all(w['input']['pending'][0]['content'] == text[a:b] for w, (a, b) in zip(extracts, spans))
    assert any(w['operation'] == 'summary' and len(w['input']['parts']) >= 2 for w in windows)
    assert len(model.calls) == sum(w['stats']['model_calls'] for w in windows)
    assert not set(ep.metadata).intersection({'extraction_progress', 'memory_result', 'extraction_error'})
    validate_state(deepcopy(mem._pipeline_state), mem.episodes_doc.values(), mem.embedder.dim)


def test_explicit_summary_preserves_unrequested_extraction_and_is_idempotent():
    model = FakeLLM(handler=respond)
    mem = Memory(llm=model, config=Config(summary_max_input_bytes=6500))
    ep = mem.add(LONG * 2)
    assert mem.summarize_episodes([ep]) == 1
    assert not ep.consolidated and not mem.fact_store.values()
    assert ep.summary == SUMMARY and ep.summary_coverage['policy'] == 'summary_source_v1' and ep.coverage is None
    count = len(model.calls)
    assert mem.summarize_episodes([ep]) == 0 and len(model.calls) == count
    assert PipelineTasks(mem, 'default').snapshot()['jobs'][0]['core'] == 'not_requested'


def test_partial_full_source_is_not_completed_and_restart_resumes_next_span(services, tmp_path):
    svc, model = services(buffered=True)
    svc.config.extraction_max_input_bytes = 6500
    result = svc.remember('u', LONG * 2, scope='long', processing='buffered')
    svc.flush('u', timeout=0)
    for _ in range(3):
        assert run_one(svc, 'u')
    ep = svc.get('u').episodes_doc.get(result['episode_id'])
    assert not ep.consolidated and ep.coverage['partial'] and not ep.coverage['strategy_completed']
    end = ep.coverage['ranges'][0][1]
    svc.close()
    reopened, model2 = services(buffered=True, path=tmp_path)
    reopened.config.extraction_max_input_bytes = 6500
    drain(reopened)
    assert reopened.get('u').episodes_doc.get(ep.id).consolidated
    first = json.JSONDecoder().raw_decode(model2.calls[0]['prompt'].split(WINDOW_PREFIX)[1])[0]
    assert first['pending'][0]['ranges'][0][0] == end


@pytest.mark.parametrize('bad', ['not-json', Completion('{"facts":[],"working":[],"summaries":[]}', 'length')])
def test_three_shared_model_attempts_are_durable_and_read_does_not_retry(services, bad):
    svc, model = services(lambda *a: bad, buffered=True)
    result = svc.remember('u', 'synthetic source', scope='long', processing='buffered')
    svc.flush('u', timeout=0)
    for stamp in (1000, 1030, 1150):
        svc._pipeline_worker.clock = lambda: stamp
        drain(svc)
    assert len(model.calls) == 3
    assert svc.processing_status('u')['counts']['failed'] == 1
    assert not svc.get('u').episodes_doc.get(result['episode_id']).consolidated
    drain(svc)
    svc.processing_status('u')
    assert len(model.calls) == 3


def test_missing_summary_preserves_core_and_retries_only_summary(services):
    def missing(prompt, system):
        if '本次仅补缺失摘要' in system:
            return respond(prompt, system)
        return '{"facts":[],"working":[],"summaries":[]}'
    svc, model = services(missing, buffered=True)
    result = svc.remember('u', LONG, processing='buffered')
    svc.flush('u', timeout=0)
    drain(svc)
    ep = svc.get('u').episodes_doc.get(result['episode_id'])
    assert ep.consolidated and not ep.summary
    svc._pipeline_worker.clock = lambda: 1030
    drain(svc)
    assert ep.summary == SUMMARY and len(model.calls) == 2
    assert svc.processing_status('u')['counts']['completed'] == 1


@pytest.mark.parametrize('mutation', ['delete', 'manual', 'config', 'stop'])
def test_model_call_does_not_block_reads_or_append_and_stale_result_cannot_commit(services, mutation):
    entered, release = threading.Event(), threading.Event()
    def blocking(prompt, system):
        entered.set()
        assert release.wait(10)
        return respond(prompt, system)
    svc, _ = services(blocking, buffered=True)
    receipt = svc.remember('u', 'original source', scope='long', processing='buffered')
    svc.flush('u', timeout=0)
    with ThreadPoolExecutor(2) as pool:
        job = pool.submit(run_one, svc, 'u')
        assert entered.wait(5)
        try:
            assert svc.get_episode('u', receipt['episode_id'])['content'] == 'original source'
            svc.remember('u', 'later source', scope='long', processing='buffered')
            if mutation == 'delete':
                svc.forget('u')
            elif mutation == 'manual':
                svc.add_fact('u', 'synthetic', 'owns', 'manual')
            elif mutation == 'config':
                svc.config.llm_topics = False
            else:
                svc._pipeline_worker.close(timeout=0)
        finally:
            release.set()
        job.result(5)
    if mutation != 'delete':
        ep = svc.get('u').episodes_doc.get(receipt['episode_id'])
        assert not ep.consolidated


@pytest.mark.parametrize('format_version', [2, 3])
@pytest.mark.parametrize('section', ['processing', 'summarization', 'messages'])
@pytest.mark.parametrize('payload', [None, {}, {'state': 'completed'}, {'state': 'failed'}, {'state': 'cancelled'}])
def test_all_legacy_task_sections_are_rejected_before_restore_and_copy_upgrade(tmp_path, section, payload, format_version):
    from mnemotrace.store.upgrade import upgrade_snapshot
    source = tmp_path / 'source'
    Memory().save(str(source))
    path = source / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['format_version'] = format_version
    data['state'].pop('pipeline', None)
    data['state'][section] = payload
    path.write_text(json.dumps(data), encoding='utf-8')
    before = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
    with pytest.raises(StoreFormatError):
        Memory.open(str(source))
    with pytest.raises(StoreFormatError):
        upgrade_snapshot(str(source), str(tmp_path / 'destination'))
    assert not (tmp_path / 'destination').exists()
    assert all((source / name).read_bytes() == value for name, value in before.items())


@pytest.mark.parametrize('field', ['extraction_progress', 'extraction_counts', 'extraction_error', 'outcome_error', 'memory_result'])
def test_legacy_episode_progress_is_rejected(tmp_path, field):
    mem = Memory()
    ep = mem.add('synthetic raw')
    ep.metadata[field] = {}
    with pytest.raises(StoreFormatError):
        mem.save(str(tmp_path / 'bad'))


def test_data_only_upgrade_preserves_sources_without_creating_tasks(tmp_path):
    from mnemotrace.store.upgrade import upgrade_snapshot
    source, destination = tmp_path / 'source', tmp_path / 'copy'
    mem = Memory()
    ep = mem.add('synthetic unknown time')
    mem.add_fact('owner', 'owns', 'synthetic object')
    mem.save(str(source))
    path = source / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['format_version'] = 2
    data['state'].pop('pipeline', None)
    path.write_text(json.dumps(data), encoding='utf-8')
    upgrade_snapshot(str(source), str(destination))
    recovered = Memory.open(str(destination))
    restored = recovered.episodes_doc.get(ep.id)
    assert restored.content == ep.content and restored.event_time is None
    assert restored.message_id is None and restored.sequence is None
    assert recovered._pipeline_state is None
    assert recovered.fact_store.values()[0].source == 'user'


@pytest.mark.parametrize('mutation', ['position', 'role', 'time', 'length', 'part_text', 'part_range', 'receipt_type', 'summary_receipt'])
def test_restore_binds_coverage_and_summary_parts_to_the_committed_source(mutation):
    mem = Memory(llm=FakeLLM(handler=respond))
    ep = mem.add(LONG)
    mem.consolidate([ep])
    mem.summarize_episodes([ep])
    state = deepcopy(mem._pipeline_state)
    data = state['namespaces']['default']
    row = data['sources'][ep.id]
    win = next(iter(data['windows'].values()))
    if mutation == 'summary_receipt':
        row['summary_receipt'] = None
    elif mutation == 'receipt_type':
        row['commits'][0]['erased_sources'] = None
    elif mutation == 'position':
        row['position'] = 0
    elif mutation.startswith('part_'):
        part = row['parts'][0]
        part['text' if mutation == 'part_text' else 'start'] = 'forged' if mutation == 'part_text' else 1
    else:
        from mnemotrace.consolidate.pipeline import digest
        view = win['input']['pending'][0]
        view[{'role': 'role', 'time': 'event_time', 'length': 'source_length'}[mutation]] = 'user' if mutation == 'role' else 42
        win['input_hash'] = digest(win['input'])
    with pytest.raises(ValueError):
        validate_state(state, mem.episodes_doc.values(), mem.embedder.dim)


@pytest.mark.parametrize('other_nature,expected_calls', [('assistant_suggestion', 1), ('user_statement', 2)])
def test_optional_conflict_checks_are_ledgered_and_cannot_cross_assertion_nature(other_nature, expected_calls):
    from mnemotrace.types import Fact
    def model(prompt, system):
        if WINDOW_PREFIX in prompt:
            return respond(prompt, system)
        data = json.loads(prompt)
        pair = data['candidates'][0]
        return json.dumps({'decisions': [dict(older=pair['older']['id'], newer=pair['newer']['id'],
            decision='replaces', reason='synthetic same-slot update')]})
    llm = FakeLLM(handler=model)
    mem = Memory(llm=llm, config=Config(conflict_llm_escalation=True))
    older = Fact('u', 'lives_in', 'City', user_id='u', valid_at=100, assertion_kind='user_statement')
    newer = Fact('u', 'based_in', 'City', user_id='u', valid_at=200, assertion_kind=other_nature)
    for fact in (older, newer):
        fact.embedding = mem.embedder.embed(fact.text)
        mem.fact_store.upsert(fact.id, fact.embedding, fact)
    ep = mem.add('Synthetic evidence', user_id='u')
    mem.consolidate([ep])
    assert len(llm.calls) == expected_calls
    assert (mem.fact_store.get(older.id).invalid_at is not None) == (expected_calls == 2)
    windows = PipelineTasks(mem, 'u').state['windows'].values()
    assert sum(w['operation'] == 'conflicts' for w in windows) == expected_calls - 1
    assert all(w['attempts'] == 1 for w in windows)


def test_erasing_one_committed_source_removes_cached_text_without_losing_other_coverage(services):
    svc, _ = services(buffered=True)
    source = svc.remember_messages('u', [dict(message_id='a', role='user', content='unique erased text'),
        dict(message_id='b', role='user', content='remaining source')], 's')
    assert svc.flush('u')['ok']
    erased, kept = [item['episode_id'] for item in source['messages']]
    with svc.write_lock('u'):
        mem = svc.get('u')
        mem.episodes_doc.delete(erased)
        svc._save('u', mem)
    mem = svc.get('u')
    assert 'unique erased text' not in json.dumps(mem._pipeline_state)
    assert mem.episodes_doc.get(kept).consolidated
    validate_state(mem._pipeline_state, mem.episodes_doc.values(), mem.embedder.dim)
    copied = deepcopy(mem._pipeline_state)
    copied['namespaces']['u']['sources'][kept]['commits'][0]['erased_sources'] = []
    with pytest.raises(ValueError):
        validate_state(copied, mem.episodes_doc.values(), mem.embedder.dim)


def test_explicit_dialogue_summary_has_its_own_cropped_proof():
    from mnemotrace.retrieve.events import event_evidence
    mem = Memory(llm=FakeLLM(handler=respond), config=Config(summary_max_input_bytes=6500))
    source = mem.remember_messages([dict(message_id='a', role='user', content='长原文' * 4000)], 's', scope='raw')
    ep = mem.episodes_doc.get(source['messages'][0]['episode_id'])
    assert mem.summarize_episodes([ep]) == 1
    assert ep.coverage is None and ep.summary_coverage['partial']
    evidence = event_evidence(ep)
    assert evidence.partial and evidence.derived and evidence.text == SUMMARY
    assert sum(b-a for a, b in evidence.coverage['ranges']) < len(ep.content)


def test_document_save_only_intent_is_preserved_when_closing(services):
    svc, model = services()
    svc.import_document('u', 'Synthetic offline document', filename='synthetic.txt',
                        consolidate=False, summarize=False)
    assert not model.calls
    result = svc.close_session('u', 'document', summarize=False, outcomes=False)
    assert result['ok'] and result['facts_added'] == 0 and not model.calls
    ep = svc.get('u').episodes_doc.values()[0]
    assert ep.source_kind == 'document' and ep.scope == 'raw' and ep.coverage is None


def test_numeric_timestamp_representation_does_not_change_message_identity():
    mem = Memory()
    msg = dict(message_id='a', role='user', content='Synthetic source', event_time=100)
    original = mem.remember_messages([msg], 's')
    repeated = mem.remember_messages([{**msg, 'event_time': 100.0}], 's')
    assert repeated['accepted'] == 0
    assert repeated['messages'] == original['messages']


def test_short_document_save_only_does_not_require_a_summary_model(services):
    svc, model = services()
    svc.llm = None
    result = svc.import_document('u', 'Synthetic short original', filename='short.txt', consolidate=False)
    assert result['ok'] and result['facts_added'] == result['summaries'] == 0
    assert not model.calls and len(svc.get('u').episodes_doc.values()) == 1


@pytest.mark.parametrize('entry', ['raw', 'document'])
def test_native_source_write_failure_discards_dirty_originals(tmp_path, monkeypatch, entry):
    mem = Memory.open(str(tmp_path / 'store'))
    mem.add_fact('owner', 'owns', 'existing synthetic fact')
    mem.save()
    original = mem.ingestor.ingest
    def partial(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('synthetic original save failed')
    monkeypatch.setattr(mem.ingestor, 'ingest', partial)
    with pytest.raises(OSError):
        if entry == 'raw':
            mem.remember('new synthetic original')
        else:
            mem.import_messages([dict(session_id='s', messages=[dict(role='document', content='synthetic document')])],
                                consolidate=False, summarize=False)
    assert not mem.episodes_doc.values() and len(mem.fact_store.values()) == 1
    assert not mem.working_mem and mem._pipeline_state is None


def test_native_close_rejects_raw_acceptance():
    mem = Memory()
    mem.close()
    with pytest.raises(RuntimeError, match='closed'):
        mem.remember('synthetic source')
    assert not mem.episodes_doc.values()


def test_erasure_removes_dependent_summary_merge_inputs(services):
    svc, _ = services(buffered=True)
    svc.config.extraction_max_input_bytes = 6500
    svc.config.summary_max_input_bytes = 6500
    first = svc.remember('u', 'unique earlier original', session_id='s', scope='long')
    second = svc.remember('u', LONG * 2, session_id='s', scope='long', processing='buffered')
    svc.flush('u', timeout=0)
    drain(svc)
    assert any(w['input']['parts'] for w in PipelineTasks(svc.get('u'), 'u').state['windows'].values())
    with svc.write_lock('u'):
        mem = svc.get('u')
        mem.episodes_doc.delete(first['episode_id'])
        svc._save('u', mem)
    mem = svc.get('u')
    state = PipelineTasks(mem, 'u').state
    assert 'unique earlier original' not in json.dumps(state)
    assert all(not w['input']['parts'] for w in state['windows'].values())
    assert state['sources'][second['episode_id']]['summary_receipt']['erased_sources'] == [first['episode_id']]
    assert mem.episodes_doc.get(second['episode_id']).consolidated
    validate_state(mem._pipeline_state, mem.episodes_doc.values(), mem.embedder.dim)


def test_cross_source_commit_receipt_is_rejected_without_replacing_snapshot(tmp_path):
    mem = Memory.open(str(tmp_path / 'store'), llm=FakeLLM(handler=respond))
    first, second = mem.add('alpha'), mem.add('bravo')
    mem.consolidate([first, second])
    path = tmp_path / 'store' / 'manifest.json'
    before = path.read_bytes()
    tasks = PipelineTasks(mem, 'default')
    wrong = tasks.rows()[second.id]['commits'][0]
    tasks.rows()[first.id]['commits'][0].update(key=wrong['key'], input_hash=wrong['input_hash'])
    with pytest.raises(StoreFormatError, match='checkpoint'):
        mem.save()
    assert path.read_bytes() == before


def test_canonical_raw_fact_keeps_sensitive_display_out_of_safe_exports(services):
    def private_response(prompt, system):
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
        return json.dumps(dict(facts=[dict(subject='user', predicate='project_decision', object='synthetic option',
            text='The password is SYNTHETIC_VALUE', assertion_kind='unspecified',
            source_ids=[data['pending'][0]['id']])], working=[], summaries=[]))
    svc, _ = services(private_response)
    svc.remember('u', 'Synthetic source about a deployment')
    fact, = svc.get('u').fact_store.values()
    assert 'password' not in fact.text and 'password' in fact.display
    assert fact.sensitive
    assert svc.export('u')['facts'] == [] and svc.memories('u')['facts'] == []


@pytest.mark.parametrize('kind', ['raw', 'dialogue'])
def test_third_party_name_never_becomes_the_owner_identity(kind):
    def names(prompt, system):
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
        nature = 'unspecified' if kind == 'raw' else 'user_statement'
        source_id = data['pending'][0]['id']
        return json.dumps(dict(facts=[dict(subject='friend', predicate='name', object='Alice',
            text='The friend is Alice', assertion_kind=nature, source_ids=[source_id]),
            dict(subject='user', predicate='owns', object='bicycle', text='I own a bicycle',
                 assertion_kind=nature, source_ids=[source_id])], working=[], summaries=[]))
    mem = Memory(llm=FakeLLM(handler=names))
    if kind == 'raw':
        mem.add('Synthetic owner and friend')
        mem.consolidate()
    else:
        mem.remember_messages([dict(message_id='a', role='user', content='Synthetic owner and friend')], 's')
        assert mem.flush_messages()['ok']
    assert not mem._identity and not mem._aliases
    facts = mem.fact_store.values()
    assert next(f for f in facts if f.predicate == 'owns').subject == 'default'
    assert next(f for f in facts if f.predicate == 'name').subject == 'friend'


@pytest.mark.parametrize('mutation', ['ranges', 'input_hash', 'policy'])
def test_cropped_dialogue_coverage_must_match_its_commit_receipt(mutation):
    mem = Memory(llm=FakeLLM(handler=respond), config=Config(extraction_max_input_bytes=6500))
    accepted = mem.remember_messages([dict(message_id='a', role='user', content=LONG * 3)], 's')
    assert mem.flush_messages()['ok']
    ep = mem.episodes_doc.get(accepted['messages'][0]['episode_id'])
    assert ep.coverage['partial']
    if mutation == 'ranges':
        ep.coverage.update(ranges=[[0, len(ep.content)]], partial=False, policy='full_message_v1')
    elif mutation == 'input_hash':
        ep.coverage['input_hash'] = '0' * 64
    else:
        ep.coverage['policy'] = 'full_source_v1'
    with pytest.raises(ValueError):
        validate_state(mem._pipeline_state, mem.episodes_doc.values(), mem.embedder.dim)


def test_erased_shared_window_allows_explicit_retry_of_surviving_summary(services):
    missing = True
    def model(prompt, system):
        return json.dumps(dict(facts=[], working=[], summaries=[])) if missing else respond(prompt, system)
    svc, _ = services(model, buffered=True)
    accepted = svc.remember_messages('u', [dict(message_id='a', role='user', content='erase this source'),
        dict(message_id='b', role='user', content=LONG[:2200])], 's')
    svc.flush('u', timeout=0)
    assert run_one(svc, 'u') and run_one(svc, 'u')
    erased, kept = [item['episode_id'] for item in accepted['messages']]
    with svc.write_lock('u'):
        mem = svc.get('u')
        assert PipelineTasks(mem, 'u').rows()[kept]['core'] == 'completed'
        mem.episodes_doc.delete(erased)
        svc._save('u', mem)
    status = svc.processing_status('u')
    assert next(j for j in status['jobs'] if j['episode_id'] == kept)['state'] == 'failed'
    calls = len(svc.llm.calls)
    drain(svc)
    assert len(svc.llm.calls) == calls
    missing = False
    svc.flush('u', timeout=0, retry_failed=True)
    drain(svc)
    ep = svc.get('u').episodes_doc.get(kept)
    assert ep.consolidated and ep.summary == SUMMARY
    assert len(svc.llm.calls) == calls + 1
