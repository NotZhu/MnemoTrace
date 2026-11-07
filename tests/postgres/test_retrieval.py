from dataclasses import replace

import pytest

from mnemotrace import Config
from mnemotrace.embed import HashingEmbedder
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
    return Memory(runtime, context, config=Config(access_boost=0))


def test_exact_vector_and_bm25_use_full_scoped_corpus(memory, runtime, spaces, user, accounts):
    old = memory.add_fact('Ana', 'works_at', 'Acme', valid_at=100)
    current = memory.add_fact('Ana', 'works_at', 'Orbit', valid_at=200)
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
                query = RetrievalQuery(FactQuery(state=state, limit=1), 'works Orbit', vector, 'hashing-v1')
                corpus = Corpus(view, query, RecordKind.FACT)
                expected = bm25_scores(query.text, [(fact.id, f'{fact.text} {date_terms(fact.valid_at)}') for fact in facts])
                assert corpus.lexical_scores(ids=[fact.id for fact in facts]) == pytest.approx(expected)
                assert corpus.lexical_scores() == pytest.approx(dict(sorted(expected.items(), key=lambda row: -row[1])[:1]))
                semantic = {fact.id: cosine(list(vector), memory._embedder.embed(fact.text)) for fact in facts}
                assert corpus.vector_scores() == pytest.approx(dict(sorted(semantic.items(), key=lambda row: -row[1])[:1]))
                assert Corpus(view, replace(query, embedding_model='other'), RecordKind.FACT).vector_scores() == {}
            assert Corpus(view, replace(query, vector=(0.0,) * len(vector)), RecordKind.FACT).vector_scores() == {}
        assert memory.search('Where does Ana work?').answer() == 'Orbit'
        assert memory.search('Where did Ana work before Orbit?').answer() == 'Acme'
        assert 'Other space' not in memory.lean_context('Where does Ana work?').context
        with pytest.raises(SpaceNotFound):
            memory.recall_multi((sibling.space_id, unrelated.space_id), 'Orbit')
    finally:
        accounts.delete_user(other)


def test_graph_alias_multihop_exclusion_and_known_at(memory):
    memory.add_fact('Wei', 'colleague', 'Lin', valid_at=100)
    job = memory.add_fact('Lin', 'works_at', 'Orbit', valid_at=100)
    memory.add_fact('Orbit', 'based_in', 'Lisbon', valid_at=100)
    memory.set_entity_aliases('Orbit', ['Orbital Lab'])
    orbit, = memory.graph_entities('Orbital Lab')
    assert {edge.fact_id for edge in memory.graph_neighbors(orbit.id, direction='in')} == {job.id}
    assert memory.search("Where does Wei's colleague work?").answer() == 'Orbit'
    result = memory.search('Lin works except Orbit')
    assert all(fact.id != job.id for fact in result.facts)
    memory.add_fact('Former Orbit', 'based_in', 'Lisbon', valid_at=100)
    memory.set_entity_aliases('Former Orbit', ['Old Lab'])
    former, = memory.graph_entities('Old Lab')
    memory.merge_entities(orbit.id, former.id)
    result = memory.search('Lin works except Old Lab')
    assert all(fact.id != job.id for fact in result.facts)
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
        graph.exclude((orbit.id,))
        assert graph.get_entity(memory.context.user_id, 'Shared') is None
        assert 'shared' not in graph.excluded_names()
        unanchored = RetrievalGraph(view, replace(query, text=''))
        assert unanchored.get_entity(memory.context.user_id, 'Orbit').id == orbit.id
    memory.merge_entities(orbit.id, elsewhere.id)
    with memory.dao.read_snapshot() as view:
        graph = RetrievalGraph(view, query)
        assert graph.get_entity(memory.context.user_id, 'Shared').id == orbit.id


