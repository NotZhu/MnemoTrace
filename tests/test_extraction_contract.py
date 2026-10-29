"""Native production contracts; these tests deliberately bypass fixture adapters."""
import json

import pytest

from mnemotrace import Memory
from mnemotrace.extraction import ExtractionError, ExtractionResponseError, LLMNotConfiguredError
from mnemotrace.llm import FakeLLM
from mnemotrace.service import MemoryService
from mnemotrace.types import Episode, Fact
from mnemotrace.consolidate.conflict import ConflictResolver
from mnemotrace.consolidate.detect import detect_conflicts
from mnemotrace.consolidate.llm_extractor import parse_json_facts


def response(facts=(), working=()):
    return json.dumps({'facts': list(facts), 'working': list(working)}, ensure_ascii=False)


def fact(obj='Beijing', predicate='lives_in'):
    return {'subject': 'user', 'predicate': predicate, 'object': obj, 'text': 'Synthetic statement'}


class Broken:
    def complete(self, *args, **kwargs):
        raise RuntimeError('SYNTHETIC_SECRET_MUST_NOT_ESCAPE')


def service(tmp_path, model):
    svc = MemoryService(data_dir=str(tmp_path), embedder_name='hashing', llm_name='')
    svc.llm = model
    return svc


def test_raw_storage_and_manual_facts_do_not_need_model(tmp_path):
    m = Memory()
    m.add('Synthetic raw record')
    m.add_fact('user', 'lives_in', 'Beijing')
    m.save(str(tmp_path))
    reopened = Memory.open(str(tmp_path))
    assert reopened.search('Where do I live?').answer() == 'Beijing'
    with pytest.raises(LLMNotConfiguredError):
        reopened.consolidate()
    assert reopened.consolidate([])['facts_added'] == 0


@pytest.mark.parametrize('operation', ['remember', 'add', 'import', 'document'])
def test_missing_model_rejected_before_input_write(tmp_path, operation):
    svc = service(tmp_path, None)
    mem = Memory()
    with pytest.raises(LLMNotConfiguredError) as error:
        if operation == 'remember':
            svc.remember('u', 'Synthetic input')
        elif operation == 'add':
            mem.add('Synthetic input', consolidate=True)
        elif operation == 'import':
            svc.import_('u', sessions=[{'messages': [{'content': 'Synthetic input'}]}])
        else:
            svc.import_document('u', 'Synthetic input', filename='input.txt')
    assert error.value.stored_raw is False
    assert not svc.get('u').episodes_doc.values()
    assert not mem.episodes_doc.values()


def test_explicit_working_works_without_model(tmp_path):
    svc = service(tmp_path, None)
    result = svc.remember('u', 'Synthetic task', scope='working')
    assert result['scope'] == 'working' and result['working_added'] == 1
    assert not svc.get('u').ingestor.pending()


def test_blank_model_name_is_missing_configuration(tmp_path):
    svc = MemoryService(data_dir=str(tmp_path), embedder_name='hashing', llm_name='   ')
    with pytest.raises(LLMNotConfiguredError):
        svc.remember('u', 'Synthetic input')


def test_direct_extractor_requires_model():
    from mnemotrace.consolidate.llm_extractor import LLMExtractor
    with pytest.raises(LLMNotConfiguredError):
        LLMExtractor(None).extract(Episode('Synthetic input'))


@pytest.mark.parametrize('count', [1, 2, 4])
def test_failed_batch_has_no_commits_and_can_retry(count):
    m = Memory(llm=Broken())
    episodes = [m.add(f'Synthetic row {i}') for i in range(count)]
    with pytest.raises(ExtractionError) as error:
        m.consolidate()
    assert error.value.code == 'extraction_failed'
    assert 'SECRET' not in str(error.value)
    assert all(not ep.consolidated for ep in episodes)
    assert not m.fact_store.values() and not m.working_mem and not m._identity
    m.engine.llm = m.engine.extractor.llm = FakeLLM(responses=[response()] * count)
    assert m.consolidate()['facts_added'] == 0
    assert all(ep.consolidated and 'extraction_error' not in ep.metadata for ep in episodes)


