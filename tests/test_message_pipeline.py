"""Synthetic end-to-end dialogue acceptance, checkpoints and bounded model calls."""
import json


from mnemotrace import MemoryEngine
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM


def message(mid, content='Synthetic item', role='user', event_time=100):
    return dict(message_id=mid, content=content, role=role, event_time=event_time)


def reply(prompt, system):
    if 'pair id' in system:
        from tests.model_fixtures import adjudication_reply
        return adjudication_reply(prompt)
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
    if 'rules' not in data:
        return json.dumps(dict(facts=[], working=[]))
    result = dict(facts=[], working=[], summaries=[dict(source_id=s['id'], text='Synthetic summary')
        for s in data['pending'] if s['id'] in data['rules']['summary_required']])
    if '本次仅补' in system:
        return json.dumps({'summaries': result['summaries']})
    for item in data['pending']:
        nature = 'user_statement' if item['role'] == 'user' else 'assistant_suggestion' if item['role'] == 'assistant' else 'unspecified'
        result['facts'].append(dict(category='其他', sensitive=False, time_kind='current', effective_date=None, subject='user', predicate='owns', object=item['content'][:30],
            text=item['content'][:30], assertion_kind=nature, source_ids=[item['id']]))
    return json.dumps(result)


def test_standalone_python_uses_same_joint_processor():
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', user_id='u')
    result = mem.flush_messages('u', 's')
    assert result['ok'] and len(mem.fact_store.values()) == 1
