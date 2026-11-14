"""Phase-one contracts exercised through the real scoped DAO and read paths."""
from dataclasses import replace

import pytest

from mnemotrace import MemoryEngine
from mnemotrace.config import Config
from mnemotrace.consolidate.adjudication import plan_merge
from mnemotrace.consolidate.postgres_merge import build_record_write
from mnemotrace.memory import Memory
from mnemotrace.service import MemoryService
from mnemotrace.store.contracts import FactQuery, FactState, MessageInput, RecordKind
from mnemotrace.store.postgres.errors import DatabaseConstraintError, VersionConflict
from mnemotrace.store.postgres.records import domain
from mnemotrace.types import Entity, Fact, RelationType
from tests.postgres.fact_fixtures import relation_writes


@pytest.fixture
def memory(runtime, spaces, user):
    return Memory(runtime, spaces.authorize(user, spaces.create(user, 'Lifecycle').space_id))


def prepare(memory, *facts, contradicts=()):
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
        previous = view.merge_history(facts)
        at = view.at.timestamp()
        relations = relation_writes(view, facts)
    verdicts = {tuple(sorted((left.id, right.id))): 'conflict' for left, right in contradicts}
    originals = {record.ref.record_id: record for record in previous}
    changed, _ = plan_merge(facts, list(map(domain, previous)), None, Config(), None, at, verdicts=verdicts)
    accepted_relations = {fact.relation_id for fact in changed}
    writes = [write for write in relations if write.ref.record_id in accepted_relations]
    writes.extend(build_record_write(memory.context, fact, originals.get(fact.id)) for fact in changed)
    return snapshot, tuple(writes)


def ingest(memory, *facts, contradicts=()):
    snapshot, writes = prepare(memory, *facts, contradicts=contradicts)
    return memory._commit(snapshot, writes=writes)


def setup_graph(memory, names=('First', 'Middle', 'Last')):
    owner = memory.context.user_id
    person = Entity('Synthetic Person', 'person', owner)
    cities = [Entity(name, 'location', owner) for name in names]
    relation = RelationType('lives_in', 'Primary place of residence', owner,
                            endpoint_types=[['person', 'location']])
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    memory._commit(snapshot, writes=tuple(memory._write(item) for item in (relation, person, *cities)))
    return person, cities, relation


def claim(memory, obj, at, *, subject=None, relation=None, **fields):
    subject_name = subject.name if subject else 'Synthetic Person'
    object_name = obj.name if isinstance(obj, Entity) else obj
    return Fact(subject_name, 'lives_in', object_name, f'{subject_name} lives in {object_name}.',
                user_id=memory.context.user_id, valid_at=at,
                subject_id=subject.id if subject else None,
                object_id=obj.id if isinstance(obj, Entity) else None,
                relation_id=relation.id if relation else '', **fields)


def source(memory, message_id, *, at=None):
    return memory.remember_messages('evidence', (MessageInput(message_id, 'user', 'Synthetic evidence',
                                                            event_time=at, extract=False),))['source_ids'][0]


