"""Explicit fact sentences share display/retrieval storage and atomic updates."""
from dataclasses import asdict

import pytest

from mnemotrace import Memory
from mnemotrace.store.contracts import RecordKind
from tests.embedder_fixtures import product_embedder
from tests.postgres.test_http_product import hdr


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


def test_text_versions_graph_and_metadata_use_the_same_assertion(text_memory, monkeypatch):
    memory = text_memory
    fact = memory.add_fact('user', 'likes', '茶', text='用户喜欢茶。', object_kind='entity', sensitive=False)
    before, vectors = stored(memory, fact.id)
    sentence = fact.text
    original_embed = memory._embedder.embed
    monkeypatch.setattr(memory._embedder, 'embed', lambda text: pytest.fail('Unchanged sentence was embedded'))
    memory.update_fact(fact.id, categories=['food'])
    memory.update_fact(fact.id, subject='user', predicate='likes', object='茶', text=sentence)
    memory.update_fact(fact.id, subject='user', predicate='prefers', object='茶', text=sentence)
    after, current_vectors = stored(memory, fact.id)
    assert before.content_version == after.content_version and vectors == current_vectors
    assert memory.graph_relations(fact.id)[0].predicate == 'prefers'
    monkeypatch.setattr(memory._embedder, 'embed', original_embed)
    memory.update_fact(fact.id, subject='user', predicate='prefers', object='茶', text='用户在下午偏好茶。')
    updated, updated_vectors = stored(memory, fact.id)
    assert updated.content_version == after.content_version + 1
    assert updated_vectors != vectors
    assert memory.get_fact(fact.id).text == '用户在下午偏好茶。'


def test_embedding_failure_does_not_commit_any_part_of_content_edit(text_memory, monkeypatch):
    memory = text_memory
    fact = memory.add_fact('user', 'likes', '茶', text='用户喜欢茶。', object_kind='entity')
    before, vectors = stored(memory, fact.id)
    graph = memory.graph_relations(fact.id)

    def fail(text):
        raise RuntimeError('Synthetic embedding failure')

    monkeypatch.setattr(memory._embedder, 'embed', fail)
    with pytest.raises(RuntimeError, match='Synthetic embedding failure'):
        memory.update_fact(fact.id, subject='user', predicate='likes', object='咖啡', text='用户喜欢咖啡。')
    assert stored(memory, fact.id) == (before, vectors)
    assert memory.graph_relations(fact.id) == graph


def test_http_sentence_is_returned_without_display_and_invalid_patch_leaves_it_unchanged(product_client):
    client = product_client
    headers = hdr('synthetic-text-contract')
    sentence = '用户在工作日上午偏好茶，而不是咖啡。'
    created = client.post('/v1/facts', headers=headers, json={
        'predicate': 'prefers', 'object': '茶', 'text': sentence, 'sensitive': False,
    })
    assert created.status_code == 200
    assert created.json()['text'] == sentence and 'display' not in created.json()
    fact_id = created.json()['id']
    before = client.get('/v1/memories', headers=headers).json()['facts']
    for patch in ({'object': '咖啡'}, {'text': '新句子'}, {'text': None}, {'display': sentence}, {'statement': sentence}):
        assert client.patch(f'/v1/facts/{fact_id}', headers=headers, json=patch).status_code == 422
    assert client.get('/v1/memories', headers=headers).json()['facts'] == before
    exported = client.get('/v1/export', headers=headers).json()
    assert exported['format_version'] == 3
    assert exported['facts'][0]['text'] == sentence and 'display' not in exported['facts'][0]


def test_new_database_has_required_sentence_and_no_display_column(text_memory):
    with text_memory.dao.read_snapshot() as view:
        columns = view._sql.run("SELECT column_name,is_nullable FROM information_schema.columns "
                                "WHERE table_schema='mnemotrace' AND table_name='facts'").fetchall()
    assert ('text', 'NO') in columns and not any(name == 'display' for name, _ in columns)
    fact = text_memory.add_fact('user', 'likes', '茶', text='用户喜欢茶。')
    assert 'display' not in asdict(fact)


def test_http_manual_assertion_retry_reuses_fact(product_client):
    headers = hdr('synthetic-manual-retry')
    body = {'predicate': 'likes', 'object': 'Synthetic tea', 'text': 'User likes synthetic tea.'}
    created = product_client.post('/v1/facts', headers=headers, json=body)
    replayed = product_client.post('/v1/facts', headers=headers, json=body)
    assert created.is_success and replayed.is_success
    assert replayed.json()['id'] == created.json()['id']
