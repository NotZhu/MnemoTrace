import pytest

from tests.postgres.fact_fixtures import seed_fact

from mnemotrace.llm import FakeLLM
from mnemotrace.memory import Memory
from mnemotrace.store.postgres.records import FACT, domain
from tests.test_fact_adjudication import model_response


@pytest.mark.parametrize('keep,published', [('older', False), ('dismiss', False), ('newer', True), ('both', True)])
def test_equal_time_evidence_is_committed_and_only_published_by_explicit_resolution(runtime, spaces, user, keep, published):
    space = spaces.create(user, 'Protected adjudication')
    context = spaces.authorize(user, space.space_id)
    mem = Memory(runtime, context, llm=FakeLLM(handler=model_response))
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    result = mem.remember('Oslo', 'review', event_time=10)
    assert result['status'] == 'completed'
    conflict, = mem.conflicts()
    assert {conflict.older, conflict.newer} & {manual.id}
    assert [fact.object for fact in mem.facts()] == ['Paris']
    challenger = conflict.newer if conflict.older == manual.id else conflict.older
    assert mem.get_fact(challenger) is None
    assert mem.graph_entities('Oslo') == ()
    with mem.export_records() as rows:
        assert [domain(row).object for row in rows] == ['Paris']
    with mem.export_records(include_sensitive=True) as rows:
        assert any(row.ref.kind == FACT and domain(row).review_pending for row in rows)
    if keep in {'newer', 'older'} and conflict.older != manual.id:
        keep = 'newer' if keep == 'older' else 'older'
    assert mem.resolve_conflict(conflict.id, keep)
    assert ('Oslo' in {fact.object for fact in mem.facts()}) is published
    assert mem.conflicts() == ()


def test_adjudication_reloads_history_after_a_concurrent_write(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication version fence')
    context = spaces.authorize(user, space.space_id)
    other = Memory(runtime, context)
    manual = seed_fact(other, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    verdict_calls = 0

    def respond(prompt, system):
        nonlocal verdict_calls
        if 'pair id' in system:
            verdict_calls += 1
            if verdict_calls == 1:
                other.set_fact_sensitive(manual.id, True)
        return model_response(prompt, system)

    model = FakeLLM(handler=respond)
    mem = Memory(runtime, context, llm=model)
    assert mem.remember('Oslo', 'review', event_time=10)['status'] == 'completed'
    assert verdict_calls == 2 and len(model.calls) == 3
    conflict, = mem.conflicts()
    current_version = conflict.older_version if conflict.older == manual.id else conflict.newer_version
    assert current_version == 2
    assert [fact.object for fact in mem.facts()] == ['Paris']


def test_invalid_adjudication_does_not_publish_any_part_of_window(runtime, spaces, user):
    space = spaces.create(user, 'Adjudication retry')
    context = spaces.authorize(user, space.space_id)

    def respond(prompt, system):
        if 'pair id' in system:
            return '{"decisions":[]}'
        return model_response(prompt, system)

    mem = Memory(runtime, context, llm=FakeLLM(handler=respond))
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False, text='Ana lives in Paris')
    accepted = mem.remember('Oslo', 'review', event_time=10, processing='buffered')
    operation, = accepted['operation_ids']
    assert mem._processing.run_one(operation, force=True)
    assert mem._processing.run_one(operation, force=True)
    status = mem._processing.tasks.operation(operation)
    assert status.state.value == 'retry_wait'
    assert status.extraction_saved and not status.core_committed
    assert [fact.id for fact in mem.facts()] == [manual.id]
    assert mem.conflicts() == ()


def test_duplicate_evidence_keeps_labels_and_adds_provenance(runtime, spaces, user):
    space = spaces.create(user, 'Manual duplicate evidence')
    context = spaces.authorize(user, space.space_id)
    model = FakeLLM(handler=model_response)
    mem = Memory(runtime, context, llm=model)
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, categories=['personal_details'], sensitive=True, text='Ana lives in Paris')
    result = mem.remember('Paris', 'review', event_time=10)
    assert result['status'] == 'completed'
    kept, = mem.facts()
    assert kept.id == manual.id and tuple(kept.provenance) == result['source_ids']
    assert (kept.categories, kept.categories_source, kept.sensitive) == (['personal_details'], 'llm', True)
    assert len(model.calls) == 1 and mem.conflicts() == ()


def test_pending_review_round_trips_through_private_export(runtime, spaces, user):
    from mnemotrace import MemoryEngine
    from mnemotrace.service import MemoryService

    context = spaces.authorize(user, spaces.create(user, 'Pending review').space_id)
    service = MemoryService(runtime, llm=FakeLLM(handler=model_response))
    mem = service.memory(context)
    seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, text='Ana lives in Paris')
    assert mem.remember('Oslo', 'review', event_time=10)['status'] == 'completed'
    assert len(mem.conflicts()) == 1
    assert {f['object'] for f in service.export(context)['facts']} == {'Paris'}
    restored = MemoryEngine()
    restored.import_snapshot(service.export(context, include_sensitive=True))
    imported, = restored.pending_conflicts()
    assert restored.resolve_conflict(imported.id, 'both')
    assert {f.object for f in restored._all_facts()} == {'Paris', 'Oslo'}


def test_private_export_remains_importable_after_erasing_a_conflict_endpoint(runtime, spaces, user):
    from mnemotrace import MemoryEngine
    from mnemotrace.service import MemoryService

    space = spaces.create(user, 'Erased conflict endpoint')
    context = spaces.authorize(user, space.space_id)
    service = MemoryService(runtime, llm=FakeLLM(handler=model_response))
    mem = service.memory(context)
    manual = seed_fact(mem, 'Ana', 'lives_in', 'Paris', valid_at=10, text='Ana lives in Paris')
    assert mem.remember('Oslo', 'review', event_time=10)['status'] == 'completed'
    assert mem.conflicts()
    assert mem.delete_fact(manual.id)
    snapshot = service.export(context, include_sensitive=True)
    assert snapshot['facts'] == snapshot['conflicts'] == []
    assert MemoryEngine().import_snapshot(snapshot)['facts_restored'] == 0