def test_limited_candidates_keep_manual_slot_head_and_bm25_excludes_shadow(memory):
    # Record-level imports can leave two current assertions in one slot.
    manual = memory.add_fact('Ana', 'works_at', 'Orbit', valid_at=100)
    shadow = Fact('Ana', 'works_at', 'Orion', user_id=memory.context.user_id, valid_at=200, source='extracted')
    write = memory._write(shadow)
    with memory.dao.read_snapshot() as view:
        snapshot = view.snapshot
    memory.dao.commit(ChangeSet(CommitConditions(snapshot), 'synthetic-competing-record', writes=(write,),
                                embeddings=(memory._embedding(write, shadow.text),)))
    query = memory._query(shadow.text, top_k=1, facts=FactQuery(limit=1))
    with memory.dao.read_snapshot() as view:
        batch = view.candidates(query)
        assert [item.record.ref.record_id for item in batch.candidates] == [manual.id]
        corpus = Corpus(view, query, RecordKind.FACT)
        assert tuple(corpus.vector_scores()) == (manual.id,)
        expected = bm25_scores(query.text, [(manual.id, f'{manual.text} {date_terms(manual.valid_at)}')])
        assert corpus.lexical_scores() == pytest.approx(expected)


def test_vector_and_scalar_budgets_exclude_graph_matches_before_cutoff(memory):
    allowed = memory.add_fact('Ana', 'likes', 'Clover', valid_at=100)
    excluded = memory.add_fact('Ana', 'likes', 'Orbit', valid_at=200)
    with memory.dao.read_snapshot() as view:
        snapshot, record = view.snapshot, view.get(RecordKind.FACT, excluded.id)
    memory._commit(snapshot, writes=(memory._write(replace(domain(record), salience=100), record),))
    orbit, = memory.graph_entities('Orbit')
    query = memory._query(excluded.text, top_k=1, facts=FactQuery(limit=1))
    with memory.dao.read_snapshot() as view:
        corpus = Corpus(view, query, RecordKind.FACT, excluded=(orbit.id,))
        assert tuple(corpus.vector_scores()) == (allowed.id,)
        assert corpus.scalar_candidates() == (allowed.id,)


def test_graph_exclusion_covers_unprojected_facts_with_an_excluded_alias(memory):
    allowed = memory.add_fact('Ana', 'likes', 'Clover', valid_at=100)
    memory.add_fact('Bo', 'likes', 'Orbit', valid_at=200)
    memory.set_entity_aliases('Orbit', ['Orbital Lab'])
    hidden = memory.add_fact('Orbital Lab', 'procedure', 'Synthetic notes', valid_at=300)
    assert memory.graph_relations(hidden.id) == ()
    query = memory._query('except Orbit', top_k=1, facts=FactQuery(limit=1))
    with memory.dao.read_snapshot() as view:
        assert tuple(candidate.record.ref.record_id for candidate in view.candidates(query).candidates) == (allowed.id,)


def test_explicit_candidate_limit_is_independent_of_output_count(memory):
    request = memory._query('Clover', top_k=10, facts=FactQuery(limit=1))
    assert request.facts.limit == 1


def test_redacted_aggregation_does_not_include_original_source_evidence(memory):
    memory.remember_raw('I recently finished a Tamiya 1/48 scale Spitfire Mk.V model kit.', 'chat', event_time=100)
    query = 'How many model kits have I worked on or bought?'
    assert 'Spitfire' in memory.lean_context(query).context
    context = memory.lean_context(query, redact_sensitive=True).context
    assert 'Spitfire' not in context and 'AGGREGATION CANDIDATES' not in context


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
            'model': 'hashing-v1', 'dimension': len(request.vector),
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
        run_worker(discovery, runtime, Config(), None, HashingEmbedder(), 'hashing-v1', once=True)
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
        tasks.commit_source_vector(lease, record, 'hashing-v1',
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
    class InvalidEmbedder(HashingEmbedder):
        def embed_batch(self, texts):
            return []

    source = memory.remember_raw('The synthetic archive code is Clover', 'chat')
    task = memory.source_vector_operation(source.id)
    broken = Memory(runtime, memory.context, embedder=InvalidEmbedder(), embedding_model='hashing-v1')
    assert broken._processing.run_one(task.operation_id)
    assert memory.operation(task.operation_id).error_code == 'embedding_invalid'
    assert memory.retrieve_episodes('Clover').limitations == ('source_vectors_failed',)
    assert memory.get_episode(source.id).content == source.content
    assert memory.retry_operation(task.operation_id).state.value == 'pending'
    assert memory._processing.run_one(task.operation_id)
    assert memory.retrieve_episodes('Clover').complete


def test_source_vector_retrieves_without_lexical_overlap(memory, runtime):
    class SynonymEmbedder(HashingEmbedder):
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
