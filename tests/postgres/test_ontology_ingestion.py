"""Ontology behavior through the normal PostgreSQL ingestion and read interfaces."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from mnemotrace.config import Config
from mnemotrace.llm import FakeLLM
from mnemotrace.service import MemoryService
from mnemotrace.store.contracts import FactQuery, FactState, MessageInput, RecordKind
from mnemotrace.store.postgres.records import domain
from mnemotrace.types import RelationType
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from tests.postgres.ontology_fixtures import Answers, assertion, ontology_reply, typed_result


@pytest.fixture
def make_memory(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Ontology').space_id)

    def make(handler, *, config=None):
        llm = FakeLLM(handler=handler)
        service = MemoryService(runtime, llm=llm, config=config)
        return service.memory(context), service

    return make


def ingest(memory, statement, when=100, session='chat'):
    result = memory.remember(statement, session, event_time=when, processing='immediate')
    assert result['ok'], result
    return result['source_ids'][0]


def records(memory, kind):
    with memory.dao.read_snapshot() as view:
        return tuple(map(domain, view.records(kind)))


def test_explicit_names_reverse_and_dedup_preserve_provenance(make_memory):
    inputs = {
        'Alice Chen works for Orbit Labs.': assertion('Alice Chen', 'works_for', 'Orbit Labs'),
        'ALICE CHEN is employed by Orbit Labs.': assertion('ALICE CHEN', 'employed_by', 'Orbit Labs'),
        'Orbit Labs employs Alice Chen.': assertion('Orbit Labs', 'employs', 'Alice Chen',
                                                  subject_type='organization', object_type='person'),
    }
    memory, _ = make_memory(Answers(inputs, {'works_for': ('works_at', False), 'employed_by': ('works_at', False),
                                             'employs': ('works_at', True)}))
    sources = [ingest(memory, statement) for statement in inputs]
    fact, = memory.facts()
    assert fact.predicate == 'works_at' and fact.provenance == sources
    assert memory.entity('  ALICE CHEN  ').id == fact.subject_id
    assert memory.entity('Orbit Labs').id == fact.object_id
    assert memory.graph_relations(fact.id)
    assert len(records(memory, RecordKind.RELATION_TYPE)) == 1


def test_self_uses_one_entity_for_both_endpoints_and_exports_original_text(make_memory):
    inputs = {
        'Orbit Labs employs Alice Chen.': assertion('Orbit Labs', 'employs', 'Alice Chen',
                                                     subject_type='organization', object_type='person'),
        'I work for Orbit Labs.': assertion('我', 'works_for', 'Orbit Labs'),
        'I founded Nova Labs.': assertion('我', 'founded', 'Nova Labs'),
    }
    memory, service = make_memory(Answers(inputs, {'works_for': ('employs', True)}))
    for statement in inputs:
        ingest(memory, statement)
    owner = memory.entity('我')
    founded, = memory.facts(FactQuery(subject='我'))
    assert founded.subject_id == owner.id
    employed = next(fact for fact in memory.facts() if fact.object_id == owner.id)
    assert (employed.subject, employed.predicate, employed.object) == ('Orbit Labs', 'employs', '我')
    assert memory.graph_relations(employed.id)[0].object_id == owner.id
    snapshot = service.export(memory.context, include_sensitive=True)
    assert next(row for row in snapshot['facts'] if row['id'] == employed.id)['text'] == 'I work for Orbit Labs.'


def test_ambiguous_assertions_keep_only_the_source(make_memory):
    inputs = {
        'Someone has something to do with Nova Labs.': None,
        'user has a temporary assignment with Nova Labs.': assertion('我', 'unknown_relation', 'Nova Labs'),
    }
    memory, service = make_memory(Answers(inputs, {'unknown_relation': (None, False)}))
    for expected_calls, statement in enumerate(inputs, 1):
        before = len(service.llm.calls)
        source_id = ingest(memory, statement)
        assert memory.get_episode(source_id).content == statement
        assert len(service.llm.calls) - before == expected_calls
    assert not memory.facts()
    for kind in (RecordKind.ENTITY, RecordKind.ENTITY_TYPE, RecordKind.RELATION_TYPE):
        assert not records(memory, kind)


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


@pytest.mark.parametrize('old_ends', [('Alice', 'Bob'), ('Bob', 'Alice')])
@pytest.mark.parametrize('new_ends', [('Bob', 'Cara'), ('Cara', 'Bob')])
def test_symmetric_conflicts_compare_the_shared_endpoint(make_memory, old_ends, new_ends):
    old_text = 'Alice and Bob are exclusive partners.'
    new_text = 'Bob and Cara are exclusive partners.'
    predicate = 'exclusive_partner'
    inputs = {text: assertion(ends[0], predicate, ends[1], object_type='person')
              for text, ends in ((old_text, old_ends), (new_text, new_ends))}
    for item in inputs.values():
        item['ontology']['direction'] = 'symmetric'
    fixed = Answers(inputs, {})
    compared = []

    def respond(prompt, system):
        if system.startswith('MNEMOTRACE_FACT_COMPARISON'):
            data = json.loads(prompt)
            compared.extend({data['facts'][item[side]]['text'] for side in ('first', 'second')}
                            for item in data['items'])
            return json.dumps(dict(decisions=[dict(id=item['id'], decision='conflict') for item in data['items']]))
        return fixed(prompt, system)

    memory, _ = make_memory(respond)
    ingest(memory, old_text, 10)
    old, = memory.facts()
    ingest(memory, new_text, 20)
    current, = memory.facts()
    assert compared == [{old_text, new_text}]
    assert current.text == new_text and current.supersedes == [old.id]
    assert memory.get_fact(old.id).invalid_at == 20


@pytest.mark.parametrize('same_window', [True, False])
def test_symmetric_exact_restatements_merge_evidence_without_model_comparison(make_memory, same_window):
    statement = 'Alice and Bob are colleagues.'
    inputs = {'Alice and Bob are colleagues. First note.': ('Alice', 'Bob'),
              'Bob and Alice are colleagues. Second note.': ('Bob', 'Alice')}

    def respond(prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
            facts = []
            for source in data['pending']:
                subject, object_ = inputs[source['content']]
                item = assertion(subject, 'colleague', object_, object_type='person', sensitive=subject == 'Bob')
                item['ontology']['direction'] = 'symmetric'
                facts.append(dict(item, text=statement, source_ids=[source['id']]))
            return json.dumps(typed_result(dict(facts=facts, digests=[]), data['entity_types']))
        assert not system.startswith('MNEMOTRACE_FACT_COMPARISON')
        return ontology_reply(prompt, system)

    memory, _ = make_memory(respond)
    if same_window:
        messages = tuple(MessageInput(str(i), 'user', text, event_time=10) for i, text in enumerate(inputs))
        result = memory.remember_messages('symmetric', messages, processing='immediate')
        assert result['ok']
        sources = list(result['source_ids'])
    else:
        first, second = inputs
        sources = [ingest(memory, first, 10)]
        old, = memory.facts()
        sources.append(ingest(memory, second, 10))
    fact, = memory.facts()
    assert fact.text == statement and fact.sensitive
    assert set(fact.provenance) == set(sources)
    assert len(memory.graph_relations(fact.id)) == 1
    if not same_window:
        assert fact.id == old.id


def test_learned_type_and_relation_are_reused_after_concurrent_registration(make_memory):
    inputs = {
        'user calibrates the Aurora scope.': assertion('我', 'calibrates', 'Aurora scope', object_type='instrument',
                                                     type_meaning='A device used to make a measurement'),
        'user calibrates the Boreal scope.': assertion('我', 'calibrates', 'Boreal scope', object_type='instrument',
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
    inputs = {statement: assertion('我', 'volunteers_at', 'Aurora Labs', meaning='Volunteers for an organization')}
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


def test_normalized_replacement_rethreads_history_and_discards_unknown_time(make_memory):
    inputs = {
        f'Full-time: user works at {company}.': assertion('我', predicate, company,
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
    assert memory.entity('Delta') is None


def test_entity_type_mismatch_keeps_the_original_entity(make_memory):
    inputs = {
        'Alice Chen works at Orbit Labs.': assertion('Alice Chen', 'works_at', 'Orbit Labs'),
        'Alice Chen has an office in Oslo.': assertion('Alice Chen', 'based_in', 'Oslo',
                                                     subject_type='organization', object_type='location'),
    }
    memory, service = make_memory(Answers(inputs, {}))
    first, second = inputs
    ingest(memory, first)
    entity = memory.entity('Alice Chen')
    before = len(service.llm.calls)
    source = ingest(memory, second, 200)
    assert len(service.llm.calls) == before + 1
    assert memory.get_episode(source).content == second
    assert memory.entity('Alice Chen') == entity
    assert memory.entity('Oslo') is None
    assert len(memory.facts()) == 1


def test_learned_type_is_sent_in_next_extraction(make_memory):
    first, second = ('user calibrates the Aurora scope.', 'user calibrates the Boreal scope.')
    definition = 'A device used to make a measurement'
    fixed = Answers({
        first: assertion('我', 'calibrates', 'Aurora scope', object_type='instrument', type_meaning=definition),
        second: assertion('我', 'calibrates', 'Boreal scope', object_type='instrument'),
    }, {})
    seen = []

    def respond(prompt, system):
        if prompt.startswith('MNEMOTRACE_WINDOW\n'):
            seen.append(json.loads(prompt.split('\n', 1)[1])['entity_types'].get('instrument'))
        return fixed(prompt, system)

    memory, _ = make_memory(respond)
    ingest(memory, first)
    ingest(memory, second, 200)
    assert seen == [None, definition]
    assert {fact.object_type for fact in memory.facts()} == {'instrument'}


def test_stale_type_catalog_regenerates_saved_extraction(make_memory):
    waiting, learning = ('user repairs the Aurora scope.', 'user calibrates the Boreal scope.')
    definition = 'A device used to make a measurement'
    fixed = Answers({
        waiting: assertion('我', 'repairs', 'Aurora scope', object_type='instrument', type_meaning=definition),
        learning: assertion('我', 'calibrates', 'Boreal scope', object_type='instrument', type_meaning=definition),
    }, {})
    memory, service = make_memory(fixed)
    operation, = memory.remember(waiting, 'waiting', event_time=100, processing='buffered')['operation_ids']
    assert memory._processing.run_one(operation, force=True)
    assert memory.operation(operation).extraction_saved
    ingest(memory, learning, 200, session='learning')
    assert memory.flush_processing('waiting')['ok']
    requests = [json.loads(call['prompt'].split('\n', 1)[1]) for call in service.llm.calls
                if call['prompt'].startswith('MNEMOTRACE_WINDOW\n')]
    catalogs = [request['entity_types'] for request in requests if request['pending'][0]['content'] == waiting]
    assert [catalog.get('instrument') for catalog in catalogs] == [None, definition]
    assert {fact.object for fact in memory.facts()} == {'Aurora scope', 'Boreal scope'}


@pytest.mark.parametrize('accept', [True, False])
def test_relation_endpoint_extension_commits_only_for_accepted_facts(make_memory, accept):
    first = 'Full-time: user works at Orbit Labs.'
    second = 'Full-time: user works at Nova Foundation.'
    inputs = {
        first: assertion('我', 'works_at', 'Orbit Labs', meaning='Person works for an organization'),
        second: assertion('我', 'works_at', 'Nova Foundation', object_type='foundation',
                          type_meaning='A nonprofit foundation', meaning='Person works for a foundation',
                          time_kind='current' if accept else 'unknown'),
    }
    fixed = Answers(inputs, {'works_at': ('works_at', False)})
    memory, service = make_memory(fixed)
    ingest(memory, first)
    original, = records(memory, RecordKind.RELATION_TYPE)
    before = len(service.llm.calls)
    ingest(memory, second, 200)
    assert len(service.llm.calls) - before == 3
    relation, = records(memory, RecordKind.RELATION_TYPE)
    assert relation.id == original.id and relation.predicate == original.predicate
    assert (['person', 'foundation'] in relation.endpoint_types) is accept
    assert ('foundation' in {value.id for value in records(memory, RecordKind.ENTITY_TYPE)}) is accept
    assert (memory.entity('Nova Foundation') is not None) is accept
    old, = memory.history('我', 'works_at')[:1]
    assert old.invalid_at == (200 if accept else None)


def test_invalid_model_selection_writes_no_catalog_or_fact(make_memory):
    statement = 'user calibrates the Aurora scope.'
    inputs = {statement: assertion('我', 'calibrates', 'Aurora scope', object_type='instrument',
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
    for kind in (RecordKind.ENTITY, RecordKind.ENTITY_TYPE, RecordKind.RELATION_TYPE):
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
            assert len(calls) == 3
            assert [call['system'].splitlines()[0] for call in calls[1:]] == [
                'MNEMOTRACE_RELATION_RESOLUTION', 'MNEMOTRACE_FACT_COMPARISON']
            assert len(json.loads(calls[1]['prompt'])['items']) == 4
    assert len(memory.facts()) == 8
    metrics = service.metrics.snapshot()
    assert metrics['ops']['pipeline_model_call']['n'] == len(service.llm.calls)
    assert metrics['counts']['pipeline_usage_unreported'] == len(service.llm.calls)


def test_unknown_comparison_discards_only_new_candidate(make_memory):
    inputs = {f'user works at {company}.': assertion('我', 'works_at', company) for company in ('Orbit', 'Nova')}
    fixed = Answers(inputs, {'works_at': ('works_at', False)})

    def respond(prompt, system):
        if system.startswith('MNEMOTRACE_FACT_COMPARISON'):
            return json.dumps(dict(decisions=[dict(id=item['id'], decision='unknown')
                                             for item in json.loads(prompt)['items']]))
        return fixed(prompt, system)

    memory, _ = make_memory(respond)
    for when, text in enumerate(inputs, 10):
        source = ingest(memory, text, when)
    old, = memory.facts()
    assert old.object == 'Orbit' and old.invalid_at is None
    assert memory.entity('Nova') is None
    assert memory.get_episode(source).content == text
    assert len(records(memory, RecordKind.FACT)) == 1


def test_failed_later_comparison_batch_publishes_no_partial_window(make_memory):
    inputs = {f'user works at Company {index}.': assertion('我', 'works_at', f'Company {index}')
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

    config = Config(joint_max_output_tokens=720, joint_core_output_tokens=320, joint_digest_output_tokens=128)
    memory, service = make_memory(respond, config=config)
    messages = tuple(MessageInput(str(index), 'user', text, event_time=100)
                     for index, text in enumerate(inputs))
    assert memory.remember_messages('seed', messages[:3], processing='immediate')['ok']
    kinds = (RecordKind.FACT, RecordKind.ENTITY, RecordKind.ENTITY_TYPE, RecordKind.RELATION_TYPE,
             RecordKind.RELATION)
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
