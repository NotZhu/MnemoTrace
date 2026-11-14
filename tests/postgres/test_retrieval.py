from dataclasses import replace

import pytest

from mnemotrace import Config
from tests.embedder_fixtures import MockEmbedder, product_embedder
from tests.postgres.fact_fixtures import seed_fact, seed_note
from mnemotrace.memory import Memory
from mnemotrace.retrieve.hybrid import date_terms
from mnemotrace.retrieve.lexical import bm25_scores
from mnemotrace.store.contracts import ChangeSet, CommitConditions, EmbeddingWrite, FactQuery, FactState, RecordKind, RecordWrite
from mnemotrace.store.contracts import RetrievalQuery, StoredRecord
from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.errors import DatabaseUnavailable, SpaceNotFound
from mnemotrace.store.postgres.maintenance import rebuild_graph, rebuild_lexical_index
from mnemotrace.store.postgres.records import SOURCE, domain
from mnemotrace.store.postgres.retrieval import Corpus
from mnemotrace.types import Fact
from mnemotrace.util import cosine


@pytest.fixture
def memory(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Retrieval').space_id)
    return Memory(runtime, context, config=Config(access_boost=0),
                  embedder=product_embedder(), embedding_model='test-vectors-v1')


def test_exact_graph_query_normalizes_names_with_surrounding_spaces(memory):
    padded, plain = '\u2003Synthetic Alice\u2003', 'Synthetic Alice'
    for subject in (padded, plain):
        seed_fact(memory, subject, 'works_at', 'Synthetic Lab', linked=True, text=f'{subject} works at Synthetic Lab.')

    padded_entity, = memory.graph_entities(padded.upper())
    plain_entity, = memory.graph_entities(plain)
    assert padded_entity.id == plain_entity.id
    with memory.dao.read_snapshot() as view:
        assert view.entity(padded.upper(), share_safe=True).ref.record_id == plain_entity.id
    assert len(memory.facts(FactQuery(subject=padded.upper(), predicate=' WORKS_AT '))) == 2
    assert len(memory.history(plain, ' WORKS_AT ')) == 2


def test_shared_entity_name_evidence_uses_the_same_normalization(memory):
    padded = '\u2003İstanbul\u2003'
    fact = seed_fact(memory, 'Synthetic traveler', 'visited', padded, linked=True,
                     text='Synthetic traveler visited İstanbul.')
    with memory.dao.read_snapshot() as view:
        assert view.entity('İSTANBUL', share_safe=True).ref.record_id == fact.object_id
    memory.set_fact_sensitive(fact.id, True)
    with memory.dao.read_snapshot() as view:
        assert view.entity('İSTANBUL', share_safe=True) is None


def test_lexical_mode_writes_and_recalls_chinese_without_vector_tasks(runtime, spaces, user):
    from tests.model_fixtures import RecordedLLM

    context = spaces.authorize(user, spaces.create(user, 'Lexical mode').space_id)
    memory = Memory(runtime, context, llm=RecordedLLM())
    original = seed_note(memory, '无关内容。' * 120 + '末尾记录绿茶', 'chat')
    seed_fact(memory, '我', 'sister', '王小明', linked=True, text='user sister 王小明')
    seed_fact(memory, '王小明', 'works_at', '月亮公司', linked=True, text='王小明 works at 月亮公司')
    seed_fact(memory, '我', 'likes', '绿茶', linked=True, text='user likes 绿茶')
    assert memory.remember('I live in Shenzhen.', 'chat')['ok']
    with memory.dao.read_snapshot() as view:
        sql = view._sql
        assert not sql.run('SELECT 1 FROM mnemotrace.embeddings WHERE user_id=%s AND space_id=%s',
                           sql.scope).fetchall()
        assert not sql.run("SELECT 1 FROM mnemotrace.operations WHERE user_id=%s AND space_id=%s AND kind='reembed'",
                           sql.scope).fetchall()
    fresh = Memory(runtime, context)
    assert fresh.search('绿茶').top().object == '绿茶'
    result = fresh.follow_path('我', ['sister', 'works_at'])
    assert result.paths[0].endpoint == '月亮公司'
    assert [fact.predicate for fact in result.paths[0].facts] == ['sister', 'works_at']
    assert fresh.get_episode(original.id).content == original.content
    assert not fresh.retrieve_digests('绿茶').digests
    assert fresh.search('Where do I live?').top().object == 'Shenzhen'
    with fresh.dao.read_snapshot() as view:
        previous = view.get(SOURCE, original.id)
        snapshot = view.snapshot
    digest = replace(domain(previous), digest='A short digest.')
    record = StoredRecord.from_record(context, digest, digest_version=1,
                                      record_version=previous.record_version + 1)
    fresh.dao.commit(ChangeSet(CommitConditions(snapshot), 'lexical-digest',
                               writes=(RecordWrite(record, previous.record_version),)))
    assert not fresh.lean_context('末尾记录', n_facts=0, n_digests=1).context
    hit, = fresh.retrieve_digests('short digest').digests
    assert (hit.id, hit.text) == (original.id, 'A short digest.')


def _seed_digest(memory, source, text, *, vector=None):
    with memory.dao.read_snapshot() as view:
        previous = view.get(SOURCE, source.id)
        snapshot = view.snapshot
    record = StoredRecord.from_record(memory.context, replace(domain(previous), digest=text),
                                      record_version=previous.record_version + 1,
                                      digest_version=(previous.digest_version or 0) + 1)
    embeddings = () if vector is None else (EmbeddingWrite(
        record.ref, record.digest_version, memory._model, tuple(vector), 'digest'),)
    memory.dao.commit(ChangeSet(CommitConditions(snapshot), f'digest-{source.id}-{record.digest_version}',
                               writes=(RecordWrite(record, previous.record_version),), embeddings=embeddings))


def test_exact_vector_and_bm25_use_full_scoped_corpus(memory, runtime, spaces, user, accounts):
    old = seed_fact(memory, 'Ana', 'works_at', 'Acme', valid_at=100, invalid_at=200, text='Ana works at Acme')
    current = seed_fact(memory, 'Ana', 'works_at', 'Orbit', valid_at=200, supersedes=[old.id], text='Ana works at Orbit')
    repeat = seed_fact(memory, 'Bo', 'likes', 'Orbit Orbit', valid_at=150, text='Bo likes Orbit Orbit')
    sibling = spaces.authorize(user, spaces.create(user, 'Other space').space_id)
    sibling_memory = Memory(runtime, sibling)
    seed_fact(sibling_memory, 'Ana', 'works_at', 'Orbit Orbit Orbit', valid_at=200, text='Ana works at Orbit Orbit Orbit')
    other = accounts.create_user()
    try:
        unrelated = spaces.authorize(other, spaces.create(other, 'Other user').space_id)
        seed_fact(Memory(runtime, unrelated), 'Ana', 'works_at', 'Orbit', valid_at=200, text='Ana works at Orbit')
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
        assert memory.search('Where does Ana work?').top().object == 'Orbit'
        assert memory.search('Where did Ana work?', facts=FactQuery(state=FactState.HISTORY)).top().object == 'Acme'
        assert 'Other space' not in memory.lean_context('Where does Ana work?').context
        with pytest.raises(SpaceNotFound):
            memory.recall_multi((sibling.space_id, unrelated.space_id), 'Orbit')
    finally:
        accounts.delete_user(other)


def test_graph_name_path_and_effective_date(memory):
    seed_fact(memory, 'Wei', 'colleague', 'Lin', valid_at=100, linked=True, text='Wei colleague Lin')
    job = seed_fact(memory, 'Lin', 'works_at', 'Orbit', valid_at=100, linked=True, text='Lin works at Orbit')
    seed_fact(memory, 'Orbit', 'based_in', 'Lisbon', valid_at=100, linked=True, text='Orbit based in Lisbon')
    orbit, = memory.graph_entities('orbit')
    assert {edge.fact_id for edge in memory.graph_neighbors(orbit.id, direction='in')} == {job.id}
    assert memory.follow_path('Wei', ['colleague', 'works_at']).paths[0].endpoint == 'Orbit'
    assert memory.follow_path('Orbital Lab', ['based_in', 'unknown']).paths == ()
    assert memory.graph_relations(job.id, facts=FactQuery(valid_at=99)) == ()
    assert memory.graph_relations(job.id, facts=FactQuery(valid_at=150))
    original, = memory.graph_relations(job.id)
    with memory.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = %s', (job.id,))
    rebuild_graph(memory.dao)
    rebuilt, = memory.graph_relations(job.id)
    assert rebuilt.id == original.id
    assert memory.graph_entities('Orbit') == (orbit,)
    with memory.dao.read_snapshot() as view:
        first_projection, = view.relations(job.id)
    rebuild_graph(memory.dao)
    with memory.dao.read_snapshot() as view:
        assert view.relations(job.id) == (first_projection,)


def test_graph_retrieval_uses_query_names_and_explicit_subject_scope(memory):
    from mnemotrace.store.postgres.graph_reads import RetrievalGraph

    fact = seed_fact(memory, '我', 'lives_in', 'Synthetic City', linked=True, text='user lives in Synthetic City')
    seed_fact(memory, 'Other Person', 'lives_in', 'Synthetic Harbor', linked=True, text='Other Person lives in Synthetic Harbor')
    with memory.dao.read_snapshot() as view:
        for text in ('Where do I live?', 'Where does his sister live?'):
            assert not RetrievalGraph(view, memory._query(text, top_k=1)).entities
        graph = RetrievalGraph(view, memory._query('Synthetic City', top_k=1))
        assert {entity.name for entity in graph.entities.values()} == {'Synthetic City', 'Synthetic Harbor'}
    assert memory.search('Where do I live?', facts=FactQuery(subject='我')).top().id == fact.id


def test_answer_evidence_matches_offline_without_domain_vocabulary(runtime, spaces, user):
    from mnemotrace import MemoryEngine

    context = spaces.authorize(user, spaces.create(user, 'Answer evidence').space_id)
    offline, persistent = MemoryEngine(), Memory(runtime, context)
    offline.add_fact('Ana', 'preferred_format', 'Synthetic blocks', text='Ana preferred format Synthetic blocks')
    seed_fact(persistent, 'Ana', 'preferred_format', 'Synthetic blocks', text='Ana preferred format Synthetic blocks')
    for engine in (offline, persistent):
        assert engine.search("What is Ana's preferred language?").abstained
        assert engine.search("What is Ana's preferred format?").top().object == 'Synthetic blocks'
        assert engine.search("Tell me about blocks").top().object == 'Synthetic blocks'
        assert engine.search("What is it?").abstained


def test_digest_answer_requires_full_query_coverage(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, 'Digest evidence').space_id)
    memory = Memory(runtime, context)
    original = seed_note(memory, "Ana's favorite programming language is Python.", 'chat')
    with memory.dao.read_snapshot() as view:
        previous = view.get(SOURCE, original.id)
        snapshot = view.snapshot
    digest = replace(domain(previous), digest=original.content)
    record = StoredRecord.from_record(context, digest, content_version=previous.content_version,
                                      record_version=previous.record_version + 1, digest_version=1)
    memory._commit(snapshot, writes=(RecordWrite(record, previous.record_version),))
    assert memory.search("What is Ana's favorite food?").abstained
    result = memory.search("What is Ana's favorite programming language?")
    assert result.via == 'digest' and 'Python' in result.sources[0].text


