import pytest

from mnemotrace.llm import FakeLLM
from mnemotrace.memory import Memory
from mnemotrace.store.contracts import FactQuery
from mnemotrace.store.postgres.errors import VersionConflict
from mnemotrace.store.postgres.records import FACT, domain
from tests.test_fact_adjudication import FUTURE, model_response, weekday_response


@pytest.mark.parametrize('keep,published', [('older', False), ('dismiss', False), ('newer', True), ('both', True)])
def test_protected_evidence_is_committed_and_only_published_by_explicit_resolution(runtime, spaces, user, keep, published):
    space = spaces.create(user, 'Protected adjudication')
    context = spaces.authorize(user, space.space_id)
    mem = Memory(runtime, context, llm=FakeLLM(handler=model_response))
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    result = mem.remember('Oslo', 'review', event_time=20)
    assert result['status'] == 'completed'
    conflict, = mem.conflicts()
    assert conflict.older == manual.id
    assert [fact.object for fact in mem.facts()] == ['Paris']
    assert mem.get_fact(conflict.newer) is None
    assert mem.graph_entities('Oslo') == ()
    with mem.export_records() as rows:
        assert [domain(row).object for row in rows] == ['Paris']
    with mem.export_records(include_sensitive=True) as rows:
        assert any(row.ref.kind == FACT and domain(row).review_pending for row in rows)
    assert mem.resolve_conflict(conflict.id, keep)
    assert ('Oslo' in {fact.object for fact in mem.facts()}) is published
    assert mem.conflicts() == ()


def test_adjudication_reloads_history_after_a_concurrent_write(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication version fence')
    context = spaces.authorize(user, space.space_id)
    other = Memory(runtime, context)
    manual = other.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    verdict_calls = 0

    def respond(prompt, system):
        nonlocal verdict_calls
        if 'pair id' in system:
            verdict_calls += 1
            if verdict_calls == 1:
                other.update_fact(manual.id, object='Berlin', sensitive=False)
        return model_response(prompt, system)

    model = FakeLLM(handler=respond)
    mem = Memory(runtime, context, llm=model)
    assert mem.remember('Oslo', 'review', event_time=20)['status'] == 'completed'
    assert verdict_calls == 2 and len(model.calls) == 3
    conflict, = mem.conflicts()
    assert conflict.text_older.endswith('Berlin')
    assert [fact.object for fact in mem.facts()] == ['Berlin']


def test_invalid_adjudication_does_not_publish_any_part_of_window(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication retry')
    context = spaces.authorize(user, space.space_id)

    def respond(prompt, system):
        if 'pair id' in system:
            return '{"decisions":[]}'
        return model_response(prompt, system)

    mem = Memory(runtime, context, llm=FakeLLM(handler=respond))
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    accepted = mem.remember('Oslo', 'review', event_time=20, processing='buffered')
    operation, = accepted['operation_ids']
    assert mem._processing.run_one(operation, force=True)
    assert mem._processing.run_one(operation, force=True)
    status = mem._processing.tasks.operation(operation)
    assert status.state.value == 'retry_wait'
    assert status.extraction_saved and not status.core_committed
    assert [fact.id for fact in mem.facts()] == [manual.id]
    assert mem.conflicts() == ()


def test_duplicate_manual_fact_gets_provenance_without_model_overrides(runtime, spaces, user):
    space = spaces.create(user, 'Manual duplicate evidence')
    context = spaces.authorize(user, space.space_id)
    model = FakeLLM(handler=model_response)
    mem = Memory(runtime, context, llm=model)
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, category='User category', sensitive=True)
    result = mem.remember('Paris', 'review', event_time=10)
    assert result['status'] == 'completed'
    kept, = mem.facts()
    assert kept.id == manual.id and tuple(kept.provenance) == result['source_ids']
    assert (kept.category, kept.sensitive) == ('User category', True)
    assert len(model.calls) == 1 and mem.conflicts() == ()


def test_historical_review_round_trips_through_private_export(runtime, spaces, user):
    import json

    from mnemotrace import MemoryEngine
    from mnemotrace.consolidate.planning import WINDOW_PREFIX
    from mnemotrace.service import MemoryService

    def respond(prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            return model_response(prompt, system)
        return json.dumps({'decisions': [
            dict(id=pair['id'], decision='conflict' if pair['first']['object'] == 'Paris' else 'coexists')
            for pair in json.loads(prompt)]})

    space = spaces.create(user, 'Historical review')
    context = spaces.authorize(user, space.space_id)
    service = MemoryService(runtime, llm=FakeLLM(handler=respond))
    mem = service.memory(context)
    first = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    last = mem.add_fact('Ana', 'lives_in', 'Berlin', valid_at=30, sensitive=False)
    assert mem.remember('Oslo', 'review', event_time=20)['status'] == 'completed'
    conflict, = mem.conflicts()
    assert conflict.older == first.id
    assert 'Oslo' not in json.dumps(service.export(context))
    snapshot = service.export(context, include_sensitive=True)
    restored = MemoryEngine()
    restored.import_snapshot(snapshot)
    imported, = restored.pending_conflicts()
    assert restored.resolve_conflict(imported.id, 'newer')
    assert [f.object for f in restored._all_facts() if f.is_live()] == ['Berlin']
    assert mem.resolve_conflict(conflict.id, 'newer')
    history = {f.object: f for f in mem.history('Ana', 'lives_in')}
    assert (history['Paris'].invalid_at, history['Oslo'].invalid_at) == (20, 30)
    assert (history['Oslo'].supersedes, history['Berlin'].supersedes) == (first.id, conflict.newer)
    assert [f.id for f in mem.facts()] == [last.id]


def test_private_export_remains_importable_after_erasing_a_conflict_endpoint(runtime, spaces, user):
    from mnemotrace import MemoryEngine
    from mnemotrace.service import MemoryService

    space = spaces.create(user, 'Erased conflict endpoint')
    context = spaces.authorize(user, space.space_id)
    service = MemoryService(runtime, llm=FakeLLM(handler=model_response))
    mem = service.memory(context)
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10)
    assert mem.remember('Oslo', 'review', event_time=20)['status'] == 'completed'
    assert mem.conflicts()
    assert mem.delete_fact(manual.id)
    snapshot = service.export(context, include_sensitive=True)
    assert snapshot['facts'] == snapshot['conflicts'] == []
    assert MemoryEngine().import_snapshot(snapshot)['facts_restored'] == 0


def test_approving_a_future_fact_preserves_current_value_until_effective_date(runtime, spaces, user):
    space = spaces.create(user, 'Future review')
    context = spaces.authorize(user, space.space_id)
    mem = Memory(runtime, context, llm=FakeLLM(handler=model_response))
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10)
    effective_at = 4_102_444_800
    assert mem.remember('Oslo', 'review', event_time=effective_at)['status'] == 'completed'
    conflict, = mem.conflicts()
    assert mem.resolve_conflict(conflict.id, 'newer')
    assert [f.id for f in mem.facts()] == [manual.id]
    assert [f.id for f in mem.facts(FactQuery(valid_at=effective_at))] == [conflict.newer]


