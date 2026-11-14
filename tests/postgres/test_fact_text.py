"""Explicit fact sentences share display/retrieval storage and atomic updates."""
from dataclasses import asdict

import pytest

from mnemotrace import Memory
from mnemotrace.store.contracts import RecordKind
from tests.embedder_fixtures import product_embedder
from tests.postgres.test_http_product import hdr
from tests.postgres.fact_fixtures import seed_fact


@pytest.fixture
def text_memory(runtime, spaces, user):
    space = spaces.create(user, 'Explicit fact text')
    return Memory(runtime, spaces.authorize(user, space.space_id),
                  embedder=product_embedder(), embedding_model='synthetic-fact-text')


def stored(memory, fact_id):
    with memory.dao.read_snapshot() as view:
        record = view.get(RecordKind.FACT, fact_id)
        vector = view._sql.run('SELECT content_version,vector::text FROM mnemotrace.embeddings '
                              'WHERE user_id=%s AND space_id=%s AND fact_id=%s',
                              (memory.context.user_id, memory.context.space_id, fact_id)).fetchall()
    return record, vector


def test_sensitivity_does_not_reembed_or_change_graph_projection(text_memory, monkeypatch):
    memory = text_memory
    fact = seed_fact(memory, '我', 'likes', '茶', text='用户喜欢茶。', linked=True)
    before, vectors = stored(memory, fact.id)
    graph = memory.graph_relations(fact.id)
    monkeypatch.setattr(memory._embedder, 'embed', lambda text: pytest.fail('Unchanged sentence was embedded'))
    memory.set_fact_sensitive(fact.id, True)
    after, current_vectors = stored(memory, fact.id)
    assert after.content_version == before.content_version and vectors == current_vectors
    assert after.record_version == before.record_version + 1
    assert memory.graph_relations(fact.id) == graph
    assert memory.get_fact(fact.id).text == fact.text


def test_embedding_failure_does_not_commit_an_incomplete_new_fact(text_memory, monkeypatch):
    memory = text_memory
    fact = seed_fact(memory, '我', 'likes', '茶', text='用户喜欢茶。', linked=True)
    before, vectors = stored(memory, fact.id)
    graph = memory.graph_relations(fact.id)

    def fail(text):
        raise RuntimeError('Synthetic embedding failure')

    monkeypatch.setattr(memory._embedder, 'embed', fail)
    with pytest.raises(RuntimeError, match='Synthetic embedding failure'):
        seed_fact(memory, '我', 'likes', '咖啡', text='用户喜欢咖啡。', linked=True)
    assert stored(memory, fact.id) == (before, vectors)
    assert memory.graph_relations(fact.id) == graph
    assert len(memory.facts()) == 1


def test_http_sentence_is_returned_without_display_and_invalid_patch_leaves_it_unchanged(product_client):
    client = product_client
    headers = hdr('synthetic-text-contract')
    sentence = '用户在工作日上午偏好茶，而不是咖啡。'
    fact_id = client.seed_fact(headers, predicate='prefers', object='茶', text=sentence).id
    before = client.get('/v1/memories', headers=headers).json()['facts']
    for patch in ({'object': '咖啡'}, {'text': '新句子'}, {'text': None}, {'display': sentence}, {'statement': sentence}):
        assert client.patch(f'/v1/facts/{fact_id}/sensitivity', headers=headers, json={'sensitive': True, **patch}).status_code == 422
    assert client.get('/v1/memories', headers=headers).json()['facts'] == before
    exported = client.get('/v1/export', headers=headers).json()
    assert exported['format_version'] == 6
    assert exported['facts'][0]['text'] == sentence and 'display' not in exported['facts'][0]


def test_new_database_has_required_sentence_and_no_display_column(text_memory):
    with text_memory.dao.read_snapshot() as view:
        columns = view._sql.run("SELECT column_name,is_nullable FROM information_schema.columns "
                                "WHERE table_schema='mnemotrace' AND table_name='facts'").fetchall()
    assert ('text', 'NO') in columns and not any(name == 'display' for name, _ in columns)
    fact = seed_fact(text_memory, '我', 'likes', '茶', text='用户喜欢茶。')
    assert 'display' not in asdict(fact)