def test_limited_candidates_can_rank_both_published_facts_in_a_slot(memory):
    # The salience shortlist should find Orbit while the other shortlists find Orion.
    manual = seed_fact(memory, 'Ana', 'works_at', 'Orbit', valid_at=100, text='Ana works at Orbit')
    shadow = Fact('Ana', 'works_at', 'Orion', user_id=memory.context.user_id, valid_at=200,
                  source='extracted', salience=0.5, id='fixture-retrieval-orion', text='Ana works at Orion')
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
    source = seed_note(memory, 'I recently finished a Tamiya 1/48 scale Spitfire Mk.V model kit.', 'chat', event_time=100)
    _seed_digest(memory, source, 'Finished a Spitfire model kit.')
    result = memory.aggregate_facts('bought', query='Spitfire model kit')
    assert result.count == 0 and result.candidates
    assert 'digest_candidates_only' in result.limitations
    hidden = memory.aggregate_facts('bought', query='Spitfire model kit',
                                    facts=FactQuery(exclude_sensitive=True))
    assert not hidden.candidates


def test_aggregate_counts_full_snapshot_and_respects_time_and_redaction(memory):
    seed_fact(memory, 'Ana', 'visited', 'Orbit', valid_at=10, sensitive=False, linked=True, text='Ana visited Orbit')
    seed_fact(memory, 'Bea', 'visited', 'ORBIT', valid_at=20, sensitive=False, linked=True, text='Bea visited ORBIT')
    seed_fact(memory, 'Bea', 'visited', 'Elsewhere', valid_at=30, sensitive=True, text='Bea visited Elsewhere')
    scope = FactQuery(limit=1, exclude_sensitive=True)
    matches = memory.aggregate_facts('visited', start=10, end=30, facts=scope)
    assert matches.count == 2 and len(matches.facts) == 1
    assert memory.aggregate_facts('visited', operation='count_distinct', facts=scope).count == 1
    assert memory.aggregate_facts('visited', start=20, end=30, facts=scope).count == 1


