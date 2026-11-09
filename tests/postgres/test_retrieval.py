from dataclasses import replace

import pytest

from mnemotrace import Config
from tests.embedder_fixtures import MockEmbedder, product_embedder
from mnemotrace.memory import Memory
from mnemotrace.retrieve.hybrid import date_terms
from mnemotrace.retrieve.lexical import bm25_scores
from mnemotrace.store.contracts import ChangeSet, CommitConditions, FactQuery, FactState, RecordKind, RecordWrite
from mnemotrace.store.contracts import RetrievalQuery, StoredRecord
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import DatabaseUnavailable, SpaceNotFound, VersionConflict
from mnemotrace.store.postgres.records import SOURCE, domain
from mnemotrace.store.postgres.retrieval import Corpus
from mnemotrace.types import Fact
from mnemotrace.util import cosine
from mnemotrace.worker import run_worker


@pytest.fixture
def memory(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Retrieval').space_id)
    return Memory(runtime, context, config=Config(access_boost=0),
                  embedder=product_embedder(), embedding_model='test-vectors-v1')


def test_lexical_mode_writes_and_recalls_chinese_without_vector_tasks(runtime, spaces, user):
    from tests.model_fixtures import RecordedLLM

    context = spaces.authorize(user, spaces.create(user, 'Lexical mode').space_id)
    memory = Memory(runtime, context, llm=RecordedLLM())
    original = memory.remember_raw('无关内容。' * 120 + '末尾记录绿茶', 'chat')
    memory.add_fact('user', 'sister', '王小明')
    memory.add_fact('王小明', 'works_at', '月亮公司')
    memory.add_fact('user', 'likes', '绿茶')
    memory.remember('I live in Shenzhen.', 'chat')
    assert memory.source_vector_operation(original.id) is None
    with memory.dao.read_snapshot() as view:
        sql = view._sql
        assert not sql.run('SELECT 1 FROM mnemotrace.embeddings WHERE user_id=%s AND space_id=%s',
                           sql.scope).fetchall()
        assert not sql.run("SELECT 1 FROM mnemotrace.operations WHERE user_id=%s AND space_id=%s AND kind='reembed'",
                           sql.scope).fetchall()
    fresh = Memory(runtime, context)
    assert fresh.search('绿茶').answer() == '绿茶'
    result = fresh.follow_path('user', ['sister', 'works_at'])
    assert result.paths[0].endpoint == '月亮公司'
    assert [fact.predicate for fact in result.paths[0].facts] == ['sister', 'works_at']
    assert fresh.retrieve_episodes('绿茶').episodes[0].id == original.id
    assert fresh.search('Where do I live?').answer() == 'Shenzhen'
    with fresh.dao.read_snapshot() as view:
        previous = view.get(SOURCE, original.id)
        snapshot = view.snapshot
    summary = replace(domain(previous), summary='A short summary.')
    record = StoredRecord.from_record(context, summary, summary_version=1,
                                      record_version=previous.record_version + 1)
    fresh.dao.commit(ChangeSet(CommitConditions(snapshot), 'lexical-summary',
                               writes=(RecordWrite(record, previous.record_version),)))
    result = fresh.lean_context('末尾记录', n_facts=0, n_chunks=0, n_summaries=1)
    assert 'A short summary.' in result.context and '末尾记录绿茶' not in result.context


def test_source_bm25_rejects_english_stop_word_only_matches(memory):
    memory.remember_raw('A17 is written on a blue folder.', 'chat')
    sources = [memory.remember_raw(f'Apollo launch code rehearsal note {i}.', 'chat') for i in range(5)]
    assert memory.retrieve_episodes("What is Apollo's launch code?", k=1).episodes[0].id in {
        source.id for source in sources
    }
    assert not memory.retrieve_episodes('What is Kryptonite?').episodes


def test_exact_vector_and_bm25_use_full_scoped_corpus(memory, runtime, spaces, user, accounts):
    old = memory.add_fact('Ana', 'works_at', 'Acme', valid_at=100)
    current = memory.add_fact('Ana', 'works_at', 'Orbit', valid_at=200, cardinality='single')
    repeat = memory.add_fact('Bo', 'likes', 'Orbit Orbit', valid_at=150)
    sibling = spaces.authorize(user, spaces.create(user, 'Other space').space_id)
    sibling_memory = Memory(runtime, sibling)
    sibling_memory.add_fact('Ana', 'works_at', 'Orbit Orbit Orbit', valid_at=200)
    other = accounts.create_user()
    try:
        unrelated = spaces.authorize(other, spaces.create(other, 'Other user').space_id)
        Memory(runtime, unrelated).add_fact('Ana', 'works_at', 'Orbit', valid_at=200)
        vector = tuple(memory._embedder.embed('works Orbit'))
        with memory.dao.read_snapshot() as view:
            for state, facts in ((FactState.CURRENT, (current, repeat)), (FactState.ALL, (old, current, repeat))):
                query = RetrievalQuery(FactQuery(state=state, limit=1), 'works Orbit', vector, 'test-vectors-v1')
                corpus = Corpus(view, query, RecordKind.FACT)
                expected = bm25_scores(query.text, [(fact.id, f'{fact.text} {date_terms(fact.valid_at)}') for fact in facts])
                assert corpus.lexical_scores(ids=[fact.id for fact in facts]) == pytest.approx(expected)
                assert corpus.lexical_scores() == pytest.approx(dict(sorted(expected.items(), key=lambda row: -row[1])[:1]))
                semantic = {fact.id: cosine(list(vector), memory._embedder.embed(fact.text)) for fact in facts}
                assert corpus.vector_scores() == pytest.approx(dict(sorted(semantic.items(), key=lambda row: (-row[1], row[0]))[:1]))
                assert Corpus(view, replace(query, embedding_model='other'), RecordKind.FACT).vector_scores() == {}
            assert Corpus(view, replace(query, vector=(0.0,) * len(vector)), RecordKind.FACT).vector_scores() == {}
        assert memory.search('Where does Ana work?').answer() == 'Orbit'
        assert memory.search('Where did Ana work?', facts=FactQuery(state=FactState.HISTORY)).answer() == 'Acme'
        assert 'Other space' not in memory.lean_context('Where does Ana work?').context
        with pytest.raises(SpaceNotFound):
            memory.recall_multi((sibling.space_id, unrelated.space_id), 'Orbit')
    finally:
        accounts.delete_user(other)


def test_graph_alias_path_and_known_at(memory):
    memory.add_fact('Wei', 'colleague', 'Lin', valid_at=100)
    job = memory.add_fact('Lin', 'works_at', 'Orbit', valid_at=100)
    memory.add_fact('Orbit', 'based_in', 'Lisbon', valid_at=100)
    memory.set_entity_aliases('Orbit', ['Orbital Lab'])
    orbit, = memory.graph_entities('Orbital Lab')
    assert {edge.fact_id for edge in memory.graph_neighbors(orbit.id, direction='in')} == {job.id}
    assert memory.follow_path('Wei', ['colleague', 'works_at']).paths[0].endpoint == 'Orbit'
    assert memory.follow_path('Orbital Lab', ['based_in', 'unknown']).paths == ()
    memory.add_fact('Former Orbit', 'based_in', 'Lisbon', valid_at=100)
    memory.set_entity_aliases('Former Orbit', ['Old Lab'])
    former, = memory.graph_entities('Old Lab')
    memory.merge_entities(orbit.id, former.id)
    assert memory.follow_path('Wei', ['colleague', 'works_at']).paths[0].endpoint == 'Orbit'
    assert memory.graph_relations(job.id, facts=FactQuery(valid_at=150, known_at=150)) == ()
    assert memory.graph_relations(job.id, facts=FactQuery(valid_at=150))
    original, = memory.graph_relations(job.id)
    with memory.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = %s', (job.id,))
    memory.rebuild_graph()
    rebuilt, = memory.graph_relations(job.id)
    assert rebuilt.id == original.id
    assert memory.graph_entities('Orbital Lab') == (orbit,)


def test_shared_alias_is_resolved_independently_of_candidate_budget(memory):
    from mnemotrace.store.postgres.graph_reads import RetrievalGraph

    memory.add_fact('Ana', 'likes', 'Orbit')
    memory.add_fact('Bo', 'likes', 'Elsewhere')
    memory.set_entity_aliases('Orbit', ['Shared'])
    orbit, = memory.graph_entities('Orbit')
    elsewhere, = memory.graph_entities('Elsewhere')
    memory.set_entity_aliases('Elsewhere', ['Shared', 'Orbit'])
    query = memory._query('except Orbit', top_k=1, facts=FactQuery(limit=1))
    with memory.dao.read_snapshot() as view:
        graph = RetrievalGraph(view, query)
        assert graph.get_entity(memory.context.user_id, 'Shared') is None
        unanchored = RetrievalGraph(view, replace(query, text=''))
        assert unanchored.get_entity(memory.context.user_id, 'Orbit').id == orbit.id
    memory.merge_entities(orbit.id, elsewhere.id)
    with memory.dao.read_snapshot() as view:
        graph = RetrievalGraph(view, query)
        assert graph.get_entity(memory.context.user_id, 'Shared').id == orbit.id


def test_limited_candidates_can_rank_both_published_facts_in_a_slot(memory):
    # The salience shortlist should find Orbit while the other shortlists find Orion.
    manual = memory.add_fact('Ana', 'works_at', 'Orbit', valid_at=100)
    shadow = Fact('Ana', 'works_at', 'Orion', user_id=memory.context.user_id, valid_at=200,
                  source='extracted', salience=0.5, id='fixture-retrieval-orion')
    write = memory._write(shadow)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    memory.dao.commit(ChangeSet(CommitConditions(snapshot), 'synthetic-competing-record', writes=(write,),
                                embeddings=memory._embeddings(write, shadow.text)))
    query = memory._query(shadow.text, top_k=1, facts=FactQuery(limit=1))
    with memory.dao.read_snapshot() as view:
        batch = view.candidates(query)
        assert {item.record.ref.record_id for item in batch.candidates} == {manual.id, shadow.id}
        corpus = Corpus(view, query, RecordKind.FACT)
        assert tuple(corpus.vector_scores()) == (shadow.id,)
        expected = bm25_scores(query.text, [(item.id, f'{item.text} {date_terms(item.valid_at)}')
                                            for item in (manual, shadow)])
        assert corpus.lexical_scores() == pytest.approx({shadow.id: expected[shadow.id]})


def test_explicit_candidate_limit_is_independent_of_output_count(memory):
    request = memory._query('Clover', top_k=10, facts=FactQuery(limit=1))
    assert request.facts.limit == 1


def test_redacted_aggregation_does_not_include_original_source_evidence(memory):
    memory.remember_raw('I recently finished a Tamiya 1/48 scale Spitfire Mk.V model kit.', 'chat', event_time=100)
    result = memory.aggregate_facts('bought', query='Spitfire model kit')
    assert result.count == 0 and result.candidates
    assert 'source_candidates_only' in result.limitations
    hidden = memory.aggregate_facts('bought', query='Spitfire model kit',
                                    facts=FactQuery(exclude_sensitive=True))
    assert not hidden.candidates


def test_aggregate_counts_full_snapshot_and_respects_time_and_redaction(memory):
    memory.add_fact('Ana', 'visited', 'Orbit', valid_at=10, sensitive=False)
    memory.add_fact('Bea', 'visited', 'ORBIT', valid_at=20, sensitive=False)
    memory.add_fact('Bea', 'visited', 'Elsewhere', valid_at=30, sensitive=True)
    scope = FactQuery(limit=1, exclude_sensitive=True)
    matches = memory.aggregate_facts('visited', start=10, end=30, facts=scope)
    assert matches.count == 2 and len(matches.facts) == 1
    assert memory.aggregate_facts('visited', operation='count_distinct', facts=scope).count == 1
    assert memory.aggregate_facts('visited', start=20, end=30, facts=scope).count == 1


def test_historical_context_excludes_working_memory_created_later(memory):
    item = memory.remember_working('Synthetic pending task', 'chat', event_time=100)
    assert item.content in memory.lean_context('pending task', session_id='chat').context
    historical = memory.lean_context('pending task', session_id='chat', as_of=item.created_at - 1)
    assert item.content not in historical.context


def test_search_returns_snapshot_result_when_access_reinforcement_conflicts(memory, cluster, monkeypatch):
    fact = memory.add_fact('Ana', 'likes', 'Clover')
    memory._config.access_boost = 0.5
    commit = memory._commit
    with Database(cluster.runtime) as second:
        concurrent = Memory(second, memory.context)

        def interleaved_commit(*args, **kwargs):
            assert concurrent.search('What does Ana like?').answer() == 'Clover'
            return commit(*args, **kwargs)

        monkeypatch.setattr(memory, '_commit', interleaved_commit)
        assert memory.search('What does Ana like?').answer() == 'Clover'
    assert memory.get_fact(fact.id).access_count == 1


def test_retrieval_ignores_old_text_vectors_after_edit(memory):
    old = memory.add_fact('Ana', 'likes', 'Clover')
    edited = memory.update_fact(old.id, object='Orion')
    request = memory._query('Clover', top_k=5)
    with memory.dao._transaction() as sql:
        sql.insert('embeddings', {
            'fact_id': old.id, 'target_kind': 'Fact', 'field': 'text', 'content_version': 1,
            'model': 'test-vectors-v1', 'dimension': len(request.vector),
            'vector': '[' + ','.join(map(str, request.vector)) + ']',
        })
    with memory.dao.read_snapshot() as view:
        corpus = Corpus(view, request, RecordKind.FACT)
        expected = cosine(list(request.vector), memory._embedder.embed(edited.text))
        assert corpus.vector_scores() == pytest.approx({old.id: expected})
        assert corpus.lexical_scores() == {}


def test_vector_queries_plan_for_each_scope_even_after_connection_reuse(memory):
    fact = memory.add_fact('Ana', 'likes', 'Clover')
    request = memory._query('Clover', top_k=5)
    with memory.dao.read_snapshot() as view:
        corpus = Corpus(view, request, RecordKind.FACT)
        for _ in range(8):
            assert tuple(corpus.vector_scores()) == (fact.id,)
        prepared = view._sql.run("SELECT statement FROM pg_prepared_statements "
                                 "WHERE position('scoped AS MATERIALIZED' in statement) > 0").fetchall()
        assert prepared == []


def test_source_vector_coverage_worker_and_erase_fencing(memory, runtime, cluster):
    source = memory.remember_raw('The synthetic archive code is Bluebird', 'chat')
    operation = memory.source_vector_operation(source.id)
    assert operation.kind.value == 'reembed'
    before = memory.retrieve_episodes('Bluebird')
    assert before.episodes[0].id == source.id
    assert before.limitations == ('source_vectors_processing',)
    with memory.dao.read_snapshot() as view:
        revision = view.snapshot.core_revision
    with Database(cluster.discovery, purpose='discovery') as discovery:
        assert discovery.discover_processing(embedding=('other', 256), include_extraction=False) == ()
        run_worker(discovery, runtime, Config(), None, product_embedder(), 'test-vectors-v1', once=True)
    after = memory.retrieve_episodes('Bluebird')
    assert after.complete and after.episodes[0].id == source.id
    assert memory.operation(operation.operation_id).state.value == 'completed'
    with memory.dao.read_snapshot() as view:
        assert view.snapshot.core_revision == revision

    another = memory.remember_raw('Another synthetic archive', 'chat')
    operation = memory.source_vector_operation(another.id)
    tasks = memory._processing.tasks
    lease = tasks.claim(operation.operation_id, embedding=memory._embedding_space)
    with memory.dao.read_snapshot() as view:
        record = view.get(SOURCE, another.id)
    memory.clear_space()
    with pytest.raises(VersionConflict):
        tasks.commit_source_vector(lease, record, 'test-vectors-v1',
                                   tuple(memory._embedder.embed(domain(record).content)))
    assert memory.get_episode(another.id) is None


def test_summary_lexical_projection_follows_text_version(memory):
    source = memory.remember_raw('Synthetic material', 'chat')
    for version, text in enumerate(('Clover', 'Snowdrop'), 1):
        with memory.dao.read_snapshot() as view:
            previous = view.get(SOURCE, source.id)
            snapshot = view.snapshot
        updated = replace(domain(previous), summary=text)
        record = StoredRecord.from_record(memory.context, updated, summary_version=version,
                                          record_version=previous.record_version + 1)
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), f'summary-test-{version}',
                                    writes=(RecordWrite(record, previous.record_version),)))
        result = memory.retrieve_episodes(text)
        assert [(ep.id, ep.text, ep.derived) for ep in result.episodes] == [(source.id, text, True)]
    assert memory.retrieve_episodes('Clover').episodes == ()


