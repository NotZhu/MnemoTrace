import json

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM
from tests.test_message_pipeline import message


def record_requests():
    calls = []

    def respond(prompt, system):
        assert prompt.startswith(WINDOW_PREFIX)
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        calls.append(data)
        return json.dumps(dict(facts=[], digests=[
            dict(source_id=source_id, text='Synthetic digest')
            for source_id in data['rules']['digest_required']
        ]))

    return MemoryEngine(config=Config(memory_max_input_tokens=1), llm=FakeLLM(handler=respond)), calls


def test_uses_ten_original_messages_in_chronological_order():
    memory, calls = record_requests()
    previous = [message(str(i), f'Synthetic message {i}', role='assistant') for i in range(12)]
    previous[-2]['content'] = '长消息' * 1000
    memory.remember(previous, 's')
    memory.remember('Synthetic input', 'other')
    memory.remember([message('next')], 's')
    assert [item['message_id'] for item in calls[-1]['context']] == [str(i) for i in range(2, 12)]
    entry = calls[-1]['context'][-2]
    assert entry['content'] == previous[-2]['content'] and entry['representation'] == 'raw'


def test_joint_request_has_fixed_members_and_full_text():
    memory, calls = record_requests()
    messages = [message(str(i), str(i) * 2500) for i in range(3)]
    result = memory.remember(messages, 's')
    assert result['digests'] == 3 and len(calls) == 1
    memory.llm = None
    assert memory.remember(messages, 's') == result
    assert [item['content'] for item in calls[0]['pending']] == [item['content'] for item in messages]
    for key in result['source_ids']:
        original = memory.episodes_doc.get(key)
        assert original.coverage == original.digest_coverage
        assert original.coverage['ranges'] == [[0, 2500]]
        assert not original.coverage['partial']