def test_search_returns_snapshot_result_when_access_reinforcement_conflicts(memory, cluster, monkeypatch):
    fact = seed_fact(memory, 'Ana', 'likes', 'Clover', text='Ana likes Clover')
    memory._config.access_boost = 0.5
    commit = memory._commit
    with Database(cluster.runtime) as second:
        concurrent = Memory(second, memory.context)

        def interleaved_commit(*args, **kwargs):
            assert concurrent.search('What does Ana like?').top().object == 'Clover'
            return commit(*args, **kwargs)

        monkeypatch.setattr(memory, '_commit', interleaved_commit)
        assert memory.search('What does Ana like?').top().object == 'Clover'
    assert memory.get_fact(fact.id).access_count == 1


def test_retrieval_ignores_old_text_vectors_after_edit(memory):
    old = seed_fact(memory, 'Ana', 'likes', 'Clover', text='Ana likes Clover')
    with memory.dao.read_snapshot() as view:
        snapshot, stored = view.snapshot, view.get(RecordKind.FACT, old.id)
    edited = replace(old, object='Orion', text='Ana likes Orion')
    write = memory._write(edited, stored, text_changed=True)
    memory._commit(snapshot, writes=(write,), embeddings=memory._embeddings(write, edited.text))
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
    fact = seed_fact(memory, 'Ana', 'likes', 'Clover', text='Ana likes Clover')
    request = memory._query('Clover', top_k=5)
    with memory.dao.read_snapshot() as view:
        corpus = Corpus(view, request, RecordKind.FACT)
        for _ in range(8):
            assert tuple(corpus.vector_scores()) == (fact.id,)
        prepared = view._sql.run("SELECT statement FROM pg_prepared_statements "
                                 "WHERE position('scoped AS MATERIALIZED' in statement) > 0").fetchall()
        assert prepared == []