def test_historical_retrieval_uses_raw_text_until_the_current_summary_is_known(memory):
    source = memory.remember_raw('Project Orchid original wording', 'chat', event_time=100)
    cutoffs = []
    for version, text in enumerate(('Project Orchid first summary', 'Project Orchid later detail'), 1):
        with memory.dao.read_snapshot() as view:
            previous = view.get(SOURCE, source.id)
            snapshot = view.snapshot
            cutoffs.append(view.at.timestamp())
        record = StoredRecord.from_record(memory.context, replace(domain(previous), summary=text),
                                          summary_version=version, record_version=previous.record_version + 1)
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), f'historical-summary-{version}',
                                    writes=(RecordWrite(record, previous.record_version),)))

    for cutoff in cutoffs:
        historical, = memory.retrieve_episodes('Orchid', as_of=cutoff).episodes
        assert historical.text == source.content and not historical.derived
        context = memory.lean_context('Orchid', as_of=cutoff).context
        assert source.content in context and 'later detail' not in context
    current, = memory.retrieve_episodes('Orchid').episodes
    assert current.text == 'Project Orchid later detail' and current.derived
    with memory.dao.read_snapshot() as view:
        after_summary = view.at.timestamp()
    historical, = memory.retrieve_episodes('Orchid', as_of=after_summary).episodes
    assert historical == current

    with memory.dao._transaction() as sql:
        sql.update('source_summaries', {'summary_written_at': None}, 'source_id = %s', (source.id,))
    historical, = memory.retrieve_episodes('Orchid', as_of=after_summary).episodes
    assert historical.text == source.content and not historical.derived
    assert memory.retrieve_episodes('Orchid').episodes == (current,)


