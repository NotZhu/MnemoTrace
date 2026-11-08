"""Public source processing invariants, using synthetic responses and a controlled worker."""
import json
from copy import deepcopy

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.pipeline import PipelineTasks, validate_state
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM

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


def test_explicit_summary_preserves_unrequested_extraction_and_is_idempotent():
    model = FakeLLM(handler=respond)
    mem = MemoryEngine(llm=model, config=Config(summary_max_input_bytes=6500))
    ep = mem.add(LONG * 2)
    assert mem.summarize_episodes([ep]) == 1
    assert not ep.consolidated and not mem.fact_store.values()
    assert ep.summary == SUMMARY and ep.summary_coverage['policy'] == 'summary_source_v1' and ep.coverage is None
    count = len(model.calls)
    assert mem.summarize_episodes([ep]) == 0 and len(model.calls) == count
    assert PipelineTasks(mem, 'default').snapshot()['jobs'][0]['core'] == 'not_requested'


@pytest.mark.parametrize('mutation', ['position', 'role', 'time', 'length', 'part_text', 'part_range', 'receipt_type', 'summary_receipt'])
def test_restore_binds_coverage_and_summary_parts_to_the_committed_source(mutation):
    mem = MemoryEngine(llm=FakeLLM(handler=respond))
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


def test_adjudication_is_ledgered_before_core_commit():
    from tests.test_fact_adjudication import model_response

    llm = FakeLLM(handler=model_response)
    mem = MemoryEngine(llm=llm)
    mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    mem.add('Oslo', event_time=20, consolidate=True)
    win, = PipelineTasks(mem, 'default').state['windows'].values()
    assert len(llm.calls) == win['stats']['model_calls'] == 2
    assert [entry['stage'] for entry in win['history'] if not entry['stage'].startswith('reset')] == [
        'generate', 'embed', 'adjudicate']
    assert win['state'] == 'completed' and win['core_saved']
    validate_state(mem._pipeline_state, mem.episodes_doc.values(), mem.embedder.dim)


def test_explicit_dialogue_summary_has_its_own_cropped_proof():
    from mnemotrace.retrieve.events import event_evidence
    mem = MemoryEngine(llm=FakeLLM(handler=respond), config=Config(summary_max_input_bytes=6500))
    source = mem.remember_messages([dict(message_id='a', role='user', content='长原文' * 4000)], 's', scope='raw')
    ep = mem.episodes_doc.get(source['messages'][0]['episode_id'])
    assert mem.summarize_episodes([ep]) == 1
    assert ep.coverage is None and ep.summary_coverage['partial']
    evidence = event_evidence(ep)
    assert evidence.partial and evidence.derived and evidence.text == SUMMARY
    assert sum(b-a for a, b in evidence.coverage['ranges']) < len(ep.content)


def test_numeric_timestamp_representation_does_not_change_message_identity():
    mem = MemoryEngine()
    msg = dict(message_id='a', role='user', content='Synthetic source', event_time=100)
    original = mem.remember_messages([msg], 's')
    repeated = mem.remember_messages([{**msg, 'event_time': 100.0}], 's')
    assert repeated['accepted'] == 0
    assert repeated['messages'] == original['messages']


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
        nature = 'unspecified' if kind == 'raw' else 'user_statement'
        source_id = data['pending'][0]['id']
        return json.dumps(dict(facts=[dict(category='其他', sensitive=False, time_kind='current', effective_date=None, subject='friend', predicate='name', object='Alice',
            text='The friend is Alice', assertion_kind=nature, source_ids=[source_id]),
            dict(category='其他', sensitive=False, time_kind='current', effective_date=None, subject='user', predicate='owns', object='bicycle', text='I own a bicycle',
                 assertion_kind=nature, source_ids=[source_id])], working=[], summaries=[]))
    mem = MemoryEngine(llm=FakeLLM(handler=names))
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
    mem = MemoryEngine(llm=FakeLLM(handler=respond), config=Config(extraction_max_input_bytes=6500))
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
