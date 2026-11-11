"""Dialogue planning uses continuous, committed representations and explicit output reserves."""
from mnemotrace.connectors.dialogue import import_messages
import json

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.planning import context_for, input_size, output_size, plan
from mnemotrace.consolidate.pipeline import PipelineTasks
from mnemotrace.llm import FakeLLM
from tests.test_message_pipeline import message, reply


def prepared(rows):
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages(rows, 's', user_id='u')
    assert mem.flush_messages('u')['ok']
    mem.remember_messages([message('next')], 's', user_id='u')
    tasks = PipelineTasks(mem, 'u')
    first = next(r for r in tasks.rows().values() if r['message_id'] == 'next')
    return mem, tasks, first


def test_context_stops_at_over_budget_instead_of_skipping():
    mem, tasks, first = prepared([message('old', 'a'), message('big', 'b' * 100), message('last', 'c')])
    mem.config.extraction_context_bytes = 2
    assert [s['message_id'] for s in context_for(mem, tasks, first)] == ['last']


@pytest.mark.parametrize('roles,expected', [
    (['assistant', 'user', 'assistant'], ['1', '2']),
    (['assistant', 'assistant'], ['0', '1']),
])
def test_user_start_preference_and_all_assistant_retention(roles, expected):
    mem, tasks, first = prepared([message(str(i), role=role) for i, role in enumerate(roles)])
    assert [s['message_id'] for s in context_for(mem, tasks, first)] == expected


def test_failed_core_gap_is_not_context():
    mem, tasks, first = prepared([message('0'), message('1'), message('2')])
    next(r for r in tasks.rows().values() if r['message_id'] == '1')['core'] = 'failed'
    assert [s['message_id'] for s in context_for(mem, tasks, first)] == ['2']


def test_digest_replaces_long_raw_as_context():
    mem, tasks, first = prepared([message('long', 'x' * 2500)])
    supplied = context_for(mem, tasks, first)
    assert supplied[0]['representation'] == 'digest'
    assert supplied[0]['content'] == 'Synthetic digest'
    assert supplied[0]['source_length'] == 2500 and supplied[0]['ranges'] == [[0, 2500]]


def test_output_budget_limits_number_of_long_messages():
    mem = MemoryEngine(config=Config(), llm=FakeLLM(handler=reply))
    mem.remember_messages([message(str(i), str(i) * 2500) for i in range(3)], 's', user_id='u')
    tasks = PipelineTasks(mem, 'u')
    session = next(iter(tasks.state['sessions'].values()))
    data = plan(mem, tasks, session, 3)
    assert len(data['pending']) == 2
    assert output_size(data, mem.config) <= mem.config.joint_max_output_tokens
    assert input_size(data, mem.config) <= mem.config.extraction_max_input_bytes


def test_input_budget_too_small_is_explicit_configuration_error():
    mem = MemoryEngine(config=Config(extraction_max_input_bytes=1), llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', user_id='u')
    tasks = PipelineTasks(mem, 'u')
    with pytest.raises(ValueError, match='Input budget'):
        plan(mem, tasks, next(iter(tasks.state['sessions'].values())), 1)


def test_crop_reserves_actual_policy_and_compensation_protocol():
    from mnemotrace.consolidate.planning import request, system_prompt
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.policy['extract_instruction'] = 'Synthetic policy ' * 100
    mem.remember_messages([message('a', '长消息' * 10000)], 's', 'u')
    tasks = PipelineTasks(mem, 'u')
    data = plan(mem, tasks, next(iter(tasks.state['sessions'].values())), 1)
    assert data['pending'][0]['partial']
    policy = mem.policy.get('extract_system', '') + mem.policy.get('extract_instruction', '')
    actual = system_prompt(policy, compensation=True) + request(data, mem.config)
    assert len(actual.encode('utf-8')) == input_size(data, mem.config, policy)
    assert len(actual.encode('utf-8')) <= mem.config.extraction_max_input_bytes


@pytest.mark.history_import
def test_import_has_individual_roles_and_unknown_times():
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    source = [dict(session_id='s', messages=[message('u', role='user', event_time=None),
                                            message('a', role='assistant', event_time=None)])]
    result = import_messages(mem, source)
    assert result['episodes'] == 2
    assert [e.role for e in mem.episodes_doc.values()] == ['user', 'assistant']
    assert all(e.event_time is None for e in mem.episodes_doc.values())
    assert import_messages(mem, json.loads(json.dumps(source)))['skipped'] == 2
