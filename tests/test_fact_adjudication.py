import json
from datetime import datetime, timezone
from dataclasses import replace

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.consolidate.adjudication import plan_merge
from mnemotrace.consolidate.planning import WINDOW_PREFIX, fact_valid_at, validate_response
from tests.embedder_fixtures import MockEmbedder
from mnemotrace.extraction import ExtractionResponseError
from mnemotrace.llm import FakeLLM
from mnemotrace.store.contracts import FactQuery
from mnemotrace.types import Fact


def claim(source_id, value='Paris', **fields):
    return dict(subject='Ana', predicate='lives_in', object=value, text=f'Ana lives in {value}',
                source_ids=[source_id], categories=[], sensitive=False,
                time_kind='current', effective_date=None, retention='unknown') | fields


def decisions(prompt, decision='conflict'):
    return json.dumps({'decisions': [{'id': pair['id'], 'decision': decision}
                                    for pair in reversed(json.loads(prompt)['items'])]})


@pytest.mark.parametrize('fields', [
    {'category': 'undefined'}, {'sensitive': 'false'}, {'effective_date': '2024-01-01'},
    {'cardinality': 'scalar'}, {'retention': None},
    {'time_kind': 'dated'}, {'time_kind': 'dated', 'effective_date': '2024-02-30'},
    {'time_kind': 'dated', 'effective_date': '20240101'}, {'time_kind': 'inferred'},
])
def test_invalid_fact_metadata_is_rejected(fields):
    data = dict(operation='extract', source_kind='note', pending=[dict(id='source', needs_digest=False)], context=[])
    raw = json.dumps(dict(facts=[claim('source', **fields)], digests=[]))
    with pytest.raises(ExtractionResponseError):
        validate_response(raw, data, Config())


def test_dated_and_unknown_facts_use_their_own_time_basis():
    expected = datetime(2024, 2, 29, tzinfo=timezone.utc).timestamp()
    assert fact_valid_at(claim('source', time_kind='dated', effective_date='2024-02-29'), 100) == expected
    assert fact_valid_at(claim('source', time_kind='unknown'), 100) is None
    assert fact_valid_at(claim('source'), None) is None


def fact(key, obj, at, **fields):
    return Fact('Ana', 'lives_in', obj, text=fields.pop('text', f'Ana lives in {obj}'), id=key, valid_at=at, created_at=100,
                provenance=[key + '-source'], **fields)


def merge(incoming, previous=(), *, judge='conflict', now=100, model=None, config=None):
    model = model or FakeLLM(handler=lambda prompt, system: decisions(prompt, judge))
    changed, stats = plan_merge(incoming, previous, model, config or Config(), MockEmbedder(), now)
    return {item.id: item for item in changed}, stats


def test_exact_duplicates_merge_provenance_and_keep_sensitive():
    first = fact('first', 'Paris', 1)
    duplicate = fact('duplicate', 'Paris', 1, sensitive=True)
    changed, stats = merge([duplicate], [first])
    assert stats['model_calls'] == 0
    assert changed['first'].provenance == ['first-source', 'duplicate-source']
    assert changed['first'].sensitive
    assert not first.sensitive


def test_late_a_b_a_reconnects_chain_independent_of_model_order():
    first, late, last = fact('first', 'Paris', 10), fact('late', 'Oslo', 20), fact('last', 'Paris', 30)
    first.invalid_at, first.expired_at, last.supersedes = 30, 80, [first.id]
    changed, stats = merge([late], [first, last])
    assert stats['model_calls'] == 1
    assert changed['first'].invalid_at == 20
    assert (changed['late'].supersedes, changed['late'].invalid_at, changed['late'].expired_at) == (['first'], 30, 100)
    assert changed['last'].supersedes == ['late']


def test_future_transition_preserves_old_value_until_effective_date():
    first, future = fact('first', 'Paris', 10), fact('future', 'Oslo', 200)
    changed, _ = merge([future], [first])
    assert changed['first'].is_live(199)
    assert not changed['future'].is_live(199)
    assert not changed['first'].is_live(200)
    assert changed['future'].is_live(200)


@pytest.mark.parametrize('semantic', [False, True])
def test_disjoint_history_is_not_sent_for_conflict_comparison(semantic):
    old = [fact(f'old-{index}', f'Old city {index}', index, invalid_at=index + 1) for index in range(1000)]
    current = fact('new', 'Current city', 1000)
    model = FakeLLM(handler=lambda prompt, system: pytest.fail('Disjoint history reached the model'))
    changed, stats = plan_merge([current], old, model, Config(), None, 1000, semantic=semantic)
    assert [item.id for item in changed] == [current.id]
    assert stats['model_calls'] == 0