def test_late_fact_rethreads_timeline_graph_and_provenance(memory):
    person, (first, middle, last), relation = setup_graph(memory)
    jan = claim(memory, first, 10, subject=person, relation=relation)
    apr = claim(memory, last, 40, subject=person, relation=relation)
    mar = claim(memory, middle, 30, subject=person, relation=relation,
                provenance=[source(memory, 'late-evidence', at=80)])
    ingest(memory, jan)
    ingest(memory, apr, contradicts=[(jan, apr)])
    first_notice = memory.get_fact(jan.id).expired_at
    ingest(memory, mar, contradicts=[(jan, mar), (mar, apr)])

    history = memory.history(person.name, relation.predicate)
    assert [(f.id, f.invalid_at, f.supersedes) for f in history] == [
        (jan.id, 30, []), (mar.id, 40, [jan.id]), (apr.id, None, [mar.id]),
    ]
    assert first_notice is not None and history[0].expired_at == first_notice
    assert (history[1].subject_type, history[1].object_type) == ('person', 'location')
    for at, fact in [(29, jan), (30, mar), (39, mar), (40, apr)]:
        query = FactQuery(valid_at=at)
        assert [item.id for item in memory.facts(query)] == [fact.id]
        edge, = memory.graph_neighbors(person.id, facts=query)
        assert edge.object_id == fact.object_id
    hit, = memory.facts(FactQuery(valid_at=35))
    assert memory.get_episode(hit.provenance[0]).event_time == 80
    result = memory.search("Where does Synthetic Person live?",
                           facts=FactQuery(valid_at=35, subject=person.name, predicate=relation.predicate))
    assert result.source_ids == tuple(hit.provenance)
    with memory.dao.read_snapshot() as view:
        assert {row.ref.record_id for row in view.fact_chain([apr.id], FactQuery(state=FactState.ALL))} == {
            jan.id, mar.id, apr.id,
        }


def test_future_disjoint_unknown_and_duplicate(memory, runtime):
    current = claim(memory, 'Current', 10)
    future = claim(memory, 'Future', 4_000_000_000)
    past = claim(memory, 'Earlier', 1, invalid_at=5)
    ingest(memory, current)
    ingest(memory, future, contradicts=[(current, future)])
    ingest(memory, past)
    assert [item.id for item in memory.facts()] == [current.id]
    assert memory.get_fact(current.id).expired_at is not None
    assert memory.get_fact(past.id).invalid_at == 5
    assert memory.get_fact(current.id).supersedes == []
    service = MemoryService(runtime)
    counts = service.stats(memory.context)['counts']
    assert (counts['facts_live'], counts['facts_superseded']) == (1, 1)
    snapshot = service.export(memory.context, include_sensitive=True)
    assert {row['id']: row['status'] for row in snapshot['facts']} == {
        current.id: 'live', past.id: 'superseded', future.id: 'scheduled',
    }
    assert MemoryEngine().import_snapshot(snapshot)['facts_restored'] == 3

    duplicate = replace(current, id='duplicate', provenance=[source(memory, 'corroboration')])
    ingest(memory, duplicate)
    assert memory.get_fact(current.id).provenance == duplicate.provenance
    unknown = claim(memory, 'Unknown', None)
    ingest(memory, unknown)
    assert unknown.id in {item.id for item in memory.facts()}
    assert unknown.id not in {item.id for item in memory.facts(FactQuery(valid_at=50))}


def test_scope_constraints_and_atomic_projection(memory, runtime, spaces):
    uid = memory.context.user_id
    elsewhere = Memory(runtime, spaces.authorize(uid, spaces.create(uid, 'Elsewhere').space_id))
    foreign_person, _, _ = setup_graph(elsewhere)
    foreign = claim(elsewhere, 'Foreign', 10)
    ingest(elsewhere, foreign)
    foreign_source = source(elsewhere, 'foreign')
    person, (first, middle, _), relation = setup_graph(memory)
    left = claim(memory, first, 10, subject=person, relation=relation)
    ingest(memory, left)
    for candidate in (
        claim(memory, middle, 20, subject=foreign_person, relation=relation),
        claim(memory, middle, 20, subject=person, relation=relation, provenance=[foreign_source]),
        claim(memory, middle, 20, subject=person, relation=relation, supersedes=[foreign.id]),
    ):
        with pytest.raises(DatabaseConstraintError):
            ingest(memory, candidate, contradicts=[(left, candidate)] if candidate.subject_id == person.id else [])
        assert memory.get_fact(left.id).invalid_at is None
        assert memory.graph_relations(left.id)[0].invalid_at is None
        assert memory.get_fact(candidate.id) is None