def test_future_manual_blocker_can_be_reviewed_before_it_is_effective(runtime, spaces, user):
    space = spaces.create(user, 'Scheduled manual review')
    mem = Memory(runtime, spaces.authorize(user, space.space_id), llm=FakeLLM(handler=model_response))
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=FUTURE)
    assert mem.remember('Oslo', 'review', event_time=FUTURE + 10)['status'] == 'completed'
    conflict, = mem.conflicts()
    assert conflict.older == manual.id and mem.get_fact(conflict.newer) is None
    assert mem.resolve_conflict(conflict.id, 'newer')
    assert [f.id for f in mem.facts(FactQuery(valid_at=FUTURE + 9))] == [manual.id]
    assert [f.id for f in mem.facts(FactQuery(valid_at=FUTURE + 10))] == [conflict.newer]


def test_manual_add_links_scheduled_predecessor_and_successor(runtime, spaces, user):
    space = spaces.create(user, 'Scheduled manual values')
    mem = Memory(runtime, spaces.authorize(user, space.space_id))
    first = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10)
    last = mem.add_fact('Ana', 'lives_in', 'Oslo', valid_at=FUTURE + 20)
    middle = mem.add_fact('Ana', 'lives_in', 'Berlin', valid_at=FUTURE + 10)
    following = mem.add_fact('Ana', 'lives_in', 'Rome', valid_at=FUTURE + 30)
    records = [mem.get_fact(fact.id) for fact in (first, middle, last, following)]
    assert [(f.invalid_at, f.supersedes) for f in records] == [
        (FUTURE + 10, None), (FUTURE + 20, first.id), (FUTURE + 30, middle.id), (None, last.id),
    ]
    assert [f.object for f in mem.facts(FactQuery(valid_at=FUTURE + 20))] == ['Oslo']


@pytest.mark.parametrize('keep', ['newer', 'both'])
def test_approval_respects_other_manual_conflict_decisions(runtime, spaces, user, keep):
    space = spaces.create(user, 'Multiple manual blockers')
    mem = Memory(runtime, spaces.authorize(user, space.space_id), llm=FakeLLM(handler=weekday_response))
    tuesday = mem.add_fact('Ana', 'works_on_days', 'Tuesday', valid_at=10)
    thursday = mem.add_fact('Ana', 'works_on_days', 'Thursday', valid_at=10)
    assert mem.remember('Wednesday only', 'review', event_time=20)['status'] == 'completed'
    conflicts = {c.older: c for c in mem.conflicts()}
    with pytest.raises(VersionConflict, match='Other conflicts'):
        mem.resolve_conflict(conflicts[tuesday.id].id, keep)
    assert mem.resolve_conflict(conflicts[thursday.id].id, 'older')
    with pytest.raises(VersionConflict, match='Other conflicts'):
        mem.resolve_conflict(conflicts[tuesday.id].id, keep)
    assert {f.id for f in mem.facts()} == {tuesday.id, thursday.id}
    assert mem.get_fact(conflicts[tuesday.id].newer) is None