def test_retrieval_backfill_is_paged_and_deduplicated(memory):
    source = memory.remember_raw('The imported record is snowdrop', 'chat')
    operation = memory.source_vector_operation(source.id)
    with memory.dao._transaction() as sql:
        sql.delete('operations', 'operation_id = %s', (operation.operation_id,))
        sql.delete('lexical_documents', 'source_id = %s', (source.id,))
    assert set(memory.retrieve_episodes('snowdrop').limitations) == {
        'lexical_backfill_pending', 'source_vectors_pending_backfill',
    }
    assert memory.backfill_retrieval(RecordKind.SOURCE, limit=1) == source.id
    assert memory.backfill_retrieval(RecordKind.SOURCE, after_id=source.id, limit=1) is None
    assert memory.backfill_retrieval(RecordKind.SOURCE, limit=1) == source.id
    assert memory.source_vector_operation(source.id).operation_id == operation.operation_id
    result = memory.retrieve_episodes('snowdrop')
    assert result.episodes[0].id == source.id
    assert result.limitations == ('source_vectors_processing',)


def test_source_failure_is_visible_and_retry_keeps_the_original(memory, runtime):
    class InvalidEmbedder(MockEmbedder):
        def embed_batch(self, texts):
            return []

    source = memory.remember_raw('The synthetic archive code is Clover', 'chat')
    task = memory.source_vector_operation(source.id)
    broken = Memory(runtime, memory.context, embedder=InvalidEmbedder(), embedding_model='test-vectors-v1')
    assert broken._processing.run_one(task.operation_id)
    assert memory.operation(task.operation_id).error_code == 'embedding_invalid'
    assert memory.retrieve_episodes('Clover').limitations == ('source_vectors_failed',)
    assert memory.get_episode(source.id).content == source.content
    assert memory.retry_operation(task.operation_id).state.value == 'pending'
    assert memory._processing.run_one(task.operation_id)
    assert memory.retrieve_episodes('Clover').complete