def test_explicit_dates_from_one_source_can_replace_an_earlier_value():
    first = fact('first', 'Paris', 10)
    second = fact('second', 'Oslo', 20)
    second.provenance = first.provenance
    changed, stats = merge([second], [first])
    assert stats['model_calls'] == 1
    assert changed[first.id].invalid_at == 20
    assert changed[second.id].supersedes == [first.id]


def test_missing_model_verdict_fails_before_changing_evidence():
    first = fact('first', 'Paris', 10)
    with pytest.raises(ExtractionResponseError):
        merge([fact('newer', 'Oslo', 20)], [first], model=FakeLLM(responses=['{"decisions":[]}']))
    assert first.invalid_at is None


def model_response(prompt, system):
    if prompt.startswith(WINDOW_PREFIX):
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        origin = data['pending'][0]
        return json.dumps(dict(facts=[claim(origin['id'], origin['content'])], digests=[]))
    return decisions(prompt, 'conflict')


def test_offline_equal_starts_discard_candidate_and_preserve_source():
    mem = MemoryEngine(llm=FakeLLM(handler=model_response))
    mem.add('Paris', event_time=10, consolidate=True)
    mem.add('Oslo', event_time=10, consolidate=True)
    assert [item.object for item in mem._all_facts()] == ['Paris']
    assert {source.content for source in mem.episodes_doc.values()} == {'Paris', 'Oslo'}


def test_add_fact_without_effective_time_reuses_durable_extracted_assertion():
    def response(prompt, system):
        source, = json.loads(prompt.removeprefix(WINDOW_PREFIX))['pending']
        return json.dumps(dict(facts=[claim(source['id'], 'Paris', retention='durable', categories=['travel'])],
                               digests=[]))

    mem = MemoryEngine(llm=FakeLLM(handler=response))
    episode = mem.add('Ana lives in Paris', event_time=10, consolidate=True)
    original, = mem.history('Ana', 'lives_in')
    duplicate = mem.add_fact('Ana', 'lives_in', 'Paris', text=original.text, categories=['misc'], sensitive=True)
    kept, = mem.history('Ana', 'lives_in')
    assert duplicate.id == kept.id == original.id
    assert (kept.valid_at, kept.retention, kept.categories) == (10, 'durable', ['travel'])
    assert kept.provenance == [episode.id] and kept.source == 'extracted' and kept.sensitive

    later = mem.add_fact('Ana', 'lives_in', 'Paris', text=original.text, valid_at=20)
    assert [(fact.id, fact.valid_at) for fact in mem.history('Ana', 'lives_in')] == [(original.id, 10), (later.id, 20)]


def test_sensitive_duplicate_reapplies_redaction_after_explicit_unmask():
    def sensitive_model(prompt, system):
        result = json.loads(model_response(prompt, system))
        result['facts'][0]['sensitive'] = True
        return json.dumps(result)

    mem = MemoryEngine(llm=FakeLLM(handler=sensitive_model))
    first = mem.add('Paris', event_time=10, consolidate=True)
    old, = mem.history('Ana', 'lives_in')
    assert not mem.set_fact_sensitive(old.id, False).sensitive
    second = mem.add('Paris', event_time=10, consolidate=True)
    kept, = mem.history('Ana', 'lives_in')
    assert kept.id == old.id and kept.sensitive
    assert kept.provenance == [first.id, second.id]


def test_late_evidence_and_sensitive_predecessor_respect_query_and_erase():
    def response(prompt, system):
        if not prompt.startswith(WINDOW_PREFIX):
            return decisions(prompt)
        origin, = json.loads(prompt.removeprefix(WINDOW_PREFIX))['pending']
        fields = (dict(sensitive=True) if origin['content'] == 'Paris' else
                  dict(time_kind='dated', effective_date='2025-02-01'))
        return json.dumps(dict(facts=[claim(origin['id'], origin['content'], **fields)], digests=[]))

    mem = MemoryEngine(llm=FakeLLM(handler=response))
    mem.add('Paris', event_time=10, consolidate=True)
    source = mem.add('Oslo', event_time=datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp(), consolidate=True)
    old, replacement = mem.history('Ana', 'lives_in')
    assert replacement.supersedes == [old.id]
    scope = FactQuery(subject='Ana', predicate='lives_in', valid_at=replacement.valid_at)
    result = mem.search('Where does Ana live?', facts=scope)
    assert result.source_ids == (source.id,)
    shared = mem.search('Where does Ana live?', facts=replace(scope, exclude_sensitive=True))
    assert shared.top().supersedes == []
    assert replacement.supersedes == [old.id]
    assert mem.delete_fact(old.id)
    kept, = mem.history('Ana', 'lives_in')
    assert kept.supersedes == [] and kept.provenance == [source.id]


