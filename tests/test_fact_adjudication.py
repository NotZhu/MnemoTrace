import json
from datetime import datetime, timezone

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.adjudication import plan_merge
from mnemotrace.consolidate.planning import WINDOW_PREFIX, fact_valid_at, validate_response
from mnemotrace.embed import HashingEmbedder
from mnemotrace.extraction import ExtractionResponseError
from mnemotrace.llm import FakeLLM
from mnemotrace.types import Fact


def claim(source_id, value='Paris', **fields):
    return dict(subject='Ana', predicate='lives_in', object=value, text=f'Ana lives in {value}',
                assertion_kind='unspecified', source_ids=[source_id], category='位置', sensitive=False,
                time_kind='current', effective_date=None) | fields


def decisions(prompt, decision='replaces'):
    return json.dumps({'decisions': [{'id': pair['id'], 'decision': decision}
                                    for pair in reversed(json.loads(prompt))]})


@pytest.mark.parametrize('fields', [
    {'category': 'undefined'}, {'sensitive': 'false'}, {'effective_date': '2024-01-01'},
    {'time_kind': 'dated'}, {'time_kind': 'dated', 'effective_date': '2024-02-30'},
    {'time_kind': 'dated', 'effective_date': '20240101'}, {'time_kind': 'inferred'},
])
def test_invalid_fact_metadata_is_rejected(fields):
    data = dict(operation='extract', source_kind='raw', pending=[dict(id='source', needs_summary=False)], context=[])
    raw = json.dumps(dict(facts=[claim('source', **fields)], working=[], summaries=[]))
    with pytest.raises(ExtractionResponseError):
        validate_response(raw, data, Config())


def test_dated_and_unknown_facts_use_their_own_time_basis():
    expected = datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()
    assert fact_valid_at(claim('source', time_kind='dated', effective_date='2024-02-29'), 100) == expected
    assert fact_valid_at(claim('source', time_kind='unknown'), 100) is None
    assert fact_valid_at(claim('source'), None) is None


def fact(key, obj, at, **fields):
    return Fact('Ana', 'lives_in', obj, id=key, valid_at=at, created_at=100,
                provenance=[key + '-source'], **fields)


def merge(incoming, previous=(), *, judge='replaces', now=100, model=None):
    model = model or FakeLLM(handler=lambda prompt, system: decisions(prompt, judge))
    changed, conflicts, stats = plan_merge(incoming, previous, model, Config(), HashingEmbedder(), now)
    return {item.id: item for item in changed}, conflicts, stats


def test_exact_duplicates_merge_provenance_and_keep_sensitive():
    first = fact('first', 'Paris', 1)
    duplicate = fact('duplicate', 'PARIS', 1, sensitive=True)
    changed, conflicts, stats = merge([duplicate], [first])
    assert not conflicts and stats['model_calls'] == 0
    assert changed['first'].provenance == ['first-source', 'duplicate-source']
    assert changed['first'].sensitive
    assert not first.sensitive


def test_late_a_b_a_reconnects_chain_independent_of_model_order():
    first, late, last = fact('first', 'Paris', 10), fact('late', 'Oslo', 20), fact('last', 'Paris', 30)
    first.invalid_at, first.expired_at, last.supersedes = 30, 80, first.id
    changed, conflicts, stats = merge([late], [first, last])
    assert not conflicts and stats['model_calls'] == 1
    assert changed['first'].invalid_at == 20
    assert (changed['late'].supersedes, changed['late'].invalid_at, changed['late'].expired_at) == ('first', 30, 100)
    assert changed['last'].supersedes == 'late'


def test_future_transition_preserves_old_value_until_effective_date():
    first, future = fact('first', 'Paris', 10), fact('future', 'Oslo', 200)
    changed, _, _ = merge([future], [first])
    assert changed['first'].is_live(199)
    assert not changed['future'].is_live(199)
    assert not changed['first'].is_live(200)
    assert changed['future'].is_live(200)