def test_digest_lexical_projection_follows_text_version(memory):
    source = seed_note(memory, 'Synthetic material', 'chat')
    for version, text in enumerate(('Clover', 'Snowdrop'), 1):
        with memory.dao.read_snapshot() as view:
            previous = view.get(SOURCE, source.id)
            snapshot = view.snapshot
        updated = replace(domain(previous), digest=text)
        record = StoredRecord.from_record(memory.context, updated, digest_version=version,
                                          record_version=previous.record_version + 1)
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), f'digest-test-{version}',
                                    writes=(RecordWrite(record, previous.record_version),)))
        result = memory.retrieve_digests(text)
        assert [(ep.id, ep.text, ep.derived) for ep in result.digests] == [(source.id, text, True)]
    assert memory.retrieve_digests('Clover').digests == ()


def test_historical_retrieval_uses_latest_digest_for_eligible_event(memory, runtime):
    import json

    from mnemotrace import MemoryEngine
    from mnemotrace.service import MemoryService

    source = seed_note(memory, 'Project Orchid original wording', 'chat', event_time=100)
    cutoffs = []
    for version, text in enumerate(('Project Orchid first digest', 'Project Orchid later detail'), 1):
        with memory.dao.read_snapshot() as view:
            previous = view.get(SOURCE, source.id)
            snapshot = view.snapshot
            cutoffs.append(view.at.timestamp())
        record = StoredRecord.from_record(memory.context, replace(domain(previous), digest=text),
                                          digest_version=version, record_version=previous.record_version + 1)
        memory.dao.commit(ChangeSet(CommitConditions(snapshot), f'historical-digest-{version}',
                                    writes=(RecordWrite(record, previous.record_version),)))

    assert not memory.retrieve_digests('Orchid', as_of=99).digests
    current, = memory.retrieve_digests('Orchid').digests
    assert current.text == 'Project Orchid later detail' and current.derived
    with memory.dao.read_snapshot() as view:
        after_digest = view.at.timestamp()
    historical, = memory.retrieve_digests('Orchid', as_of=100).digests
    assert historical == current

    snapshot = json.loads(''.join(MemoryService(runtime).iter_export(memory.context, include_sensitive=True)))
    written_at = snapshot['episodes'][0]['digest_written_at']
    assert cutoffs[-1] <= written_at <= after_digest
    restored = MemoryEngine()
    restored.import_snapshot(snapshot)
    assert not restored.retrieve_digests('Orchid', as_of=99)
    assert restored.retrieve_digests('Orchid', as_of=100)[0].text == current.text

    with memory.dao._transaction() as sql:
        sql.update('source_digests', {'digest_written_at': None}, 'source_id = %s', (source.id,))
    assert memory.retrieve_digests('Orchid', as_of=100).digests == (current,)
    assert memory.retrieve_digests('Orchid').digests == (current,)