def test_semantic_recall_crosses_categories_with_a_real_vector_signal():
    previous = fact('first', 'Paris', 1, categories=['travel'], embedding=[1.0, 0.0])
    incoming = fact('second', 'Oslo', 2, categories=['milestones'], embedding=[1.0, 0.0])
    incoming = replace(incoming, predicate='based_in', relation_id='')
    model = FakeLLM(handler=lambda prompt, system: decisions(prompt))
    changed, stats = plan_merge([incoming], [previous], model, Config(), object(), 100)
    assert stats['model_calls'] == 1
    assert next(item for item in changed if item.id == previous.id).invalid_at == 2


def test_complete_adjudication_across_batch_boundaries():
    previous = [fact(str(index), f'city {index}', 1) for index in range(17)]
    incoming = fact('incoming', 'Oslo', 2)
    changed, stats = merge([incoming], previous, judge='coexists', config=Config(joint_max_output_tokens=440))
    assert stats['model_calls'] == 2
    assert list(changed) == ['incoming']


def test_partial_semantic_workset_cannot_drop_a_confirmed_predecessor():
    previous = fact('previous', 'Paris', 20, embedding=[0.0, 1.0])
    latest = fact('latest', 'Oslo', 30, supersedes=[previous.id], embedding=[1.0, 0.0])
    previous = replace(previous, predicate='based_in', relation_id='')
    latest = replace(latest, predicate='home_base', relation_id='')
    previous.invalid_at = latest.valid_at
    incoming = fact('incoming', 'Berlin', 10, embedding=[1.0, 0.0])
    model = FakeLLM(handler=lambda prompt, system: decisions(prompt))
    changed, stats = plan_merge([incoming], [previous, latest], model, Config(), object(), 100)
    assert stats['model_calls'] == 1
    by_id = {item.id: item for item in changed}
    assert previous.id not in by_id
    assert by_id[incoming.id].invalid_at == 30
    assert by_id[latest.id].supersedes == [incoming.id, previous.id]


def test_manual_duplicate_links_support_without_changing_user_labels():
    old = fact('manual', 'Paris', 1, source='user', categories=['personal_details'])
    changed, stats = merge([fact('duplicate', 'Paris', 1, sensitive=True)], [old])
    assert stats['model_calls'] == 0
    kept = changed[old.id]
    assert kept.provenance == ['manual-source', 'duplicate-source']
    assert (kept.source, kept.categories, kept.sensitive) == ('user', ['personal_details'], True)


def test_lexical_recall_includes_explicit_preference_reversal():
    old, new = fact('old', 'jazz', 10), fact('new', 'jazz', 20)
    old = replace(old, predicate='likes', relation_id='')
    new = replace(new, predicate='dislikes', relation_id='')
    changed, stats = merge([new], [old])
    assert stats['model_calls'] == 1
    assert changed[old.id].invalid_at == 20


def test_two_coexisting_predecessors_expire_at_the_adjudicated_successor():
    first, second, latest = fact('a', 'Alpha', 10), fact('b', 'Beta', 20), fact('c', 'Gamma', 30)
    changed, _ = merge([latest], [first, second])
    assert changed[first.id].invalid_at == changed[second.id].invalid_at == 30
    assert changed[latest.id].supersedes == [first.id, second.id]


def test_oversized_history_comparison_discards_candidate_without_truncating_evidence():
    config = Config()
    previous = fact('long', '会议' * 5000, 10)
    incoming = fact('short', 'Oslo', 20)

    def respond(prompt, system):
        assert len((system + prompt).encode()) <= config.extraction_max_input_bytes
        return decisions(prompt)

    changed, stats = merge([incoming], [previous], model=FakeLLM(handler=respond))
    assert stats['model_calls'] == 0
    assert not changed
    assert previous.invalid_at is None


def test_snapshot_rejects_legacy_assertion_before_writing_sources_or_facts():
    from dataclasses import asdict

    restored = MemoryEngine()
    snapshot = dict(
        format_version=6,
        facts=[asdict(fact('current', 'Paris', 10)),
               dict(asdict(fact('legacy', 'Oslo', 20)), assertion_kind='assistant_suggestion')],
        episodes=[dict(id='ep-1', content='Synthetic source', session_id='s', role='user', event_time=10)],
    )
    with pytest.raises(ValueError, match=r'facts\[1\]: unsupported field'):
        restored.import_snapshot(snapshot)
    assert restored._all_facts() == []
    assert not restored.episodes_doc.values()


def test_literal_case_conditions_and_subsecond_validity_survive_adjudication():
    first = fact('first', 'Ab1', 10.01, categories=['misc'])
    for new in (
        fact('case', 'ab1', 10.01),
        fact('time', 'Ab1', 10.02),
        fact('condition', 'Ab1', 10.01, text='Ana lives in Ab1 during the winter'),
    ):
        changed, stats = merge([new], [first], judge='coexists')
        assert new.id in changed and stats['model_calls'] == 1
    assert first.categories == ['misc']
