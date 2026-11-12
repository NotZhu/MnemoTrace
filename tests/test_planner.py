from tests.model_fixtures import seed_linked, seed_timeline
from mnemotrace import MemoryEngine
from mnemotrace.store.contracts import FactQuery


def test_explicit_path_returns_all_endpoints_and_supporting_facts():
    memory = MemoryEngine()
    one = seed_linked(memory, 'user', 'sister', 'Ana', text='user sister Ana')
    two = seed_linked(memory, 'user', 'sister', 'Bea', text='user sister Bea')
    three = seed_linked(memory, 'Ana', 'works_at', 'Orbit', text='Ana works at Orbit')
    four = seed_linked(memory, 'Bea', 'works_at', 'Nadir', text='Bea works at Nadir')
    result = memory.follow_path('user', ['sister', 'works_at'])
    assert {path.endpoint: path.facts for path in result.paths} == {
        'Orbit': (one, three), 'Nadir': (two, four),
    }
    assert not result.limitations
    assert memory.follow_path('user', ['sister', 'lives_in']).paths == ()
    assert memory.follow_path('user', ['sister', 'works_at'], facts=FactQuery(limit=1)).limitations
    assert not memory.follow_path('user', ['sister', 'works_at'], facts=FactQuery(exclude_sensitive=True)).paths


def test_statistics_respect_window_and_do_not_limit_count_to_evidence():
    memory = MemoryEngine()
    seed_linked(memory, 'Ana', 'visited', 'OrBit', valid_at=1, sensitive=False, text='Ana visited OrBit')
    seed_linked(memory, 'Bea', 'visited', 'orbit', valid_at=2, sensitive=False, text='Bea visited orbit')
    memory.add_fact('Bea', 'visited', 'Nadir', valid_at=3, sensitive=True, text='Bea visited Nadir')
    memory.add_fact('Other', 'visited', 'Elsewhere', valid_at=2, user_id='other', text='Other visited Elsewhere')
    scope = FactQuery(limit=1, exclude_sensitive=True)
    result = memory.aggregate_facts('visited', start=1, end=3, facts=scope)
    assert result.count == 2 and len(result.facts) == 1
    assert memory.aggregate_facts('visited', operation='count_distinct', facts=scope).count == 1
    assert memory.aggregate_facts('visited', start=2, end=3, facts=scope).count == 1


def test_statistics_return_source_evidence_for_an_unmodeled_event():
    memory = MemoryEngine()
    source = memory.add('Synthetic archived session')
    source.digest = 'I bought a synthetic model kit'
    memory._sync_episode(source)
    result = memory.aggregate_facts('bought', query='model kit')
    assert result.count == 0 and result.limitations == ('digest_candidates_only',)
    assert [candidate.id for candidate in result.candidates] == [source.id]
    redacted = memory.aggregate_facts('bought', query='model kit', facts=FactQuery(exclude_sensitive=True))
    assert not redacted.candidates


def test_plain_query_does_not_plan_or_execute_an_aggregate():
    class NoModelCalls:
        def complete(self, *args, **kwargs):
            raise AssertionError("The recall path must not plan a natural-language query")

    memory = MemoryEngine(llm=NoModelCalls())
    fact = memory.add_fact('Ana', 'visited', 'Orbit', text='Ana visited Orbit')
    query = 'How many times did Ana visit Orbit?'
    assert memory.search(query).top().id == fact.id
    assert 'AGGREGATION' not in memory.lean_context(query)


def test_historical_context_filters_sources_before_top_k():
    from unittest.mock import patch

    memory = MemoryEngine()
    with patch('time.time', return_value=100):
        old = memory.add('violet cipher archived in Cedar folder', event_time=10)
    with patch('time.time', return_value=200):
        new = memory.add('violet cipher', event_time=20)
    scope = FactQuery(valid_at=15)
    query = 'violet cipher'
    assert not memory.retrieve_digests(query)
    for source in (old, new):
        source.digest = source.content
        source.digest_written_at = source.ingested_at
        memory._sync_episode(source)
    assert memory.retrieve_digests(query, k=1)[0].id == new.id
    context = memory.lean_context(query, n_digests=1, facts=scope, n_facts=0)
    assert old.id in context and new.id not in context


def test_backfilled_replacements_keep_previous_validity_boundaries():
    from unittest.mock import patch

    memory = MemoryEngine()
    for known, valid, value in ((100, 1, 'First'), (200, 800, 'Last'), (300, 400, 'Middle')):
        with patch('time.time', return_value=known):
            seed_timeline(memory, 'Ana', 'works_at', value, valid_at=valid, text=' '.join(('Ana', 'works at', value)))

    from dataclasses import asdict

    restored = MemoryEngine()
    restored.import_snapshot(dict(format_version=4, facts=[asdict(fact) for fact in memory.fact_store.values()], episodes=[]))
    result = restored.aggregate_facts('works_at', facts=FactQuery(valid_at=600))
    assert [(fact.object, fact.invalid_at) for fact in result.facts] == [('Middle', 800)]


def test_backfill_queries_by_effective_time():
    from unittest.mock import patch

    memory = MemoryEngine()
    with patch('time.time', return_value=100):
        seed_timeline(memory, 'Ana', 'works_at', 'First', valid_at=1, text='Ana works at First')
    with patch('time.time', return_value=200):
        seed_timeline(memory, 'Ana', 'works_at', 'Second', valid_at=2, text='Ana works at Second')

    before = memory.aggregate_facts('works_at', facts=FactQuery(valid_at=1))
    after = memory.aggregate_facts('works_at', facts=FactQuery(valid_at=3))
    assert [(fact.object, fact.invalid_at, fact.expired_at) for fact in before.facts] == [('First', 2, 200)]
    assert [fact.object for fact in after.facts] == ['Second']
