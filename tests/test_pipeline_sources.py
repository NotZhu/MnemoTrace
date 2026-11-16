"""Public source processing invariants, using synthetic responses and a controlled worker."""
import json

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.pipeline import PipelineTasks
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM

LONG = 'Synthetic source with a recorded detail. ' * 200
DIGEST = 'A synthetic detail was recorded.'


def boundary_facts(data):
    return [
        dict(subject=f'Synthetic {section}', predicate='contains', object=code,
             text=f'The {section} contains {code}', source_ids=[item['id']],
             categories=[], sensitive=False, time_kind='current', effective_date=None, retention='unknown')
        for item in data['pending']
        for section, code in (('opening', 'HEAD-41'), ('middle', 'MIDDLE-42'), ('closing', 'TAIL-43'))
        if code in item['content']
    ]


def respond(prompt, system=''):
    if '<session>' in prompt:
        return '[]'
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
    facts = boundary_facts(data)
    return json.dumps(dict(facts=facts, digests=[
        dict(source_id=s['id'], text='HEAD-41, MIDDLE-42 and TAIL-43' if facts else DIGEST) for s in data['pending']
        if s['id'] in data['rules']['digest_required']
    ]))


@pytest.mark.parametrize('shorthand', [True, False])
def test_full_original_is_shared_by_facts_and_digest(shorthand):
    from mnemotrace.retrieve.events import event_evidence

    head = 'HEAD-41'
    middle = 'MIDDLE-42'
    tail = 'TAIL-43'
    text = (f'{head} is in Synthetic opening. ' + '长原文' * 4000
            + f' Synthetic middle contains {middle}. ' + '长原文' * 4000
            + f' Synthetic closing contains {tail}')
    model = FakeLLM(handler=respond)
    memory = MemoryEngine(llm=model, config=Config(memory_max_input_tokens=7500))
    value = text if shorthand else [dict(message_id='a', role='user', content=text)]
    result = memory.remember(value, 's')
    source_id, = result['source_ids']
    source = memory.episodes_doc.get(source_id)
    call, = model.calls
    view, = json.loads(call['prompt'].removeprefix(WINDOW_PREFIX))['pending']
    assert view['content'].startswith(head) and view['content'].endswith(tail)
    assert middle in view['content']
    assert source.consolidated and source.coverage == source.digest_coverage
    assert source.coverage['ranges'] == view['ranges'] and not source.coverage['partial']
    assert {fact.object for fact in memory._all_facts()} == {head, middle, tail}
    assert 'policy' not in source.coverage and 'strategy_completed' not in source.coverage
    assert event_evidence(source).text == 'HEAD-41, MIDDLE-42 and TAIL-43'
    assert memory.get_episode(source_id)['content'] == text
    assert memory.consolidate()['digests'] == 0 and len(model.calls) == 1


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'invalid', 'extra', 'truncated'])
def test_invalid_joint_response_retries_entire_window(damage, tmp_path, monkeypatch):
    from mnemotrace._pipeline import flush, run_next_stage
    from mnemotrace.store.contracts import message_inputs
    from mnemotrace.llm.base import Completion
    from mnemotrace.llm.cache import CachedLLM

    monkeypatch.setattr('mnemotrace._pipeline.RETRY_DELAYS', (0, 0))
    captured = []

    def reply(prompt, system):
        captured.append(prompt)
        source, = json.loads(prompt.removeprefix(WINDOW_PREFIX))['pending']
        digest = dict(source_id=source['id'], text=DIGEST)
        result = dict(facts=[dict(subject='我', predicate='likes', object='tea', text='I like tea',
            source_ids=[source['id']], categories=[], sensitive=False, time_kind='current',
            effective_date=None, retention='unknown')], digests=[digest])
        if len(captured) == 1:
            if damage == 'missing':
                result['digests'] = []
            elif damage == 'duplicate':
                result['digests'].append(digest)
            elif damage == 'invalid':
                digest['text'] = ' '
            elif damage == 'extra':
                result['digests'].append(dict(digest, source_id='another-source'))
            else:
                return Completion(json.dumps(result), finish_reason='length')
        return json.dumps(result)

    # Retrying the same prompt must also work with the replay cache enabled.
    memory = MemoryEngine(llm=CachedLLM(FakeLLM(handler=reply), str(tmp_path)))
    tasks = PipelineTasks(memory, 'default')
    source_id, = tasks.accept(message_inputs(LONG, message_id='m'), 's', 100)
    assert run_next_stage(memory, 'default')
    key, = tasks.state['windows']
    first = tasks.state['windows'][key]
    assert first['state'] == 'retry_wait' and first['result'] is None
    assert not memory._all_facts() and not memory.episodes_doc.get(source_id).digest
    assert flush(memory, 'default', 's')['ok']
    assert list(tasks.state['windows']) == [key]
    assert len(captured) == 2 and captured[0] == captured[1]
    assert memory.episodes_doc.get(source_id).digest == DIGEST
    assert len(memory._all_facts()) == 1