def test_source_vector_retrieves_without_lexical_overlap(memory, runtime):
    class SynonymEmbedder(MockEmbedder):
        def embed(self, text):
            return [1.0, 0.0]

    memory = Memory(runtime, memory.context, embedder=SynonymEmbedder(2), embedding_model='synthetic')
    source = memory.remember_raw('Celestial star', 'chat')
    assert memory.retrieve_episodes('Daybreak').episodes == ()
    operation = memory.source_vector_operation(source.id)
    assert memory._processing.run_one(operation.operation_id)
    assert [ep.id for ep in memory.retrieve_episodes('Daybreak').episodes] == [source.id]
    with memory.dao.read_snapshot() as view:
        previous = view.get(SOURCE, source.id)
        snapshot = view.snapshot
    summary = replace(domain(previous), summary='Condensed evidence')
    record = StoredRecord.from_record(memory.context, summary, summary_version=1,
                                      record_version=previous.record_version + 1)
    memory.dao.commit(ChangeSet(CommitConditions(snapshot), 'source-summary',
                                writes=(RecordWrite(record, previous.record_version),)))
    context = memory.lean_context('Daybreak', n_facts=0, n_chunks=0, n_summaries=1)
    assert 'Condensed evidence' in context.context


def test_snapshot_keeps_all_signals_at_one_version_and_failure_stays_visible(memory, cluster):
    memory.add_fact('Ana', 'likes', 'Clover')
    request = memory._query('Clover', top_k=5)
    with Database(cluster.runtime) as second:
        writer = Memory(second, memory.context)
        with memory.dao.read_snapshot() as view:
            before = view.candidates(request)
            writer.add_fact('Bo', 'likes', 'Clover')
            assert view.candidates(request) == before
        assert len(memory.search('Clover').facts) == 2
        second.close()
        with pytest.raises(DatabaseUnavailable):
            writer.search('Clover')


