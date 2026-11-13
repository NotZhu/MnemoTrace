"""Exercise the new extraction protocol through the PostgreSQL product entry point."""
import json

from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM
from mnemotrace.memory import Memory


def test_resolves_relation_before_creating_fact(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'New extraction').space_id)
    statement = 'Alice Chen joined Orbit Labs.'

    def reply(prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            data = json.loads(prompt[len(WINDOW_PREFIX):])
            assert 'organization' in data['entity_types']
            return json.dumps(dict(facts=[dict(
                subject='Alice Chen', object='Orbit Labs', text=statement,
                source_ids=[data['pending'][0]['id']], categories=[], sensitive=False,
                time_kind='current', effective_date=None, retention='normal',
                ontology=dict(subject=dict(type='person', type_meaning=None),
                              object=dict(type='organization', type_meaning=None),
                              meaning='The person is employed by the organization', direction='forward'),
            )], digests=[]))
        assert system.startswith('MNEMOTRACE_RELATION_RESOLUTION')
        data = json.loads(prompt)
        item, = data['items']
        assert 'predicate' not in item
        target = next(key for key in item['candidates'] if data['candidates'][key]['predicate'] == 'works_at')
        return json.dumps(dict(decisions=[dict(id=item['id'], decision='reuse', target=target,
                                              reverse=False, predicate=None)]))

    model = FakeLLM(handler=reply)
    memory = Memory(runtime, context, llm=model)
    result = memory.remember(statement, 'input', event_time=10)
    assert result['ok'], result
    fact, = memory.facts()
    assert (fact.subject, fact.predicate, fact.object, fact.valid_at) == ('Alice Chen', 'works_at', 'Orbit Labs', 10)
    assert tuple(fact.provenance) == result['source_ids']
    assert memory.entity('  alice CHEN ').id == fact.subject_id
    assert memory.graph_relations(fact.id)
    assert len(model.calls) == 2
