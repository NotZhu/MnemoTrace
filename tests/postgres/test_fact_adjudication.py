import json

from tests.postgres.fact_fixtures import seed_fact as store_fact

from mnemotrace.llm import FakeLLM
from mnemotrace.memory import Memory
from mnemotrace.store.contracts import FactQuery, RetrievalQuery
from mnemotrace.store.postgres.graph_reads import RetrievalGraph
from mnemotrace.store.postgres.records import FACT, domain
from mnemotrace.consolidate.ontology import builtin
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.types import Entity
from tests.postgres.ontology_fixtures import assertion, ontology_reply, typed_result
from tests.test_fact_adjudication import decisions


def seed_fact(memory, subject, predicate, object_, **fields):
    owner = memory.context.user_id
    person = Entity(subject, 'person', owner)
    place = Entity(object_, 'location', owner)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    memory._commit(snapshot, writes=tuple(memory._write(item) for item in (person, place, builtin(owner, predicate))))
    return store_fact(memory, subject, predicate, object_, subject_id=person.id, object_id=place.id, **fields)


def model_response(prompt, system):
    if prompt.startswith(WINDOW_PREFIX):
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        origin, = data['pending']
        value = origin['content'].split()[-1]
        item = assertion('Ana', 'lives_in', value, object_type='location')
        return json.dumps(typed_result(dict(facts=[dict(item, text=origin['content'], source_ids=[origin['id']])], digests=[])))
    if system.startswith('MNEMOTRACE_RELATION_RESOLUTION'):
        return ontology_reply(prompt, system, {'lives_in': ('lives_in', False)})
    return decisions(prompt)


def test_equal_time_candidate_keeps_only_original_evidence(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Equal-time disagreement').space_id)
    mem = Memory(runtime, context, llm=FakeLLM(handler=model_response))
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    result = mem.remember('Ana lives in Oslo', 'input', event_time=10)
    assert result['status'] == 'completed'
    assert [fact.id for fact in mem.facts()] == [manual.id]
    assert mem.graph_entities('Oslo') == ()
    assert mem.get_episode(result['source_ids'][0]).content == 'Ana lives in Oslo'
    with mem.export_records(include_sensitive=True) as rows:
        assert [domain(row).id for row in rows if row.ref.kind == FACT] == [manual.id]


def test_adjudication_reloads_history_after_a_concurrent_write(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication version fence')
    context = spaces.authorize(user, space.space_id)
    other = Memory(runtime, context)
    manual = seed_fact(other, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    verdict_calls = 0

    def respond(prompt, system):
        nonlocal verdict_calls
        if 'MNEMOTRACE_FACT_COMPARISON' in system:
            verdict_calls += 1
            if verdict_calls == 1:
                other.set_fact_sensitive(manual.id, True)
        return model_response(prompt, system)

    model = FakeLLM(handler=respond)
    mem = Memory(runtime, context, llm=model)
    assert mem.remember('Ana lives in Oslo', 'review', event_time=10)['status'] == 'completed'
    assert verdict_calls == 2 and len(model.calls) == 5
    assert mem.get_fact(manual.id).sensitive
    assert [fact.object for fact in mem.facts()] == ['Paris']


def test_invalid_adjudication_does_not_publish_any_part_of_window(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication retry')
    context = spaces.authorize(user, space.space_id)

    def respond(prompt, system):
        if 'MNEMOTRACE_FACT_COMPARISON' in system:
            return '{"decisions":[]}'
        return model_response(prompt, system)

    mem = Memory(runtime, context, llm=FakeLLM(handler=respond))
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    accepted = mem.remember('Ana lives in Oslo', 'review', event_time=10, processing='buffered')
    operation, = accepted['operation_ids']
    assert mem._processing.run_one(operation, force=True)
    assert mem._processing.run_one(operation, force=True)
    status = mem._processing.tasks.operation(operation)
    assert status.state.value == 'retry_wait'
    assert status.extraction_saved and not status.core_committed
    assert [fact.id for fact in mem.facts()] == [manual.id]


def test_duplicate_evidence_keeps_labels_and_adds_provenance(runtime, spaces, user):
    space = spaces.create(user, 'Manual duplicate evidence')
    context = spaces.authorize(user, space.space_id)
    def sensitive_response(prompt, system):
        result = model_response(prompt, system)
        if not prompt.startswith(WINDOW_PREFIX):
            return result
        data = json.loads(result)
        data['facts'][0]['sensitive'] = True
        return json.dumps(data)

    model = FakeLLM(handler=sensitive_response)
    mem = Memory(runtime, context, llm=model)
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, categories=['personal_details'], sensitive=True, text='Ana lives in Paris')
    assert not mem.set_fact_sensitive(manual.id, False).sensitive
    result = mem.remember('Ana lives in Paris', 'review', event_time=10)
    assert result['status'] == 'completed'
    kept, = mem.facts()
    assert kept.id == manual.id and tuple(kept.provenance) == result['source_ids']
    assert (kept.categories, kept.sensitive) == (['personal_details'], True)
    with mem.dao.read_snapshot() as view:
        shared = RetrievalGraph(view, RetrievalQuery(FactQuery(exclude_sensitive=True), ''))
        assert shared.get_entity(mem.context.user_id, 'Ana') is None
    assert len(model.calls) == 2


def test_discarded_candidate_is_absent_from_private_export(runtime, spaces, user):
    from mnemotrace import MemoryEngine
    from mnemotrace.service import MemoryService

    context = spaces.authorize(user, spaces.create(user, 'Discarded candidate').space_id)
    service = MemoryService(runtime, llm=FakeLLM(handler=model_response))
    mem = service.memory(context)
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, text='Ana lives in Paris')
    assert mem.remember('Ana lives in Oslo', 'input', event_time=10)['status'] == 'completed'
    snapshot = service.export(context, include_sensitive=True)
    assert snapshot['format_version'] == 6
    assert 'conflicts' not in snapshot
    assert [row['id'] for row in snapshot['facts']] == [manual.id]
    assert MemoryEngine().import_snapshot(snapshot)['facts_restored'] == 1
