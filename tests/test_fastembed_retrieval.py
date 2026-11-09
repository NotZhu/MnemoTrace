import os
from time import perf_counter

import pytest

from mnemotrace import Config, MemoryEngine
from mnemotrace.embed import EmbeddingUnavailable, FastEmbedEmbedder
from mnemotrace.util import cosine


def test_configured_embedding_load_failure_is_reported_as_unready(monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from mnemotrace.server import app

    def unavailable():
        raise EmbeddingUnavailable('Synthetic missing model cache')

    monkeypatch.setattr(app, '_svc', None)
    monkeypatch.setattr(app.MemoryService, 'from_env', unavailable)
    with TestClient(app.app) as client:
        response = client.get('/ready')
        assert response.status_code == 503
        assert response.json()['detail']['code'] == 'embedding_unavailable'


def test_real_fastembed_with_preloaded_model(tmp_path):
    cache = os.environ.get('FASTEMBED_TEST_CACHE')
    if not cache:
        pytest.skip('Set FASTEMBED_TEST_CACHE to the preloaded pinned model cache')
    pytest.importorskip('fastembed')
    started = perf_counter()
    embedder = FastEmbedEmbedder(cache_dir=cache, local_files_only=True)
    cold_ms = (perf_counter() - started) * 1000
    assert embedder.dim == 384 and embedder.embed_batch([]) == []

    corpus = ['The automobile is parked in the garage.', '我每天骑自行车上班。',
              'The meeting is scheduled for Friday.', '绿茶的冲泡温度不宜太高。',
              'The computer needs a new battery.', '河岸长着一排柳树。',
              'The wedding is next month.', '他养了一只橘猫。']
    queries = [('Where is the car?', 0), ('I commute to work by bicycle.', 1), ('红茶茶具', 3)]
    observations = {}
    for name, backend in (('lexical', None), ('hybrid', embedder)):
        memory = MemoryEngine(embedder=backend)
        ids = [memory.add(text).id for text in corpus]
        tail = memory.add('无关填充。' * 400 + '末尾关键字玄鹤')
        timings = []
        matches = []
        for query, index in queries:
            started = perf_counter()
            hits = memory.retrieve_episodes(query, k=5)
            timings.append((perf_counter() - started) * 1000)
            matches.append(ids[index] in {item.id for item in hits})
        assert tail.id in {item.id for item in memory.retrieve_episodes('玄鹤', k=5)}
        for _ in range(9):
            for query, _ in queries:
                started = perf_counter()
                memory.retrieve_episodes(query, k=5)
                timings.append((perf_counter() - started) * 1000)
        timings.sort()
        observations[name] = (matches, round(timings[14], 2), round(timings[28], 2))
    assert observations['hybrid'][0][:2] == [True, True]
    assert sum(observations['hybrid'][0]) >= sum(observations['lexical'][0])
    print(f'FastEmbed load_ms={cold_ms:.2f}, (hits@5, p50_ms, p95_ms)={observations}')

    threshold = Config().conflict_sim_threshold
    for statements in (
        ('Ana works at Skylark Labs.', 'Ana is employed by Skylark Labs.', 'Ana lives in Toronto.'),
        ('陈晨在青岚公司任职', '陈晨的雇主是青岚公司', '陈晨住在北京'),
    ):
        source, same_slot, other_slot = embedder.embed_batch(statements)
        assert cosine(source, same_slot) >= threshold > cosine(source, other_slot)

    with pytest.raises(EmbeddingUnavailable, match='model cache is unavailable'):
        FastEmbedEmbedder(cache_dir=tmp_path, local_files_only=True)