def test_shared_source_and_assertion_kind_prevent_retirement():
    first = fact('first', 'Paris', 10, assertion_kind='user_statement')
    shared = fact('shared', 'Oslo', 20, assertion_kind='user_statement')
    shared.provenance = first.provenance
    other = fact('other', 'Berlin', 30, assertion_kind='assistant_suggestion')
    changed, conflicts, stats = merge([shared, other], [first])
    assert not conflicts and stats['model_calls'] == 1
    assert set(changed) == {'shared', 'other'}
    assert changed['shared'].invalid_at is None


def test_missing_model_verdict_fails_before_changing_evidence():
    first = fact('first', 'Paris', 10)
    with pytest.raises(ExtractionResponseError):
        merge([fact('newer', 'Oslo', 20)], [first], model=FakeLLM(responses=['{"decisions":[]}']))
    assert first.invalid_at is None


def model_response(prompt, system):
    if prompt.startswith(WINDOW_PREFIX):
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        origin = data['pending'][0]
        return json.dumps(dict(facts=[claim(origin['id'], origin['content'])], working=[], summaries=[]))
    return decisions(prompt, 'conflict')


def test_offline_manual_conflict_is_hidden_until_approval():
    mem = MemoryEngine(llm=FakeLLM(handler=model_response))
    manual = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10, sensitive=False)
    mem.add('Oslo', event_time=20, consolidate=True)
    conflict, = mem.pending_conflicts()
    pending = mem.fact_store.get(conflict.newer)
    assert pending.review_pending
    assert [item.id for item in mem._all_facts()] == [manual.id]
    assert mem.resolve_conflict(conflict.id, 'both')
    assert {item.object for item in mem._all_facts()} == {'Paris', 'Oslo'}
    assert not pending.is_live(10)


def test_manual_sensitive_override_survives_duplicate_evidence():
    old = fact('first', 'Paris', 1, sensitive_override=True)
    changed, _, _ = merge([fact('duplicate', 'Paris', 1, sensitive=True)], [old])
    assert not changed['first'].sensitive
    assert changed['first'].provenance == ['first-source', 'duplicate-source']


def test_semantic_recall_crosses_categories_with_a_real_vector_signal():
    previous = fact('first', 'Paris', 1, category='位置', embedding=[1.0, 0.0])
    incoming = fact('second', 'Oslo', 2, category='事件', embedding=[1.0, 0.0])
    incoming.predicate = 'based_in'
    model = FakeLLM(handler=lambda prompt, system: decisions(prompt))
    changed, conflicts, stats = plan_merge([incoming], [previous], model, Config(), object(), 100)
    assert not conflicts and stats['model_calls'] == 1
    assert next(item for item in changed if item.id == previous.id).invalid_at == 2


def test_complete_adjudication_across_batch_boundaries():
    previous = [fact(str(index), f'city {index}', 1) for index in range(17)]
    incoming = fact('incoming', 'Oslo', 2)
    changed, conflicts, stats = merge([incoming], previous, judge='coexists')
    assert not conflicts and stats['model_calls'] == 2
    assert list(changed) == ['incoming']


def test_partial_semantic_workset_cannot_drop_a_confirmed_predecessor():
    previous = fact('previous', 'Paris', 20, embedding=[0.0, 1.0])
    latest = fact('latest', 'Oslo', 30, supersedes=previous.id, embedding=[1.0, 0.0])
    previous.predicate, latest.predicate = 'based_in', 'home_base'
    previous.invalid_at = latest.valid_at
    incoming = fact('incoming', 'Berlin', 10, embedding=[1.0, 0.0])
    model = FakeLLM(handler=lambda prompt, system: decisions(prompt))
    changed, conflicts, stats = plan_merge([incoming], [previous, latest], model, Config(), object(), 100)
    assert not conflicts and stats['model_calls'] == 1
    assert len(changed) == 1 and changed[0].id == incoming.id
    assert latest.supersedes == previous.id


def test_manual_duplicate_links_support_without_changing_user_labels():
    old = fact('manual', 'Paris', 1, source='user', category='Custom category')
    changed, conflicts, stats = merge([fact('duplicate', 'Paris', 1, sensitive=True)], [old])
    assert not conflicts and stats['model_calls'] == 0
    kept = changed[old.id]
    assert kept.provenance == ['manual-source', 'duplicate-source']
    assert (kept.source, kept.category, kept.sensitive) == ('user', 'Custom category', False)