def test_joint_retries_exhaust_without_publishing(monkeypatch):
    from mnemotrace.llm.failures import ATTEMPT_LIMIT

    monkeypatch.setattr('mnemotrace._pipeline.RETRY_DELAYS', (0, 0))
    model = FakeLLM(handler=lambda *_: '{"facts":[],"digests":[]}')
    memory = MemoryEngine(llm=model)
    from mnemotrace.extraction import ExtractionResponseError
    with pytest.raises(ExtractionResponseError) as error:
        memory.remember(LONG, 's', message_id='m')
    assert error.value.code == 'extraction_invalid_response'
    source_id, = error.value.episode_ids
    assert len(model.calls) == ATTEMPT_LIMIT
    source = memory.episodes_doc.get(source_id)
    assert source.content == LONG and source.coverage is None and not source.digest
    assert not memory._all_facts()


def test_explicit_retry_ignores_a_superseded_window_after_success():
    from mnemotrace._pipeline import flush, run_next_stage
    from mnemotrace.store.contracts import message_inputs

    model = FakeLLM(handler=respond)
    memory = MemoryEngine(llm=model)
    tasks = PipelineTasks(memory, 'default')
    tasks.accept(message_inputs(LONG, message_id='m'), 's', 100)
    assert run_next_stage(memory, 'default')
    memory.set_policy(extract_instruction='Updated joint instructions')
    assert run_next_stage(memory, 'default')
    assert flush(memory, 'default', 's', retry_failed=True)['ok']
    assert flush(memory, 'default', 's', retry_failed=True)['ok']
    assert len(model.calls) == 2


def test_adjudication_is_ledgered_before_core_commit():
    from tests.test_fact_adjudication import model_response

    llm = FakeLLM(handler=model_response)
    mem = MemoryEngine(llm=llm)
    mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    mem.add('Oslo', event_time=20, consolidate=True)
    win, = PipelineTasks(mem, 'default').state['windows'].values()
    assert len(llm.calls) == win['stats']['model_calls'] == 2
    assert [entry['stage'] for entry in win['history'] if not entry['stage'].startswith('reset')] == [
        'generate', 'embed', 'adjudicate']
    assert win['state'] == 'completed'


def test_numeric_timestamp_representation_does_not_change_message_identity():
    model = FakeLLM(handler=respond)
    mem = MemoryEngine(llm=model)
    original = mem.remember('Synthetic source', 's', message_id='a', event_time=100)
    repeated = mem.remember([dict(message_id='a', role='user', content='Synthetic source', event_time=100.0)], 's')
    assert repeated['source_ids'] == original['source_ids']
    assert len(model.calls) == 1


def test_retransmission_rejects_mutated_or_partial_batch():
    from mnemotrace.consolidate.pipeline import MessageConflictError

    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    batch = [dict(message_id=key, role='user', content=key) for key in ('first', 'second')]
    original = mem.remember(batch, 'chat')
    for changed in (batch[::-1], batch[:1], [batch[0], dict(batch[1], message_id='new')],
                    [dict(batch[0], role='assistant'), batch[1]]):
        with pytest.raises(MessageConflictError):
            mem.remember(changed, 'chat')
    assert len(mem.episodes_doc.values()) == 2
    assert mem.remember(batch, 'chat')['source_ids'] == original['source_ids']


def test_native_close_rejects_raw_acceptance():
    mem = MemoryEngine()
    mem.close()
    with pytest.raises(RuntimeError, match='closed'):
        mem.remember('synthetic source')
    assert not mem.episodes_doc.values()


@pytest.mark.parametrize('kind', ['raw', 'dialogue'])
def test_third_party_name_never_becomes_the_owner_identity(kind):
    def names(prompt, system):
        data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX)[1])[0]
        source_id = data['pending'][0]['id']
        return json.dumps(dict(facts=[dict(categories=[], sensitive=False, time_kind='current', effective_date=None, retention='unknown', subject='friend', predicate='name', object='Alice',
            text='The friend is Alice', source_ids=[source_id]),
            dict(categories=[], sensitive=False, time_kind='current', effective_date=None, retention='unknown', subject='我', predicate='owns', object='bicycle', text='I own a bicycle',
                 source_ids=[source_id])], digests=[]))
    mem = MemoryEngine(llm=FakeLLM(handler=names))
    if kind == 'raw':
        mem.add('Synthetic owner and friend')
        mem.consolidate()
    else:
        assert mem.remember('Synthetic owner and friend', 's', message_id='a')['ok']
    facts = mem.fact_store.values()
    assert next(f for f in facts if f.predicate == 'owns').subject == '我'
    assert next(f for f in facts if f.predicate == 'name').subject == 'friend'