def test_sensitivity_versions_and_erasure(memory):
    left = claim(memory, 'Left', 10, provenance=[source(memory, 'left')])
    right = claim(memory, 'Right', 20, provenance=[source(memory, 'right')])
    ingest(memory, left)
    snapshot, writes = prepare(memory, right, contradicts=[(left, right)])
    memory.set_fact_sensitive(left.id, True)
    assert not memory.facts(FactQuery(exclude_sensitive=True))
    with pytest.raises(VersionConflict):
        memory._commit(snapshot, writes=writes)
    assert memory.get_fact(left.id).invalid_at is None and memory.get_fact(right.id) is None
    ingest(memory, right, contradicts=[(left, right)])
    assert memory.facts(FactQuery(exclude_sensitive=True))[0].supersedes == []
    assert memory.get_fact(right.id).supersedes == [left.id]
    memory.lean_context('Synthetic Person', redact_sensitive=True, reinforce_hits=True)
    assert memory.get_fact(right.id).supersedes == [left.id]
    assert memory.delete_fact(left.id)
    assert memory.get_fact(right.id).supersedes == []
    with memory.dao.read_snapshot() as view:
        assert view.get(RecordKind.FACT, left.id) is None
        assert domain(view.get(RecordKind.FACT, right.id)).provenance == right.provenance


def test_fact_write_requires_registered_relation(memory):
    candidate = claim(memory, 'City', 10)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    with pytest.raises(DatabaseConstraintError):
        memory._commit(snapshot, writes=(memory._write(candidate),))
    assert memory.get_fact(candidate.id) is None


def test_entity_names_are_unique_after_normalization(memory):
    owner = memory.context.user_id
    first = Entity('Synthetic Twin', 'person', owner)
    second = Entity('  synthetic TWIN  ', 'person', owner)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    with pytest.raises(DatabaseConstraintError):
        memory._commit(snapshot, writes=tuple(memory._write(item) for item in (first, second)))
    with memory.dao.read_snapshot() as view:
        assert view.get(RecordKind.ENTITY, first.id) is None
        assert view.get(RecordKind.ENTITY, second.id) is None


def test_endpoint_signature_filters_projection_without_discarding_fact(memory):
    from mnemotrace.audit import graph_audit

    person, _, relation = setup_graph(memory)
    linked = claim(memory, person, 10, subject=person, relation=relation)
    ingest(memory, linked)
    assert memory.get_fact(linked.id).object_type == 'person'
    assert memory.graph_relations(linked.id) == ()
    with memory.dao.read_snapshot() as view:
        assert graph_audit(view, 10)['by_kind'].get('missing_edge', 0) == 0


def test_http_sensitivity_is_only_fact_edit(product_client, product_scope, product_service):
    context, _ = product_scope('lifecycle-http')
    memory = product_service.memory(context)
    fact = claim(memory, 'Synthetic City', 10)
    ingest(memory, fact)
    headers = {'X-Synthetic-Scope': 'lifecycle-http'}
    endpoint = '/v1/facts/' + fact.id
    assert product_client.post('/v1/facts', headers=headers, json={'text': 'A new fact'}).status_code == 404
    assert product_client.patch(endpoint, headers=headers, json={'text': 'Another fact'}).status_code == 405
    assert product_client.patch(endpoint + '/sensitivity', headers=headers,
                                json={'sensitive': 'false'}).status_code == 422
    response = product_client.patch(endpoint + '/sensitivity', headers=headers, json={'sensitive': True})
    assert response.status_code == 200
    assert response.json()['sensitive'] is True
    assert memory.get_fact(fact.id).text == fact.text
    assert product_client.get('/v1/memories', headers=headers).json()['facts'] == []
    assert product_client.get('/v1/memories?include_sensitive=true', headers=headers).json()['facts'][0]['id'] == fact.id
    assert product_client.post('/v1/recall', headers=headers,
                               json={'query': 'Synthetic City', 'known_at': 10}).status_code == 422