@pytest.mark.parametrize('bad', ['', '[]', '{}', '{"facts":[],"working":[]',
                                 '[{"facts":[],"working":[]}]',
                                 '{"facts":[{}],"working":[]}',
                                 '{"facts":[],"working":[{"content":"x","kind":[]}]}',
                                 '{"facts":[],"working":[{"content":"x","kind":"task","ttl":999}]}',
                                 '{"facts":[],"working":[{"content":"x","kind":"bad"}]}'])
def test_invalid_output_is_not_successful_empty_result(bad):
    m = Memory(llm=FakeLLM(responses=[bad]))
    ep = m.add('Synthetic input')
    with pytest.raises(ExtractionResponseError):
        m.consolidate()
    assert not ep.consolidated


def test_mixed_batch_failure_does_not_register_name_or_write_working():
    def complete(prompt, system=None, **kwargs):
        if 'first' in prompt:
            return response([fact('Synthetic Name', 'name')], [{'content': 'Temporary task', 'kind': 'task'}])
        return 'invalid'
    m = Memory(llm=FakeLLM(handler=complete))
    m.remember('first', scope='auto', session_id='s1')
    m.remember('second', scope='auto', session_id='s2')
    with pytest.raises(ExtractionResponseError):
        m.consolidate()
    assert not m.engine.extractor.self_name and not m._identity and not m.working_mem
    assert len(m.ingestor.pending()) == 2


@pytest.mark.parametrize(('facts', 'working', 'expected'), [
    ([fact()], [], 'long'), ([], [{'content': 'Temporary task', 'kind': 'task'}], 'working'),
    ([fact()], [{'content': 'Temporary task', 'kind': 'task'}], 'mixed'), ([], [], 'none'),
])
def test_auto_routes_in_one_model_call(tmp_path, facts, working, expected):
    model = FakeLLM(responses=[response(facts, working)])
    svc = service(tmp_path, model)
    result = svc.remember('u', 'I moved to Beijing today and will live here permanently.')
    # Summary generation is a separate existing operation, not a routing call.
    assert len([c for c in model.calls if 'JSON 事实' in c['prompt']]) == 1
    assert result['scope'] == expected
    assert result['extracted'] == len(facts) and result['working_added'] == len(working)


def test_service_failure_keeps_raw_on_disk_and_retries_after_restart(tmp_path):
    svc = service(tmp_path, Broken())
    with pytest.raises(ExtractionError) as error:
        svc.remember('u', 'Synthetic input', session_id='s')
    assert error.value.stored_raw and len(error.value.episode_ids) == 1
    restarted = service(tmp_path, FakeLLM(responses=[response([fact()])]))
    result = restarted.close_session('u', 's', summarize=False, outcomes=False)
    assert result['facts_added'] == 1
    assert restarted.close_session('u', 's', summarize=False, outcomes=False)['facts_added'] == 0


def test_identity_survives_restart_without_model(tmp_path):
    m = Memory(llm=FakeLLM(responses=[response([fact('Synthetic Name', 'name'), fact()])]))
    m.add('Synthetic identity')
    m.consolidate()
    m.save(str(tmp_path))
    assert Memory.open(str(tmp_path)).engine.self_name('default') == 'Synthetic Name'


def test_preference_embedding_uses_final_normalized_text():
    m = Memory(llm=FakeLLM(responses=[response([fact('the idea of tea', 'likes')])]))
    m.add('Synthetic preference')
    m.consolidate()
    stored, = m.fact_store.values()
    assert stored.object == 'tea'
    assert stored.embedding == m.embedder.embed(stored.text)


def test_duplicate_import_retries_failed_extraction_without_extra_episodes():
    m = Memory(llm=Broken())
    sessions = [{'session_id': 's', 'messages': [{'content': 'Synthetic record'}]}]
    with pytest.raises(ExtractionError):
        m.import_messages(sessions, summarize=False)
    model = FakeLLM(responses=[response([fact()])])
    m.llm = m.engine.llm = m.engine.extractor.llm = model
    stats = m.import_messages(sessions, summarize=False)
    assert stats['skipped'] == 1 and stats['facts_added'] == 1
    assert len(m.episodes_doc.values()) == 1


