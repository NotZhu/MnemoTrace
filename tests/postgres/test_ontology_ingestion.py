"""Ontology behavior through the normal PostgreSQL ingestion and read interfaces."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock

import pytest

from mnemotrace.llm import FakeLLM
from mnemotrace.service import MemoryService
from mnemotrace.store.contracts import FactQuery, FactState, MessageInput, RecordKind
from mnemotrace.store.postgres.records import domain
from mnemotrace.types import RelationType
from tests.postgres.ontology_fixtures import Answers, assertion


@pytest.fixture
def make_memory(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Ontology').space_id)

    def make(handler):
        llm = FakeLLM(handler=handler)
        service = MemoryService(runtime, llm=llm)
        return service.memory(context), service

    return make


def ingest(memory, statement, when=100, session='chat'):
    result = memory.remember(statement, session, event_time=when, processing='immediate')
    assert result['ok'], result
    return result['source_ids'][0]


def records(memory, kind):
    with memory.dao.read_snapshot() as view:
        return tuple(map(domain, view.records(kind)))


def test_alias_synonym_reverse_and_dedup_preserve_provenance(make_memory):
    inputs = {
        'Alice Chen (also Ace) works for Orbit Labs.': assertion('Alice Chen', 'works_for', 'Orbit Labs', aliases=['Ace']),
        'Ace is employed by Orbit Labs.': assertion('Ace', 'employed_by', 'Orbit Labs'),
        'Orbit Labs employs Ace.': assertion('Orbit Labs', 'employs', 'Ace',
                                             subject_type='organization', object_type='person'),
    }
    memory, _ = make_memory(Answers(inputs, {'works_for': ('works_at', False), 'employed_by': ('works_at', False),
                                             'employs': ('works_at', True)}))
    sources = [ingest(memory, statement) for statement in inputs]
    fact, = memory.facts()
    assert fact.predicate == 'works_at' and fact.provenance == sources
    assert memory.entity('Alice Chen').id == memory.entity('Ace').id == fact.subject_id
    assert memory.entity('Orbit Labs').id == fact.object_id
    assert memory.graph_relations(fact.id)
    assert len(records(memory, RecordKind.RELATION_TYPE)) == 1


def test_ambiguity_retains_text_and_never_changes_the_timeline(make_memory):
    inputs = {
        'Full-time: user works for Orbit Labs.': assertion('user', 'works_for', 'Orbit Labs'),
        'Full-time: user has an unclear role at Nova Labs.': assertion('user', 'works_at', 'Nova Labs'),
    }
    memory, service = make_memory(Answers(inputs, {'works_for': ('works_at', False), 'works_at': (None, False)}))
    first_id, second_id = [ingest(memory, statement, when) for statement, when in zip(inputs, (10, 20))]
    first = next(f for f in memory.facts() if first_id in f.provenance)
    second = next(f for f in memory.facts() if second_id in f.provenance)
    assert first.invalid_at is None and second.relation_id is None
    assert second.id in {f.id for f in memory.search('unclear role at Nova Labs').facts}
    assert second_id in memory.get_fact(second.id).provenance
    assert memory.aggregate_facts('works_at').count == 1
    assert memory.graph_relations(second.id) == () and memory.conflicts() == ()
    assert service.export(memory.context)['facts']
    assert memory.entity('Nova Labs') is None


def test_symmetric_relation_deduplicates_both_orientations_and_traverses_either_way(make_memory):
    inputs = {
        'Alice is a colleague of Bob.': assertion('Alice', 'colleague', 'Bob', object_type='person'),
        'Bob is a coworker of Alice.': assertion('Bob', 'coworker', 'Alice', object_type='person'),
        'Bob is a colleague of Cara.': assertion('Bob', 'colleague', 'Cara', object_type='person'),
    }
    for claim in inputs.values():
        claim['ontology']['direction'] = 'symmetric'
    memory, _ = make_memory(Answers(inputs, {'colleague': ('colleague', False), 'coworker': ('colleague', False)}))
    sources = [ingest(memory, statement) for statement in inputs]
    facts = memory.facts()
    assert len(facts) == 2
    fact = next(item for item in facts if sources[0] in item.provenance)
    assert fact.provenance == sources[:2]
    assert len(memory.graph_relations(fact.id)) == 1
    assert any(path.endpoint == 'Alice' for path in memory.follow_path('Cara', ['colleague'] * 2).paths)
    assert any(path.endpoint == 'Cara' for path in memory.follow_path('Alice', ['colleague'] * 2).paths)


def test_learned_type_and_relation_are_reused_after_concurrent_registration(make_memory):
    inputs = {
        'user calibrates the Aurora scope.': assertion('user', 'calibrates', 'Aurora scope', object_type='instrument',
                                                     type_meaning='A device used to make a measurement'),
        'user calibrates the Boreal scope.': assertion('user', 'calibrates', 'Boreal scope', object_type='instrument',
                                                     type_meaning='A device used to make a measurement'),
    }
    barrier, lock = Barrier(2), Lock()
    entered = set()

    def pause(data):
        with lock:
            first = data['statement'] not in entered
            entered.add(data['statement'])
        if first:
            barrier.wait(timeout=10)

    memory, _ = make_memory(Answers(inputs, {'calibrates': ('calibrates', False)}, on_relation=pause))
    ops = [memory.remember(statement, str(index), event_time=100 + index,
                          processing='buffered')['operation_ids'][0] for index, statement in enumerate(inputs)]
    for op in ops:
        assert memory._processing.run_one(op, force=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(lambda op: memory._processing.run_one(op, force=True), ops))
    assert memory.flush_processing(retry_failed=True)['ok']
    assert {item.id for item in records(memory, RecordKind.ENTITY_TYPE)} == {'person', 'instrument'}
    relation, = records(memory, RecordKind.RELATION_TYPE)
    assert relation.predicate == 'calibrates' and relation.endpoint_types == [['person', 'instrument']]
    assert {fact.relation_id for fact in memory.facts()} == {relation.id}
    assert len(memory.facts()) == 2


def test_bounded_relation_candidates_do_not_close_the_catalog(make_memory):
    statement = 'user volunteers at Aurora Labs.'
    inputs = {statement: assertion('user', 'volunteers_at', 'Aurora Labs', meaning='Volunteers for an organization')}
    memory, _ = make_memory(Answers(inputs, {'volunteers_at': ('volunteers_at', False)}))
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    definitions = (RelationType(f'other_{index}', 'Unrelated definition', memory.context.user_id,
                                endpoint_types=[['person', 'organization']]) for index in range(25))
    memory._commit(snapshot, writes=tuple(map(memory._write, definitions)))
    ingest(memory, statement)
    fact, = memory.facts()
    assert fact.relation_id
    assert fact.predicate == 'volunteers_at'
    assert len(records(memory, RecordKind.RELATION_TYPE)) == 26


def test_normalized_replacement_rethreads_history_and_unknown_goes_to_review(make_memory):
    inputs = {
        f'Full-time: user works at {company}.': assertion('user', predicate, company,
                                                          time_kind='unknown' if company == 'Delta' else 'current')
        for company, predicate in [('Aster', 'works_for'), ('Birch', 'employed_by'),
                                   ('Cedar', 'holds_job_at'), ('Delta', 'works_at')]
    }
    memory, _ = make_memory(Answers(inputs, {f['predicate']: ('works_at', False) for f in inputs.values()}))
    for statement, when in zip(inputs, (10, 40, 30, None)):
        ingest(memory, statement, when)
    rows = {f.object: f for f in memory.facts(FactQuery(state=FactState.ALL))}
    assert rows['Aster'].invalid_at == 30 and rows['Cedar'].invalid_at == 40
    assert rows['Cedar'].supersedes == [rows['Aster'].id] and rows['Birch'].supersedes == [rows['Cedar'].id]
    assert rows['Birch'].invalid_at is None and 'Delta' not in rows
    assert any(c.reason == 'Fixed synthetic comparison' for c in memory.conflicts())


def test_homonym_and_private_alias_do_not_leak(make_memory):
    inputs = {
        'Alice Secret (also Ace) works at Orbit Labs.': assertion('Alice Secret', 'works_at', 'Orbit Labs',
                                                                 aliases=['Ace'], sensitive=True),
        'Ace consults at Nova Labs.': assertion('Ace', 'works_for', 'Nova Labs'),
        'Ace is a separate person on the Transit team.': assertion('Ace', 'works_for', 'Transit'),
    }

    def distinguish(data):
        if 'separate person' in data['source'] and data['mention'] == 'Ace':
            return dict(decision='new', id=None, evidence='separate person')

    memory, service = make_memory(Answers(inputs, {'works_at': ('works_at', False), 'works_for': ('works_at', False)},
                                           entity_decision=distinguish))
    for when, statement in enumerate(inputs, 10):
        ingest(memory, statement, when)
    facts = {item.object: item for item in memory.facts()}
    assert facts['Orbit Labs'].subject_id == facts['Nova Labs'].subject_id != facts['Transit'].subject_id
    assert memory.entity('Ace') is None
    assert 'Alice Secret' not in json.dumps(service.graph(memory.context))
    assert 'Alice Secret' not in json.dumps(service.export(memory.context))
    assert service.graph(memory.context, q='Alice Secret')['edges'] == []


def test_alias_visibility_and_erasure_follow_its_own_evidence(make_memory):
    inputs = {
        'Alice Chen works at Orbit Labs.': assertion('Alice Chen', 'works_at', 'Orbit Labs'),
        'Alice Chen (Alias Open) works at Orbit Labs.': assertion('Alice Chen', 'works_at', 'Orbit Labs',
                                                                  aliases=['Alias Open']),
        'Alice Chen (Alias Private) works at Orbit Labs.': assertion('Alice Chen', 'works_at', 'Orbit Labs',
                                                                     aliases=['Alias Private'], sensitive=True),
    }
    memory, service = make_memory(Answers(inputs, {'works_at': ('works_at', False)}))
    first, public, private = inputs
    original = ingest(memory, first)
    fact, = memory.facts()
    memory.set_fact_sensitive(fact.id, False)
    alias_source = ingest(memory, public)
    private_source = ingest(memory, private)
    assert memory.get_fact(fact.id).provenance == [original, alias_source, private_source]
    with memory.dao.read_snapshot() as view:
        assert view.named_entities('Alias Open', limit=2, share_safe=True)
        assert view.named_entities('Alias Private', limit=2)
        assert not view.named_entities('Alias Private', limit=2, share_safe=True)
    assert service.graph(memory.context, q='Alias Open')['edges']
    assert not service.graph(memory.context, q='Alias Private')['edges']
    with memory.dao.read_snapshot() as view:
        snapshot, stored = view.snapshot, view.get(RecordKind.FACT, fact.id)
    retained = replace(domain(stored), provenance=[original, private_source])
    memory._commit(snapshot, writes=(memory._write(retained, stored),))
    assert memory.entity('Alias Open') is None
    assert not service.graph(memory.context, q='Alias Open')['edges']
    assert memory.entity('Alias Private') is not None
    assert memory.get_fact(fact.id).provenance == [original, private_source]
    assert memory.delete_fact(fact.id)
    assert records(memory, RecordKind.ENTITY_ALIAS) == ()


def test_invalid_model_selection_writes_no_catalog_or_fact(make_memory):
    statement = 'user calibrates the Aurora scope.'
    inputs = {statement: assertion('user', 'calibrates', 'Aurora scope', object_type='instrument',
                                  type_meaning='A device used to make a measurement')}
    good = Answers(inputs, {})

    def respond(prompt, system):
        if 'MNEMOTRACE_RELATION_RESOLUTION' in system:
            return json.dumps(dict(decision='reuse', id='foreign', reverse=False))
        return good(prompt, system)

    memory, _ = make_memory(respond)
    accepted = memory.remember(statement, 'chat', event_time=100, processing='buffered')
    operation, = accepted['operation_ids']
    assert memory._processing.run_one(operation, force=True)
    assert memory._processing.run_one(operation, force=True)
    assert memory.operation(operation).error_code == 'extraction_invalid_response'
    assert memory.list_sources() and not memory.facts()
    for kind in (RecordKind.ENTITY, RecordKind.ENTITY_TYPE, RecordKind.ENTITY_ALIAS, RecordKind.RELATION_TYPE):
        assert records(memory, kind) == ()


def test_window_calls_and_meter_cover_every_stage(make_memory):
    inputs = {f'{person} works at Orbit in round {round_}.': assertion(person, 'works_at', 'Orbit')
              for round_ in (1, 2) for person in ('Alice', 'Bob', 'Cara', 'Dana')}
    memory, service = make_memory(Answers(inputs, {'works_at': ('works_at', False)}))
    for round_ in (1, 2):
        start = len(service.llm.calls)
        messages = tuple(MessageInput(str(index), 'user', text, event_time=round_ * 100)
                         for index, text in enumerate(inputs) if f'round {round_}.' in text)
        result = memory.remember_messages(f'round{round_}', messages, processing='immediate')
        assert result['ok'], result
        if round_ == 2:
            calls = service.llm.calls[start:]
            assert len(calls) == 4
            assert [call['system'].splitlines()[0] for call in calls[1:]] == [
                'MNEMOTRACE_ENTITY_RESOLUTION', 'MNEMOTRACE_RELATION_RESOLUTION', 'MNEMOTRACE_FACT_COMPARISON']
            assert len(json.loads(calls[1]['prompt'])['items']) == 8
    assert len(memory.facts()) == 8
    metrics = service.metrics.snapshot()
    assert metrics['ops']['pipeline_model_call']['n'] == len(service.llm.calls)
    assert metrics['counts']['pipeline_usage_unreported'] == len(service.llm.calls)


def test_unknown_comparison_remains_private_until_adjudicated(make_memory):
    inputs = {f'user works at {company}.': assertion('user', 'works_at', company) for company in ('Orbit', 'Nova')}
    fixed = Answers(inputs, {'works_at': ('works_at', False)})

    def respond(prompt, system):
        if system.startswith('MNEMOTRACE_FACT_COMPARISON'):
            return json.dumps(dict(decisions=[dict(id=item['id'], decision='unknown', reason='Insufficient context')
                                             for item in json.loads(prompt)['items']]))
        return fixed(prompt, system)

    memory, service = make_memory(respond)
    for when, text in enumerate(inputs, 10):
        ingest(memory, text, when)
    old, = memory.facts()
    assert old.object == 'Orbit' and old.invalid_at is None
    conflict, = memory.conflicts()
    assert conflict.reason == 'Insufficient context'
    assert memory.graph_entities('Nova') == ()
    assert all(fact.object != 'Nova' for fact in memory.search('Nova').facts)
    assert all(fact['object'] != 'Nova' for fact in service.export(memory.context)['facts'])
    assert memory.resolve_conflict(conflict.id, 'both')
    assert {fact.object for fact in memory.facts()} == {'Orbit', 'Nova'}


def test_failed_later_comparison_batch_publishes_no_partial_window(make_memory):
    inputs = {f'user works at Company {index}.': assertion('user', 'works_at', f'Company {index}')
              for index in range(7)}
    fixed = Answers(inputs, {'works_at': ('works_at', False)})
    comparisons, fail = 0, False

    def respond(prompt, system):
        nonlocal comparisons
        if fail and system.startswith('MNEMOTRACE_FACT_COMPARISON'):
            comparisons += 1
            if comparisons == 2:
                return '{"decisions":[]}'
        return fixed(prompt, system)

    memory, service = make_memory(respond)
    messages = tuple(MessageInput(str(index), 'user', text, event_time=100)
                     for index, text in enumerate(inputs))
    assert memory.remember_messages('seed', messages[:3], processing='immediate')['ok']
    kinds = (RecordKind.FACT, RecordKind.ENTITY, RecordKind.ENTITY_TYPE, RecordKind.RELATION_TYPE,
             RecordKind.RELATION, RecordKind.ENTITY_ALIAS, RecordKind.CONFLICT)
    before = {kind: records(memory, kind) for kind in kinds}
    fail = True
    result = memory.remember_messages('new', messages[3:], processing='buffered')
    operation, = result['operation_ids']
    assert memory._processing.run_one(operation, force=True)
    assert memory._processing.run_one(operation, force=True)
    assert comparisons == 2
    assert memory.operation(operation).error_code == 'extraction_invalid_response'
    assert {kind: records(memory, kind) for kind in kinds} == before
    assert service.metrics.snapshot()['ops']['pipeline_model_call']['n'] == len(service.llm.calls)
