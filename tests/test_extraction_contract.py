"""Native production contracts; these tests deliberately bypass fixture adapters."""
import json

import pytest

from mnemotrace import Memory
from mnemotrace.extraction import ExtractionError, ExtractionResponseError, LLMNotConfiguredError
from mnemotrace.llm import FakeLLM as NativeFakeLLM, Completion
from mnemotrace.service import MemoryService
from mnemotrace.consolidate.planning import WINDOW_PREFIX


class FakeLLM(NativeFakeLLM):
    """Attach the fixed row fixture to the actual source IDs supplied to this call."""
    def complete_result(self, prompt, system=None, **kwargs):
        completion = super().complete_result(prompt, system=system, **kwargs)
        if WINDOW_PREFIX not in prompt:
            return completion
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
        try:
            out = json.loads(completion.text)
        except ValueError:
            return completion
        if not isinstance(out, dict) or set(out) != {'facts', 'working'}:
            return completion
        for group in ('facts', 'working'):
            for row in out[group]:
                row.setdefault('source_ids', [data['pending'][0]['id']])
                row.setdefault('assertion_kind', 'unspecified' if data['source_kind'] != 'dialogue' else 'user_statement')
                if group == 'facts' and all(k in row for k in ('subject', 'predicate', 'object')):
                    row.setdefault('text', f"{row['subject']} {row['predicate']} {row['object']}")
        out['summaries'] = []
        return Completion(json.dumps(out), completion.finish_reason, completion.usage)


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
            svc.import_('u', sessions=[{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic input'}]}])
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
    m.llm = FakeLLM(responses=[response()] * count)
    assert m.flush_messages(timeout=2, retry_failed=True)['ok']
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


@pytest.mark.parametrize(('facts', 'working', 'expected'), [
    ([fact()], [], 'long'), ([], [{'content': 'Temporary task', 'kind': 'task'}], 'working'),
    ([fact()], [{'content': 'Temporary task', 'kind': 'task'}], 'mixed'), ([], [], 'none'),
])
def test_auto_routes_in_one_model_call(tmp_path, facts, working, expected):
    model = FakeLLM(responses=[response(facts, working)])
    svc = service(tmp_path, model)
    result = svc.remember('u', 'I moved to Beijing today and will live here permanently.')
    # Summary generation is a separate existing operation, not a routing call.
    assert len([c for c in model.calls if WINDOW_PREFIX in c['prompt']]) == 1
    assert result['scope'] == expected
    assert result['extracted'] == len(facts) and result['working_added'] == len(working)


def test_service_failure_keeps_raw_on_disk_and_retries_after_restart(tmp_path, monkeypatch):
    from mnemotrace._pipeline_worker import PipelineWorker
    from mnemotrace._pipeline import run_one
    # Drive the pipeline deterministically; a live worker races the flush wait on
    # slow hosts and returns timed_out before the retry window is picked up.
    monkeypatch.setattr(PipelineWorker, 'consume', lambda _: None)
    svc = service(tmp_path, Broken())
    with pytest.raises(ExtractionError) as error:
        svc.remember('u', 'Synthetic input', session_id='s')
    assert error.value.stored_raw and len(error.value.episode_ids) == 1
    restarted = service(tmp_path, FakeLLM(responses=[response([fact()])]))
    restarted.flush('u', 's', retry_failed=True, timeout=0)
    while run_one(restarted, 'u'):
        pass
    result = restarted.flush('u', 's')
    assert len(restarted.get('u').fact_store.values()) == 1
    assert result['ok'] and restarted.flush('u', 's')['ok']
    assert len(restarted.get('u').fact_store.values()) == 1


def test_identity_survives_restart_without_model(tmp_path):
    m = Memory(llm=FakeLLM(responses=[response([fact('Synthetic Name', 'name'), fact()])]))
    m.add('Synthetic identity')
    m.consolidate()
    m.save(str(tmp_path))
    assert Memory.open(str(tmp_path))._identity.get('default') == 'Synthetic Name'


def test_preference_embedding_uses_final_normalized_text():
    m = Memory(llm=FakeLLM(responses=[response([fact('the idea of tea', 'likes')])]))
    m.add('Synthetic preference')
    m.consolidate()
    stored, = m.fact_store.values()
    assert stored.object == 'tea'
    assert stored.embedding == m.embedder.embed(stored.text)


def test_duplicate_import_requires_explicit_retry_without_extra_episodes():
    from tests.test_message_pipeline import reply
    def denied(*args):
        raise PermissionError('synthetic authentication failure')
    m = Memory(llm=FakeLLM(handler=denied))
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic record'}]}]
    m.remember_messages(sessions[0]['messages'], 's', mode='historical')
    # A deliberately failed checkpoint is retried only by an explicit flush request.
    from mnemotrace._pipeline import flush_memory
    assert not flush_memory(m, 'default', timeout=.01)['ok']
    m.llm = FakeLLM(handler=reply)
    stats = m.import_messages(sessions)
    assert stats['skipped'] == 1 and stats['facts_added'] == 0
    assert m.flush_messages(retry_failed=True)['ok']
    assert len(m.episodes_doc.values()) == 1 and len(m.fact_store.values()) == 1


def test_failed_core_flush_preserves_working(tmp_path):
    svc = service(tmp_path, Broken())
    svc.remember('u', 'Synthetic temporary task', session_id='s', scope='working')
    with pytest.raises(ExtractionError):
        svc.remember('u', 'Synthetic core input', session_id='s', scope='long')
    assert not svc.flush('u', 's', timeout=0)['ok']
    assert len(svc.get('u').working_memory('u', 's')) == 1


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
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1')
    svc = service(tmp_path, Broken())
    try:
        result = svc.remember('u', 'Synthetic input', session_id='s', processing='buffered')
        assert result['queued'] and result['scope'] == 'auto'
        svc.flush('u', timeout=1)
        ep, = svc.get('u').episodes_doc.values()
        assert not ep.consolidated
        assert svc.processing_status('u')['jobs'][0]['error_code'] == 'extraction_failed'
        assert svc._pipeline_worker.thread.is_alive()
    finally:
        svc.close()


def test_working_retry_is_idempotent_after_restore(tmp_path):
    model = FakeLLM(handler=lambda *args: response([], [{'content': 'task', 'kind': 'task'}]))
    mem = Memory(llm=model)
    result = mem.remember('Synthetic task')
    mem.consolidate()
    mem.save(str(tmp_path))
    restored = Memory.open(str(tmp_path), llm=model)
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
    from mnemotrace.consolidate.planning import WINDOW_PREFIX
    def model(prompt, system):
        if WINDOW_PREFIX not in prompt:
            return 'Synthetic summary'
        payload = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
        return response([{**fact(), 'source_ids': [payload['pending'][0]['id']]}])
    svc = service(tmp_path, FakeLLM(handler=model))
    svc.remember('u', 'Synthetic first statement')
    result = svc.remember('u', 'Synthetic repeated statement')
    assert result['scope'] == 'long' and result['extracted'] == 0


def test_sync_counts_only_include_current_input(tmp_path):
    def complete(prompt, system=''):
        from mnemotrace.consolidate.planning import WINDOW_PREFIX
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
    assert len(svc.get('u').working_mem) == 1 and result['total_facts'] == 1
    assert len(svc.get('u').ingestor.pending()) == 1


def test_long_mode_rejects_working_output():
    mem = Memory(llm=FakeLLM(responses=[response([], [{'content': 'task', 'kind': 'task'}])]))
    ep = mem.add('Synthetic long input')
    with pytest.raises(ExtractionResponseError):
        mem.consolidate()
    assert not ep.consolidated and not mem.working_mem


def test_consolidation_sweeps_consumed_working():
    mem = Memory()
    item = mem.remember_working('Synthetic finished task')
    item.consumed = True
    mem.consolidate([])
    assert not mem.working_mem


def test_explicit_working_can_be_flushed_without_model(tmp_path):
    svc = service(tmp_path, None)
    svc.remember('u', 'Synthetic explicit task', session_id='s', scope='working')
    assert svc.flush('u', 's')['ok']
    assert len(svc.get('u').working_mem) == 1
    assert svc.stats('u')['counts']['extraction_failures'] == 0


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


@pytest.mark.parametrize('has_new_session', [False, True])
def test_import_receipts_do_not_restart_failed_joint_work(tmp_path, has_new_session, monkeypatch):
    from mnemotrace._pipeline_worker import PipelineWorker
    from mnemotrace._pipeline import run_one
    from tests.test_message_pipeline import reply
    monkeypatch.setattr(PipelineWorker, 'consume', lambda _: None)
    model = FakeLLM(handler=lambda *args: '{')
    svc = service(tmp_path, model)
    stamp = [1000.0]
    svc._pipeline_worker.clock = lambda: stamp[0]
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user',
                  'content': 'Synthetic long record ' * 120}]}]
    assert svc.import_('u', sessions=sessions)['queued']
    svc.flush('u', 's', timeout=0)
    for delay in (0, 30, 120):
        stamp[0] += delay
        while run_one(svc, 'u'):
            pass
    assert len(model.calls) == 3
    if has_new_session:
        sessions.append({'session_id': 't', 'messages': [{'message_id': 'm2', 'role': 'user', 'content': 'Synthetic new record'}]})
    repeated = svc.import_('u', sessions=sessions)
    assert repeated['episodes'] == int(has_new_session) and repeated['skipped'] == 1
    assert len(model.calls) == 3
    model._handler = reply
    svc.flush('u', 's', timeout=0, retry_failed=True)
    while run_one(svc, 'u'):
        pass
    assert svc.processing_status('u', 's')['jobs'][0]['state'] == 'completed'
    svc.close()


