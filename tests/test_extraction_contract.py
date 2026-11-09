"""Native production contracts; these tests deliberately bypass fixture adapters."""
import json

import pytest

from mnemotrace import MemoryEngine
from mnemotrace.extraction import ExtractionError, ExtractionResponseError, LLMNotConfiguredError
from mnemotrace.llm import FakeLLM as NativeFakeLLM, Completion
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
        if not isinstance(out, dict) or set(out) != {'facts'}:
            return completion
        for group in ('facts',):
            for row in out[group]:
                row.setdefault('source_ids', [data['pending'][0]['id']])
                if group == 'facts' and all(k in row for k in ('subject', 'predicate', 'object')):
                    row.setdefault('category', '其他')
                    row.setdefault('sensitive', False)
                    row.setdefault('time_kind', 'current')
                    row.setdefault('effective_date', None)
                    row.setdefault('cardinality', 'unknown')
                    row.setdefault('retention', 'unknown')
                    row.setdefault('text', f"{row['subject']} {row['predicate']} {row['object']}")
        out['digests'] = []
        return Completion(json.dumps(out), completion.finish_reason, completion.usage)


def response(facts=()):
    return json.dumps({'facts': list(facts)}, ensure_ascii=False)


def fact(obj='Beijing', predicate='lives_in'):
    return {'subject': 'user', 'predicate': predicate, 'object': obj, 'text': 'Synthetic statement'}


class Broken:
    def complete(self, *args, **kwargs):
        raise RuntimeError('SYNTHETIC_SECRET_MUST_NOT_ESCAPE')


def test_raw_storage_and_manual_facts_do_not_need_model():
    m = MemoryEngine()
    m.add('Synthetic raw record')
    m.add_fact('user', 'lives_in', 'Beijing')
    assert m.search('Where do I live?').answer() == 'Beijing'
    with pytest.raises(LLMNotConfiguredError):
        m.consolidate()
    assert m.consolidate([])['facts_added'] == 0


@pytest.mark.parametrize('count', [1, 2, 4])
def test_failed_batch_has_no_commits_and_can_retry(count):
    m = MemoryEngine(llm=Broken())
    episodes = [m.add(f'Synthetic row {i}') for i in range(count)]
    with pytest.raises(ExtractionError) as error:
        m.consolidate()
    assert error.value.code == 'extraction_failed'
    assert 'SECRET' not in str(error.value)
    assert all(not ep.consolidated for ep in episodes)
    assert not m.fact_store.values() and not m._identity
    m.llm = FakeLLM(responses=[response()] * count)
    assert m.flush_messages(timeout=2, retry_failed=True)['ok']
    assert m.consolidate()['facts_added'] == 0
    assert all(ep.consolidated and 'extraction_error' not in ep.metadata for ep in episodes)


@pytest.mark.parametrize('bad', ['', '[]', '{}', '{"facts":[],',
                                 '[{"facts":[],}]',
                                 '{"facts":[{}],}',
                                 '{"facts":[],"working":[{"content":"x","kind":[]}]}',
                                 '{"facts":[],"working":[{"content":"x","kind":"task","ttl":999}]}',
                                 '{"facts":[],"working":[{"content":"x","kind":"bad"}]}'])
def test_invalid_output_is_not_successful_empty_result(bad):
    m = MemoryEngine(llm=FakeLLM(responses=[bad]))
    ep = m.add('Synthetic input')
    with pytest.raises(ExtractionResponseError):
        m.consolidate()
    assert not ep.consolidated


def test_preference_embedding_uses_final_normalized_text():
    from tests.embedder_fixtures import MockEmbedder

    m = MemoryEngine(embedder=MockEmbedder(), llm=FakeLLM(responses=[response([fact('the idea of tea', 'likes')])]))
    m.add('Synthetic preference')
    m.consolidate()
    stored, = m.fact_store.values()
    assert stored.object == 'tea'
    assert stored.embedding == m.embedder.embed(stored.text)


def test_duplicate_import_requires_explicit_retry_without_extra_episodes():
    from tests.test_message_pipeline import reply
    def denied(*args):
        raise PermissionError('synthetic authentication failure')
    m = MemoryEngine(llm=FakeLLM(handler=denied))
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


def test_remote_mcp_preserves_safe_extraction_error_details():
    import httpx
    pytest.importorskip('mcp')
    from mnemotrace.mcp.server import _err
    request = httpx.Request('POST', 'https://synthetic.invalid/v1/remember')
    error = ExtractionError(stored_raw=True, episode_ids=['synthetic-episode'])
    response = httpx.Response(502, json={'detail': error.detail()}, request=request)
    rendered = _err(httpx.HTTPStatusError('failed', request=request, response=response))
    assert 'extraction_failed' in rendered and 'synthetic-episode' in rendered


@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('generate_digests', [False, True])
def test_completed_duplicate_import_does_not_require_model(consolidate, generate_digests):
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic completed record'}]}]
    model = FakeLLM(handler=reply)
    mem = MemoryEngine(llm=model)
    mem.import_messages(sessions, user_id='u')
    mem.llm = None
    result = mem.import_messages(sessions, user_id='u', consolidate=consolidate, generate_digests=generate_digests)
    assert result['skipped'] == 1 and result['episodes'] == 0 and result['facts_added'] == 0
    assert result['digests'] == 0 and len(mem.fact_store.values()) == 1


@pytest.mark.parametrize('consolidate', [None, True])
@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('new_input', [False, True])
def test_import_preflight_checks_only_new_automatic_input(consolidate, pending, new_input):
    from tests.test_message_pipeline import reply
    sessions = [{'session_id': 's', 'messages': [{'message_id': 'm1', 'role': 'user', 'content': 'Synthetic existing record'}]}]
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages(sessions[0]['messages'], 's', 'u', mode='historical')
    if not pending:
        assert mem.flush_messages('u')['ok']
    mem.llm = None
    def invoke():
        return mem.import_messages(sessions, user_id='u', consolidate=consolidate)
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
    mem = MemoryEngine()
    assert mem.import_messages(sessions, consolidate=consolidate)['episodes'] == 0
    assert not mem.episodes_doc.values()