def test_raw_writes_do_not_project_and_digest_vectors_can_recall(memory, runtime):
    from mnemotrace.store.contracts import MessageInput

    class SynonymEmbedder(MockEmbedder):
        def embed(self, text):
            assert text not in {'Celestial star', 'Unprocessed dialogue', 'Unprocessed note'}
            return [1.0, 0.0]

    memory = Memory(runtime, memory.context, embedder=SynonymEmbedder(2), embedding_model='synthetic')
    source = seed_note(memory, 'Celestial star', 'chat')
    memory.remember('Unprocessed note', 'chat', extract=False)
    memory.remember_messages('chat', (MessageInput('raw-message', 'user', 'Unprocessed dialogue', extract=False),))
    with memory.dao.read_snapshot() as view:
        for table in ('embeddings', 'lexical_documents', 'operations'):
            assert not view._sql.run(f'SELECT 1 FROM mnemotrace.{table} WHERE user_id=%s AND space_id=%s',
                                     view._sql.scope).fetchall()
    assert not memory.retrieve_digests('Daybreak').digests
    assert not memory.lean_context('Daybreak').context
    assert memory.get_episode(source.id).content == source.content

    _seed_digest(memory, source, 'Condensed evidence', vector=(1.0, 0.0))
    result = memory.retrieve_digests('Daybreak')
    assert result.complete
    assert [(item.id, item.text) for item in result.digests] == [(source.id, 'Condensed evidence')]
    context = memory.lean_context('Daybreak', n_facts=0, n_digests=1)
    assert 'Condensed evidence' in context.context and source.content not in context.context


def test_rebuild_lexical_index_restores_missing_digest(memory):
    source = seed_note(memory, 'An archival source', 'chat')
    _seed_digest(memory, source, 'Snowdrop')
    with memory.dao._transaction() as sql:
        sql.delete('lexical_documents', 'source_id = %s', (source.id,))
    assert 'lexical_backfill_pending' in memory.retrieve_digests('Snowdrop').limitations
    assert rebuild_lexical_index(memory.dao, RecordKind.SOURCE, limit=1) == source.id
    assert rebuild_lexical_index(memory.dao, RecordKind.SOURCE, after_id=source.id, limit=1) is None
    assert rebuild_lexical_index(memory.dao, RecordKind.SOURCE, limit=1) == source.id
    assert memory.retrieve_digests('Snowdrop').digests[0].id == source.id


def test_rebuild_lexical_index_refreshes_fact_with_stale_analyzer(memory):
    fact = seed_fact(memory, 'Synthetic gardener', 'grows', 'Snowdrop',
                     text='Synthetic gardener grows Snowdrop.')
    with memory.dao._transaction() as sql:
        sql.update('lexical_documents', {'analyzer_version': 'obsolete'}, 'fact_id = %s', (fact.id,))
    query = memory._query('Snowdrop', top_k=1, use_vectors=False)
    with memory.dao.read_snapshot() as view:
        assert 'lexical_backfill_pending' in view.candidates(query).limitations
    assert rebuild_lexical_index(memory.dao, RecordKind.FACT) is None
    with memory.dao.read_snapshot() as view:
        batch = view.candidates(query)
        assert 'lexical_backfill_pending' not in batch.limitations
        assert batch.candidates[0].record.ref.record_id == fact.id
        assert batch.candidates[0].lexical > 0


def test_snapshot_keeps_all_signals_at_one_version_and_failure_stays_visible(memory, cluster):
    seed_fact(memory, 'Ana', 'likes', 'Clover', text='Ana likes Clover')
    request = memory._query('Clover', top_k=5)
    with Database(cluster.runtime) as second:
        writer = Memory(second, memory.context)
        with memory.dao.read_snapshot() as view:
            before = view.candidates(request)
            seed_fact(writer, 'Bo', 'likes', 'Clover', text='Bo likes Clover')
            assert view.candidates(request) == before
        assert len(memory.search('Clover').facts) == 2
        second.close()
        with pytest.raises(DatabaseUnavailable):
            writer.search('Clover')


def test_http_structured_queries_use_authorized_space_and_redaction(product_client, product_scope, product_service):
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
        seed_fact(product_service.memory(context), subject, predicate, object_,
                  text=f'{subject} {predicate} {object_}', sensitive=sensitive, linked=True)
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


