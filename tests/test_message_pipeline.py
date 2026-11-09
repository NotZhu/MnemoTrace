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
        return json.dumps(dict(facts=[], ))
    result = dict(facts=[], digests=[dict(source_id=s['id'], text='Synthetic digest')
        for s in data['pending'] if s['id'] in data['rules']['digest_required']])
    if '本次仅补' in system:
        return json.dumps({'digests': result['digests']})
    for item in data['pending']:
        if item['role'] == 'assistant':
            continue
        result['facts'].append(dict(category='其他', sensitive=False, time_kind='current', effective_date=None, cardinality='unknown', retention='unknown', subject='user', predicate='owns', object=item['content'][:30],
            text=item['content'][:30], source_ids=[item['id']]))
    return json.dumps(result)


def test_standalone_python_uses_same_joint_processor():
    mem = MemoryEngine(llm=FakeLLM(handler=reply))
    mem.remember_messages([message('a')], 's', user_id='u')
    result = mem.flush_messages('u', 's')
    assert result['ok'] and len(mem.fact_store.values()) == 1


PROPOSAL = 'I suggest switching your default package manager to pnpm.'
REPORT = 'I already completed your release checklist.'


def dialogue_evidence(data):
    """Recorded responses for the role and provenance contract."""
    facts = []
    for item in data['pending']:
        content = item['content']
        if content in {'My default package manager is npm.', 'Yes, use that by default.'}:
            refs = [item['id']]
            value = 'npm'
            if content.startswith('Yes'):
                proposal = next(source for source in (*data['context'], *data['pending'])
                                if source['content'] == PROPOSAL)
                refs.insert(0, proposal['id'])
                value = 'pnpm'
            facts.append(dict(subject='user', predicate='prefers', object=value,
                              text=f'I prefer {value} as my default package manager.',
                              category='偏好', sensitive=False, time_kind='current', effective_date=None, cardinality='unknown', retention='unknown',
                              source_ids=refs))
    return dict(facts=facts, digests=[])


def test_assistant_evidence_is_retrievable_and_confirmation_has_user_time():
    def respond(prompt, system):
        if 'pair id' in system:
            return json.dumps(dict(decisions=[dict(id=pair['id'], decision='replaces')
                                              for pair in json.loads(prompt)]))
        return json.dumps(dialogue_evidence(json.loads(prompt.removeprefix(WINDOW_PREFIX))))

    mem = MemoryEngine(llm=FakeLLM(handler=respond))

    def send(item):
        mem.remember_messages([item], 's')
        assert mem.flush_messages(session_id='s')['ok']

    send(message('task', 'Prepare my release checklist.'))
    send(message('before', 'My default package manager is npm.'))
    send(message('proposal', PROPOSAL, 'assistant', 200))
    send(message('report', REPORT, 'assistant', 250))
    proposal = next(ep for ep in mem.retrieve_episodes(PROPOSAL) if ep.text == PROPOSAL)
    assert proposal.role == 'assistant'
    assert [f.object for f in mem._all_facts() if f.is_live()] == ['npm']
    assert 'pnpm' not in json.dumps(mem.structured_profile())

    send(message('confirmation', 'Yes, use that by default.', event_time=300))
    old = next(f for f in mem._all_facts() if f.object == 'npm')
    new, = [f for f in mem._all_facts() if f.is_live()]
    assert (new.object, new.valid_at, new.supersedes, old.invalid_at) == ('pnpm', 300, old.id, 300)
    assert new.provenance[0] == proposal.id
    assert mem.get_episode(new.provenance[1])['content'] == 'Yes, use that by default.'
    assert new.source_roles == ['assistant', 'user']