def test_outcomes_failure_does_not_clear_working(tmp_path):
    svc = service(tmp_path, Broken())
    svc.remember('u', 'Synthetic temporary task', session_id='s', scope='working')
    with pytest.raises(ExtractionError):
        svc.close_session('u', 's')
    assert len(svc.get('u').working_memory('u', 's')) == 1


@pytest.mark.parametrize('verdict', ['does not replace', 'NO REPLACES', '', 'REPLACES.'])
def test_invalid_replacement_verdict_never_invalidates(verdict, caplog):
    resolver = ConflictResolver(llm=FakeLLM(responses=[verdict]))
    assert not resolver._llm_says_replaces(Fact('u', 'x', 'a'), Fact('u', 'y', 'b'))
    assert 'invalid verdict' in caplog.text


@pytest.mark.parametrize(('verdict', 'count'), [('NO CONFLICT', 0), ('CONFLICT', 1), (' coexist ', 0)])
def test_conflict_verdict_is_exact(verdict, count):
    a, b = Fact('u', 'likes', 'coffee'), Fact('u', 'avoids', 'coffee')
    assert len(detect_conflicts([a, b], FakeLLM(responses=[verdict]), 'default', set())) == count


def test_truncated_outcomes_are_invalid():
    with pytest.raises(ExtractionResponseError):
        parse_json_facts('[{"kind":"finding","statement":"Synthetic complete item"},{"kind":')