def test_explicit_path_respects_visible_facts(memory):
    seed_fact(memory, 'Ana', 'sister', 'Cora', sensitive=False, linked=True, text='Ana sister Cora')
    seed_fact(memory, 'Bea', 'sister', 'Cora', sensitive=True, linked=True, text='Bea sister Cora')
    seed_fact(memory, 'Cora', 'works_at', 'Orbit', sensitive=False, linked=True, text='Cora works at Orbit')
    scope = FactQuery(exclude_sensitive=True)
    assert not memory.follow_path('Bea', ['sister', 'works_at'], facts=scope).paths
    result = memory.follow_path('ANA', ['sister', 'works_at'], facts=scope)
    assert result.paths[0].endpoint == 'Orbit'
    assert result.paths[0].facts[0].subject == 'Ana'


def test_valid_time_applies_to_aggregate_and_search_after_restart(memory, cluster):
    first = seed_fact(memory, 'Ana', 'works_at', 'First', valid_at=100, invalid_at=200, text='Ana works at First')
    second = seed_fact(memory, 'Ana', 'works_at', 'Second', valid_at=200, supersedes=[first.id], text='Ana works at Second')
    with Database(cluster.runtime) as reopened:
        fresh = Memory(reopened, memory.context)
        for at, fact in [(199, first), (200, second)]:
            scope = FactQuery(valid_at=at, subject='Ana', predicate='works_at')
            assert fresh.aggregate_facts('works_at', facts=scope).facts[0].id == fact.id
            assert fresh.search('Where does Ana work?', facts=scope).top().id == fact.id


def test_explicit_slot_checks_full_view_and_preserves_literal_case(memory):
    bob = seed_fact(memory, "Bob", "favorite_language", "Ab1", sensitive=False, text='Bob favorite language Ab1')
    assert memory.search("Alice favorite language").status == "evidence_only"
    slot = FactQuery(subject="Bob", predicate="favorite_language", exclude_sensitive=True)
    assert memory.search("language", facts=slot).answer() == "Ab1"
    seed_fact(memory, "Bob", "favorite_language", "ab1", sensitive=False, text='Bob favorite language ab1')
    result = memory.search("language", facts=slot, top_k=1)
    assert result.status == "evidence_only" and len(result.facts) == 1
    assert memory.aggregate_facts("favorite_language", operation="count_distinct", facts=slot).count == 2
    assert bob.id in {fact.id for fact in memory.facts(slot)}


def test_focus_boundaries_match_offline_and_persistent_reads(memory):
    from mnemotrace.memory_engine import MemoryEngine

    offline = MemoryEngine()
    for language, labels in [("Java", ["technology"]), ("JavaScript", [])]:
        text = f"user uses {language}"
        offline.add_fact("我", "uses", language, categories=labels, sensitive=False, text=text)
        seed_fact(memory, "我", "uses", language, categories=labels, sensitive=False, text=text)
    for terms in (["java"], ["category:technology"]):
        offline.set_focus(mute=terms)
        memory.set_settings(mute=tuple(terms))
        for backend in (offline, memory):
            result = backend.search("uses JavaScript")
            assert [fact.object for fact in result.facts] == ["JavaScript"]
    offline.set_focus(mute=[])
    memory.set_settings(mute=())
    for backend in (offline, memory):
        assert {fact.object for fact in backend.search("uses JavaScript").facts} == {"Java", "JavaScript"}


def test_explicit_slot_preserves_accepted_subject_whitespace(memory):
    fact = seed_fact(memory, " Alice ", "works_at", "Synthetic Lab", text=' Alice  works at Synthetic Lab')
    result = memory.search("work", facts=FactQuery(subject=" Alice ", predicate="works_at"))
    assert result.status == "answered" and result.top().id == fact.id


def test_explicit_slot_reads_existing_facts_during_embedding_failure(memory, monkeypatch):
    seed_fact(memory, 'Ana', 'works_at', 'Orbit', text='Ana works at Orbit.')

    def unavailable(_):
        raise RuntimeError('Synthetic embedding outage')

    monkeypatch.setattr(memory._embedder, 'embed', unavailable)
    assert memory.search('Where does Ana work?', facts=FactQuery(subject='Ana', predicate='works_at')).answer() == 'Orbit'
    with pytest.raises(RuntimeError, match='Synthetic embedding outage'):
        memory.search('Where does Ana work?')