def test_http_structured_queries_use_authorized_space_and_redaction(product_client, product_scope):
    context, key = product_scope()
    space_id = context.space_id
    headers = {'Authorization': f'Bearer {key}'}

    def post(path, **body):
        return product_client.post(path, json={'space_id': space_id, **body}, headers=headers)

    for subject, predicate, object_, sensitive in (
        ('user', 'sister', 'Ana', False),
        ('Ana', 'works_at', 'Orbit', False),
        ('Ana', 'works_at', 'Secret', True),
    ):
        assert post('/v1/facts', subject=subject, predicate=predicate,
                    object=object_, sensitive=sensitive).status_code == 200
    result = post('/v1/aggregate', predicate='works_at', redact_sensitive=True, limit=1)
    assert result.status_code == 200
    assert result.json()['count'] == 1
    result = post('/v1/follow-path', anchor='user', predicates=['sister', 'works_at'], redact_sensitive=True)
    assert result.status_code == 200
    assert [path['endpoint'] for path in result.json()['paths']] == ['Orbit']
    assert post('/v1/follow-path', anchor='user', predicates=['sister']).status_code == 422
    foreign, _ = product_scope('foreign')
    denied = product_client.post('/v1/aggregate', json={'space_id': foreign.space_id, 'predicate': 'works_at'},
                                 headers=headers)
    assert denied.status_code in (403, 404)