@pytest.mark.parametrize('layer', ['core', 'service'])
@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('summarize', [False, True])
def test_completed_duplicate_import_does_not_require_model(tmp_path, layer, consolidate, summarize):
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic completed record'}]}]
    model = FakeLLM(handler=reply)
    if layer == 'core':
        initial = Memory(llm=model)
        initial.import_messages(sessions, user_id='u')
        initial.save(str(tmp_path))
        mem = Memory.open(str(tmp_path))
        result = mem.import_messages(sessions, user_id='u', consolidate=consolidate, summarize=summarize)
    else:
        initial = service(tmp_path, model)
        initial.import_('u', sessions=sessions, consolidate=True)
        initial.close()
        restarted = service(tmp_path, None)
        result = restarted.import_('u', sessions=sessions, consolidate=consolidate, summarize=summarize)
        mem = restarted.get('u')
    assert result['skipped'] == 1 and result['episodes'] == 0 and result['facts_added'] == 0
    assert result['summaries'] == 0 and len(mem.fact_store.values()) == 1


@pytest.mark.parametrize('layer', ['core', 'service'])
@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('new_input', [False, True])
def test_import_preflight_checks_only_new_automatic_input(tmp_path, layer, consolidate, pending, new_input):
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic existing record'}]}]
    initial = Memory(llm=FakeLLM(handler=reply)) if layer == 'core' else service(tmp_path, FakeLLM(handler=reply))
    if layer == 'core':
        initial.remember_messages(sessions[0]['messages'], 's', 'u', mode='historical')
        if not pending:
            assert initial.flush_messages('u')['ok']
        initial.save(str(tmp_path))
        mem = Memory.open(str(tmp_path))
        invoke = lambda: mem.import_messages(sessions, user_id='u', consolidate=consolidate)
    else:
        with initial.write_lock('u'):
            mem = initial.get('u')
            mem.remember_messages(sessions[0]['messages'], 's', 'u', mode='historical')
            initial._save('u', mem)
        if not pending:
            assert initial.flush('u')['ok']
        initial.close()
        restarted = service(tmp_path, None)
        mem = restarted.get('u')
        invoke = lambda: restarted.import_('u', sessions=sessions, consolidate=consolidate)
    if new_input:
        sessions.append({'session_id': 'new', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic new record'}]})
        with pytest.raises(LLMNotConfiguredError):
            invoke()
    else:
        assert invoke()['skipped'] == 1
    assert len(mem.episodes_doc.values()) == 1


@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('sessions', [[], [{'session_id': 's', 'messages': [{'content': '  '}]}]])
def test_empty_import_does_not_require_model(consolidate, sessions):
    mem = Memory()
    assert mem.import_messages(sessions, consolidate=consolidate)['episodes'] == 0
    assert not mem.episodes_doc.values()


@pytest.mark.parametrize('consolidate', [None, True])
def test_cached_import_respects_disabled_service_model(tmp_path, consolidate):
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic completed record'}]}]
    svc = service(tmp_path, FakeLLM(handler=reply))
    svc.import_('u', sessions=sessions, consolidate=True)
    svc.llm = None
    assert svc.import_('u', sessions=sessions, consolidate=consolidate)['skipped'] == 1
    sessions.append({'session_id': 'new', 'messages': [{'message_id': 'm2', 'role': 'user', 'content': 'Synthetic new record'}]})
    with pytest.raises(LLMNotConfiguredError):
        svc.import_('u', sessions=sessions, consolidate=consolidate)
    assert len(svc.get('u').episodes_doc.values()) == 1


@pytest.mark.parametrize('consolidate', [None, True])
def test_import_intent_change_requires_new_message_id(tmp_path, consolidate):
    from mnemotrace.consolidate.pipeline import MessageConflictError
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic pending record'}]}]
    svc = service(tmp_path, None)
    svc.import_('u', sessions=sessions, consolidate=False)
    svc.llm = FakeLLM(handler=reply)
    with pytest.raises(MessageConflictError):
        svc.import_('u', sessions=sessions, consolidate=consolidate)
    assert len(svc.get('u').episodes_doc.values()) == 1
    sessions[0]['messages'][0]['message_id'] = 'm2'
    svc.import_('u', sessions=sessions, consolidate=True)
    assert svc.processing_status('u')['jobs'][0]['core'] == 'not_requested'
    assert svc.processing_status('u')['jobs'][1]['core'] == 'completed'

def test_auto_working_memory_gets_config_ttl_and_expires():
    """#9：自动抽取的工作记忆必须带墙钟过期时间，否则客户端不关闭会话会永久累积。"""
    model = FakeLLM(responses=[response(working=[{'content': 'Temporary task', 'kind': 'task'}])])
    m = Memory(llm=model)
    assert m.config.working_memory_ttl_seconds is not None
    m.remember_messages([{'message_id': 'a', 'role': 'user', 'content': 'Please migrate the database next week.', 'event_time': 1.0}], 's', 'u')
    assert m.flush_messages('u')['ok']
    item, = m.working_mem.values()
    assert item.expires_at is not None and item.expires_at > item.created_at
    # 到点后清扫确实删除；未到点保留
    assert m.sweep_working(as_of=item.expires_at - 1) == 0
    assert m.sweep_working(as_of=item.expires_at) == 1
    assert not m.working_mem


def test_auto_working_memory_ttl_can_be_disabled():
    """TTL 设为 None 时保留旧行为（仅随会话清除），不强行设 expires_at。"""
    model = FakeLLM(responses=[response(working=[{'content': 'Temporary task', 'kind': 'task'}])])
    m = Memory(llm=model)
    m.config.working_memory_ttl_seconds = None
    m.remember_messages([{'message_id': 'a', 'role': 'user', 'content': 'Please migrate the database next week.', 'event_time': 1.0}], 's', 'u')
    assert m.flush_messages('u')['ok']
    item, = m.working_mem.values()
    assert item.expires_at is None