def test_http_missing_model_and_provider_failure_are_safe(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    monkeypatch.setenv('MNEMOTRACE_OPEN', '1')
    svc = service(tmp_path, None)
    monkeypatch.setattr(appmod, '_svc', svc)
    with TestClient(appmod.app) as client:
        headers = {'Authorization': 'Bearer synthetic-user'}
        rejected = client.post('/v1/remember', json={'content': 'SYNTHETIC_RAW_SECRET'}, headers=headers)
        assert rejected.status_code == 503
        assert rejected.json()['detail']['stored_raw'] is False
        assert not svc.get('synthetic-user').episodes_doc.values()
        svc.llm = Broken()
        svc._discard("synthetic-user")
        failed = client.post('/v1/remember', json={'content': 'SYNTHETIC_RAW_SECRET'}, headers=headers)
        assert failed.status_code == 502 and failed.json()['detail']['stored_raw']
        assert 'SYNTHETIC' not in failed.text
        assert len(svc.get('synthetic-user').ingestor.pending()) == 1


def test_remote_mcp_preserves_safe_extraction_error_details():
    import httpx
    pytest.importorskip('mcp')
    from mnemotrace.mcp.server import _err
    request = httpx.Request('POST', 'https://synthetic.invalid/v1/remember')
    error = ExtractionError(stored_raw=True, episode_ids=['synthetic-episode'])
    response = httpx.Response(502, json={'detail': error.detail()}, request=request)
    rendered = _err(httpx.HTTPStatusError('failed', request=request, response=response))
    assert 'extraction_failed' in rendered and 'synthetic-episode' in rendered


def test_async_failure_is_visible_and_worker_stays_alive(tmp_path, monkeypatch):
    monkeypatch.setenv('MNEMOTRACE_ASYNC_CONSOLIDATION', '1')
    svc = service(tmp_path, Broken())
    try:
        result = svc.remember('u', 'Synthetic input', session_id='s')
        assert result['queued'] and 'scope' not in result
        svc.flush()
        ep, = svc.get('u').episodes_doc.values()
        assert not ep.consolidated and ep.metadata['extraction_error'] == 'extraction_failed'
        assert svc._worker.is_alive()
    finally:
        svc.close()


def test_storage_failure_keeps_entire_batch_pending_and_retry_keeps_working(monkeypatch):
    def complete(prompt, system=''):
        from mnemotrace.consolidate.windows import WINDOW_PREFIX
        window = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        facts, working = [], []
        for span in window['pending']:
            index = '1' if 'row 1' in span['content'] else '0'
            facts.append({**fact(index, 'owns'), 'source_ids': [span['id']]})
            working.append({'content': 'task ' + index, 'kind': 'task', 'source_ids': [span['id']]})
        return response(facts, working)
    mem = Memory(llm=FakeLLM(handler=complete))
    for index in range(2):
        result = mem.remember('row ' + str(index))
        mem.episodes_doc.get(result['episode_id']).event_time = index
    original = mem.fact_store.upsert

    def fail_second(key, vector, item):
        if item.object == '1':
            raise OSError('Synthetic storage failure')
        return original(key, vector, item)

    monkeypatch.setattr(mem.fact_store, 'upsert', fail_second)
    with pytest.raises(OSError):
        mem.consolidate()
    assert len(mem.fact_store.values()) == 1
    assert len(mem.ingestor.pending()) == 2
    assert not mem.working_mem
    monkeypatch.setattr(mem.fact_store, 'upsert', original)
    mem.consolidate()
    assert {w.content for w in mem.working_mem.values()} == {'task 0', 'task 1'}
    assert not mem.ingestor.pending() and len(mem.fact_store.values()) == 2


def test_working_retry_is_idempotent_after_restore(tmp_path):
    model = FakeLLM(handler=lambda *args: response([], [{'content': 'task', 'kind': 'task'}]))
    mem = Memory(llm=model)
    result = mem.remember('Synthetic task')
    mem.consolidate()
    mem.save(str(tmp_path))
    restored = Memory.open(str(tmp_path), llm=model)
    restored.episodes_doc.get(result['episode_id']).consolidated = False
    assert restored.consolidate()['working_added'] == 0
    item, = restored.working_mem.values()
    assert item.metadata['episode_id'] == result['episode_id']


@pytest.mark.parametrize(('facts', 'working', 'expected'), [
    ([fact('it', 'likes')], [], 'none'),
    ([fact('it', 'likes')], [{'content': 'task', 'kind': 'task'}], 'working'),
    ([fact('Synthetic Name', 'name')], [], 'long'),
])
def test_scope_uses_accepted_content_and_identity(tmp_path, facts, working, expected):
    svc = service(tmp_path, FakeLLM(handler=lambda *args: response(facts, working)))
    result = svc.remember('u', 'Synthetic filtered content')
    assert result['scope'] == expected and result['extracted'] == 0
    assert result['working_added'] == len(working)


def test_scope_includes_existing_durable_fact(tmp_path):
    svc = service(tmp_path, FakeLLM(handler=lambda *args: response([fact()])))
    svc.remember('u', 'Synthetic first statement')
    result = svc.remember('u', 'Synthetic repeated statement')
    assert result['scope'] == 'long' and result['extracted'] == 0


def test_sync_counts_only_include_current_input(tmp_path):
    def complete(prompt, system=''):
        from mnemotrace.consolidate.windows import WINDOW_PREFIX
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        facts, working = [], []
        for span in data['pending']:
            index = '1' if 'first pending' in span['content'] else '2'
            facts.append({**fact(index, 'owns'), 'source_ids': [span['id']]})
            working.append({'content': 'task ' + index, 'kind': 'task', 'source_ids': [span['id']]})
        return response(facts, working)
    svc = service(tmp_path, FakeLLM(handler=complete))
    svc.get('u').remember('first pending', user_id='u')
    result = svc.remember('u', 'current input')
    assert result['extracted'] == 1 and result['working_added'] == 1
    assert len(svc.get('u').working_mem) == 2 and result['total_facts'] == 2


def test_long_mode_rejects_working_output():
    mem = Memory(llm=FakeLLM(responses=[response([], [{'content': 'task', 'kind': 'task'}])]))
    ep = mem.add('Synthetic long input')
    with pytest.raises(ExtractionResponseError):
        mem.consolidate()
    assert not ep.consolidated and not mem.working_mem


def test_facts_only_api_never_silently_discards_working():
    from mnemotrace.consolidate.llm_extractor import LLMExtractor
    extractor = LLMExtractor(FakeLLM(responses=[response([], [{'content': 'task', 'kind': 'task'}])]))
    ep = Episode('Synthetic input', metadata={'memory_scope': 'auto'})
    with pytest.raises(ValueError, match='raw_items'):
        extractor.extract(ep)


def test_consolidation_sweeps_consumed_working():
    mem = Memory()
    item = mem.remember_working('Synthetic finished task')
    item.consumed = True
    mem.consolidate([])
    assert not mem.working_mem


def test_close_missing_model_reports_existing_raw(tmp_path):
    svc = service(tmp_path, None)
    result = svc.remember('u', 'Synthetic explicit task', session_id='s', scope='working')
    with pytest.raises(LLMNotConfiguredError) as error:
        svc.close_session('u', 's')
    assert error.value.stored_raw and error.value.episode_ids == [result['episode_id']]
    assert len(svc.get('u').working_mem) == 1
    assert svc.stats('u')['counts']['extraction_failures'] == 1
    assert svc.sessions('u')['sessions'][0]['extraction_failures'] == 1


@pytest.mark.parametrize('endpoint', ['/v1/remember', '/v1/chat/completions'])
def test_invalid_http_scope_is_rejected_before_model_or_input(tmp_path, monkeypatch, endpoint):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc = service(tmp_path, None)
    monkeypatch.setenv('MNEMOTRACE_OPEN', '1')
    monkeypatch.setattr(appmod, '_svc', svc)
    body = {'content': 'Synthetic input', 'scope': 'invalid'} if endpoint == '/v1/remember' else {
        'messages': [{'role': 'user', 'content': 'Synthetic input'}], 'memory': {'scope': 'invalid'}}
    with TestClient(appmod.app) as client:
        result = client.post(endpoint, json=body, headers={'Authorization': 'Bearer synthetic-user'})
        assert result.status_code == 422
        assert not svc.get('synthetic-user').episodes_doc.values()


@pytest.mark.parametrize(('scope', 'facts', 'working'), [
    ('mixed', [fact()], [{'content': 'task', 'kind': 'task'}]), ('none', [], []),
])
def test_mcp_reports_final_scope_and_working(tmp_path, scope, facts, working):
    import asyncio
    from mnemotrace.mcp import server as mcp
    from mnemotrace.mcp.backends import LocalBackend
    svc = service(tmp_path, FakeLLM(handler=lambda *args: response(facts, working)))
    mcp.set_backend(LocalBackend(namespace='u', service=svc))
    try:
        text = asyncio.run(mcp.mnemotrace_remember('Synthetic input'))
        assert 'scope=' + scope in text
        if working:
            assert '1 working item' in text
    finally:
        mcp.set_backend(None)


@pytest.mark.parametrize('result_scope', ['none', 'long', 'working', 'mixed', 'explicit'])
def test_ephemeral_history_counts_content_after_working_is_cleared(tmp_path, result_scope):
    facts = [fact()] if result_scope in {'long', 'mixed'} else []
    working = [{'content': 'Synthetic task', 'kind': 'task'}] if result_scope in {'working', 'mixed'} else []
    svc = service(tmp_path, FakeLLM(handler=lambda *args: response(facts, working)))
    request_scope = 'working' if result_scope == 'explicit' else 'auto'
    svc.remember('u', 'Synthetic routed input', session_id='s', scope=request_scope)
    expected = int(result_scope in {'working', 'mixed', 'explicit'})
    counts = svc.stats('u')['counts']
    assert counts['episodes_ephemeral'] == expected
    assert counts['working_live'] == expected
    svc.clear_working('u', 's')
    counts = svc.stats('u')['counts']
    assert counts['episodes_ephemeral'] == expected and counts['working_live'] == 0


@pytest.mark.parametrize('summarize', [False, True])
@pytest.mark.parametrize('has_new_session', [False, True])
def test_import_retry_completes_requested_summaries(tmp_path, summarize, has_new_session):
    sessions = [{'session_id': 's', 'messages': [{'content': 'Old synthetic record'}]}]
    svc = service(tmp_path, Broken())
    with pytest.raises(ExtractionError):
        svc.import_('u', sessions=sessions)

    def complete(prompt, system=''):
        if 'JSON 事实' in prompt:
            return response([fact('old' if 'Old synthetic' in prompt else 'new', 'owns')])
        return 'Synthetic summary'

    restarted = service(tmp_path, FakeLLM(handler=complete))
    foreign = restarted.get('other').add('Unrelated synthetic record', user_id='other', session_id='s')
    if has_new_session:
        sessions.append({'session_id': 't', 'messages': [{'content': 'New synthetic record'}]})
    count = len(sessions)
    result = restarted.import_('u', sessions=sessions, summarize=summarize)
    assert result['episodes'] == int(has_new_session) and result['skipped'] == 1
    assert result['summaries'] == (count if summarize else 0)
    episodes = restarted.get('u').episodes_doc.values()
    assert len(episodes) == count and all(ep.consolidated for ep in episodes)
    assert sum(bool(ep.summary) for ep in episodes) == (count if summarize else 0)
    assert not foreign.summary
    # A later request can fill summaries even when there is no extraction left to retry.
    repeated = restarted.import_('u', sessions=sessions, summarize=True)
    assert repeated['episodes'] == 0 and repeated['summaries'] == (0 if summarize else count)
    assert restarted.import_('u', sessions=sessions)['summaries'] == 0


@pytest.mark.parametrize('predicate', ['FINDING', 'Finding'])
def test_outcome_dedup_survives_predicate_case_edit_and_restart(tmp_path, predicate):
    statement = 'Synthetic conclusion: use a local queue for background processing.'
    model = FakeLLM(handler=lambda *args: json.dumps([{'kind': 'finding', 'statement': statement}]))
    svc = service(tmp_path, model)
    svc.remember('u', 'Synthetic source event', session_id='s', scope='working')
    assert svc.close_session('u', 's', summarize=False)['outcomes'] == 1
    stored, = svc.get('u').fact_store.values()
    svc.update_fact('u', stored.id, predicate=predicate)
    restarted = service(tmp_path, model)
    for _ in range(2):
        assert restarted.close_session('u', 's', summarize=False)['outcomes'] == 0
    remaining, = restarted.get('u').fact_store.values()
    assert remaining.predicate == predicate and remaining.text == statement


@pytest.mark.parametrize('tool', ['agent_status', 'session_report', 'stats'])
def test_mcp_text_displays_extraction_failures(tmp_path, tool):
    import asyncio
    from mnemotrace.mcp import server as mcp
    from mnemotrace.mcp.backends import LocalBackend
    svc = service(tmp_path, Broken())
    with pytest.raises(ExtractionError):
        svc.remember('u', 'Synthetic failed input', session_id='s')
    mcp.set_backend(LocalBackend(namespace='u', service=svc))
    try:
        function = getattr(mcp, 'mnemotrace_' + tool)
        kwargs = {} if tool == 'stats' else {'session_id': 's'}
        text = asyncio.run(function(**kwargs))
        assert 'extraction failures: 1 episode(s)' in text.lower()
        assert 'Synthetic failed input' not in text
    finally:
        mcp.set_backend(None)


@pytest.mark.parametrize('layer', ['core', 'service'])
@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('summarize', [False, True])
def test_completed_duplicate_import_does_not_require_model(tmp_path, layer, consolidate, summarize):
    sessions = [{'session_id': 's', 'messages': [{'content': 'Synthetic completed record'}]}]
    model = FakeLLM(handler=lambda *args: response([fact()]))
    if layer == 'core':
        initial = Memory(llm=model)
        initial.import_messages(sessions, user_id='u', summarize=False)
        initial.save(str(tmp_path))
        mem = Memory.open(str(tmp_path))
        result = mem.import_messages(sessions, user_id='u', consolidate=consolidate, summarize=summarize)
    else:
        initial = service(tmp_path, model)
        initial.import_('u', sessions=sessions, summarize=False)
        restarted = service(tmp_path, None)
        result = restarted.import_('u', sessions=sessions, consolidate=consolidate, summarize=summarize)
        mem = restarted.get('u')
    assert mem.llm is None
    assert result['skipped'] == 1 and result['episodes'] == 0 and result['facts_added'] == 0
    assert result['summaries'] == int(summarize)
    assert len(mem.episodes_doc.values()) == 1 and len(mem.fact_store.values()) == 1
    assert not mem.ingestor.pending()


@pytest.mark.parametrize('layer', ['core', 'service'])
@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('state', ['pending', 'mixed_pending', 'mixed_complete', 'append'])
def test_import_preflight_checks_actual_new_and_pending_work(tmp_path, layer, consolidate, state):
    sessions = [{'session_id': 's', 'messages': [{'content': 'Synthetic existing record'}]}]
    is_pending = state in {'pending', 'mixed_pending'}
    model = None if is_pending else FakeLLM(handler=lambda *args: response([fact()]))
    if layer == 'core':
        initial = Memory(llm=model)
        initial.import_messages(sessions, user_id='u', consolidate=not is_pending, summarize=False)
        initial.save(str(tmp_path))
        mem = Memory.open(str(tmp_path))
        invoke = lambda rows, **kw: mem.import_messages(rows, user_id='u', **kw)
    else:
        initial = service(tmp_path, model)
        initial.import_('u', sessions=sessions, consolidate=not is_pending, summarize=False)
        restarted = service(tmp_path, None)
        mem = restarted.get('u')
        invoke = lambda rows, **kw: restarted.import_('u', sessions=rows, **kw)
    fingerprints = set(mem._import_seen)
    existing, = mem.episodes_doc.values()
    if state.startswith('mixed'):
        sessions.append({'session_id': 'new', 'messages': [{'content': 'Synthetic new record'}]})
    with pytest.raises(LLMNotConfiguredError) as error:
        invoke(sessions, consolidate=consolidate, summarize=False, dedupe=state != 'append')
    assert error.value.stored_raw is is_pending
    assert error.value.episode_ids == ([existing.id] if is_pending else [])
    assert len(mem.episodes_doc.values()) == 1 and mem._import_seen == fingerprints
    assert bool(mem.ingestor.pending()) is is_pending


@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('sessions', [[], [{'session_id': 's', 'messages': [{'content': '  '}]}]])
def test_empty_import_does_not_require_model(consolidate, sessions):
    mem = Memory()
    result = mem.import_messages(sessions, consolidate=consolidate, summarize=False)
    assert result['episodes'] == 0 and result['facts_added'] == 0
    assert not mem.episodes_doc.values()


@pytest.mark.parametrize('consolidate', [None, True])
def test_cached_import_respects_disabled_service_model(tmp_path, consolidate):
    sessions = [{'session_id': 's', 'messages': [{'content': 'Synthetic completed record'}]}]
    svc = service(tmp_path, FakeLLM(handler=lambda *args: response([fact()])))
    svc.import_('u', sessions=sessions, summarize=False)
    svc.llm = None
    assert svc.import_('u', sessions=sessions, consolidate=consolidate, summarize=False)['skipped'] == 1
    sessions.append({'session_id': 'new', 'messages': [{'content': 'Synthetic new record'}]})
    with pytest.raises(LLMNotConfiguredError):
        svc.import_('u', sessions=sessions, consolidate=consolidate, summarize=False)
    assert svc.get('u').llm is None and len(svc.get('u').episodes_doc.values()) == 1


@pytest.mark.parametrize('consolidate', [None, True])
def test_cached_import_can_enable_model_for_pending_retry(tmp_path, consolidate):
    sessions = [{'session_id': 's', 'messages': [{'content': 'Synthetic pending record'}]}]
    svc = service(tmp_path, None)
    svc.import_('u', sessions=sessions, consolidate=False, summarize=False)
    svc.llm = FakeLLM(handler=lambda *args: response([fact()]))
    result = svc.import_('u', sessions=sessions, consolidate=consolidate, summarize=False)
    assert result['skipped'] == 1 and result['facts_added'] == 1
    assert svc.get('u').llm is svc.llm and not svc.get('u').ingestor.pending()