def test_explicit_path_disambiguates_only_visible_anchors(memory):
    memory.add_fact('Ana', 'sister', 'Cora', sensitive=False)
    memory.add_fact('Bea', 'sister', 'Cora', sensitive=True)
    memory.add_fact('Cora', 'works_at', 'Orbit', sensitive=False)
    memory.set_entity_aliases('Ana', ['Shared'])
    memory.set_entity_aliases('Bea', ['Shared'])
    with pytest.raises(ValueError, match='canonical entity name'):
        memory.follow_path('Shared', ['sister', 'works_at'])
    result = memory.follow_path('Shared', ['sister', 'works_at'], facts=FactQuery(exclude_sensitive=True))
    assert result.paths[0].endpoint == 'Orbit'
    assert result.paths[0].facts[0].subject == 'Ana'


@pytest.mark.parametrize('clock_skew', [0, -60, 60])
def test_backfilled_validity_is_read_at_the_selected_knowledge_time(memory, cluster, monkeypatch, clock_skew):
    from time import time
    from mnemotrace.consolidate.manual import build_manual_fact

    def client_clock_fact(*args, **kwargs):
        fact = build_manual_fact(*args, **kwargs)
        fact.created_at += clock_skew
        return fact

    monkeypatch.setattr('mnemotrace.memory.build_manual_fact', client_clock_fact)

    base = time()
    memory.add_fact('Ana', 'works_at', 'First', valid_at=base - 100, cardinality='single')
    with memory.dao.read_snapshot() as view:
        first_known = view.at.timestamp()
    memory.add_fact('Ana', 'works_at', 'Last', valid_at=base + 800, cardinality='single')
    with memory.dao.read_snapshot() as view:
        last_known = view.at.timestamp()
    memory.add_fact('Ana', 'works_at', 'Middle', valid_at=base + 400, cardinality='single')
    with memory.dao.read_snapshot() as view:
        middle_known = view.at.timestamp()

    with Database(cluster.runtime) as reopened:
        fresh = Memory(reopened, memory.context)
        for known, expected, end in ((first_known, 'First', None), (last_known, 'First', base + 800),
                                     (middle_known, 'Middle', base + 800)):
            scope = FactQuery(valid_at=base + 600, known_at=known)
            fact, = fresh.aggregate_facts('works_at', facts=scope).facts
            assert fact.object == expected
            if end is None:
                assert fact.invalid_at is None
            else:
                assert fact.invalid_at == pytest.approx(end, abs=1e-6, rel=0)
            assert fresh.search('Where does Ana work?', facts=scope).top().id == fact.id
        history = FactQuery(state=FactState.HISTORY, valid_at=base + 600, known_at=last_known)
        assert fresh.aggregate_facts('works_at', facts=history).count == 0