def test_hashing_recall_includes_explicit_preference_reversal():
    old, new = fact('old', 'jazz', 10), fact('new', 'jazz', 20)
    old.predicate, new.predicate = 'likes', 'dislikes'
    changed, _, stats = merge([new], [old])
    assert stats['model_calls'] == 1
    assert changed[old.id].invalid_at == 20


def test_two_coexisting_predecessors_expire_at_the_adjudicated_successor():
    first, second, latest = fact('a', 'Alpha', 10), fact('b', 'Beta', 20), fact('c', 'Gamma', 30)
    changed, _, _ = merge([latest], [first, second])
    assert changed[first.id].invalid_at == changed[second.id].invalid_at == 30
    assert changed[second.id].supersedes is None


def test_long_history_candidate_fits_the_prompt_budget():
    config = Config()
    previous = fact('long', '会议' * 5000, 10)
    incoming = fact('short', 'Oslo', 20)

    def respond(prompt, system):
        assert len((system + prompt).encode()) <= config.extraction_max_input_bytes
        return decisions(prompt)

    changed, conflicts, stats = merge([incoming], [previous], model=FakeLLM(handler=respond))
    assert stats['model_calls'] == 1
    assert {item.id for item in changed.values()} == {incoming.id}
    assert len(conflicts) == 1


def test_approved_future_challenger_keeps_the_old_fact_live_until_effective():
    from mnemotrace.consolidate.adjudication import resolve_facts

    old, future = fact('old', 'Paris', 10, source='user'), fact('future', 'Oslo', 200, review_pending=True)
    resolve_facts({'older': old, 'newer': future}, 'newer', 100)
    assert old.is_live(199)
    assert not old.is_live(200) and future.is_live(200)


def test_historical_manual_conflict_is_reviewable_and_revises_only_its_interval():
    def respond(prompt, system):
        if prompt.startswith(WINDOW_PREFIX):
            return model_response(prompt, system)
        return json.dumps({'decisions': [
            dict(id=pair['id'], decision='conflict' if pair['first']['object'] == 'Paris' else 'coexists')
            for pair in json.loads(prompt)]})

    mem = MemoryEngine(llm=FakeLLM(handler=respond))
    first = mem.add_fact('Ana', 'lives_in', 'Paris', valid_at=10)
    last = mem.add_fact('Ana', 'lives_in', 'Berlin', valid_at=30)
    mem.add('Oslo', event_time=20, consolidate=True)
    conflict, = mem.pending_conflicts()
    assert mem.resolve_conflict(conflict.id, 'newer')
    middle = mem.fact_store.get(conflict.newer)
    assert (first.invalid_at, middle.valid_at, middle.invalid_at) == (20, 20, 30)
    assert (middle.supersedes, last.supersedes) == (first.id, middle.id)
    assert [f.object for f in mem._all_facts() if f.is_live()] == ['Berlin']


def test_snapshot_restores_pending_conflict_with_remapped_fact_ids():
    from dataclasses import asdict
    from mnemotrace.types import Conflict

    old, held = fact('old', 'Paris', 10, source='user'), fact('held', 'Oslo', 20, review_pending=True)
    conflict = Conflict(old.id, held.id, old.text, held.text)
    payload = dict(facts=[asdict(old), asdict(held)], episodes=[], conflicts=[asdict(conflict)])
    restored = MemoryEngine()
    restored.import_snapshot(payload, user_id='target')
    restored.import_snapshot(payload, user_id='target')
    pending, = restored.pending_conflicts('target')
    assert pending.pair_key != conflict.pair_key
    assert restored.resolve_conflict(pending.id, 'both')
    restored.import_snapshot(payload, user_id='target')
    assert restored.pending_conflicts('target') == []
    assert {f.object for f in restored._all_facts()} == {'Paris', 'Oslo'}


def test_snapshot_rejects_an_unlinked_held_fact_before_restoring():
    from dataclasses import asdict

    held = fact('held', 'Oslo', 20, review_pending=True)
    restored = MemoryEngine()
    with pytest.raises(ValueError, match='pending fact has no conflict'):
        restored.import_snapshot(dict(facts=[asdict(held)], episodes=[]))
    assert restored._all_facts(include_unpublished=True) == []
