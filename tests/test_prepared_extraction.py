"""Detached extraction executes without accessing or mutating live storage."""
import json

from mnemotrace import Memory
from mnemotrace.llm import FakeLLM
from mnemotrace.consolidate.windows import WINDOW_PREFIX


def reply(prompt, system):
    data = json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]
    return json.dumps({'facts': [{'subject': 'user', 'predicate': 'owns', 'object': s['content'],
                                 'source_ids': [s['id']]} for s in data['pending']], 'working': []})


def test_execution_is_detached_and_apply_uses_current_records():
    mem = Memory(llm=FakeLLM(handler=reply))
    ep = mem.add('synthetic bicycle')
    prepared = mem.engine.prepare([ep])
    assert prepared.windows[0].pending[0].episode is not ep
    outputs = prepared.execute()
    assert not mem.fact_store.values() and not ep.consolidated
    ep.metadata['custom'] = 'intervening edit'
    result = mem.engine.apply(prepared.windows[0], outputs[0])
    assert result['facts_added'] == 1
    assert ep.consolidated and ep.metadata['custom'] == 'intervening edit'


def test_source_change_rejects_stale_output():
    mem = Memory(llm=FakeLLM(handler=reply))
    ep = mem.add('synthetic bicycle')
    prepared = mem.engine.prepare([ep])
    ep.content = 'modified source'
    outputs = prepared.execute()
    assert outputs[0].items['facts'][0]['object'] == 'synthetic bicycle'
    assert mem.engine.rebind(prepared.windows[0]) is None
    assert not mem.fact_store.values()
