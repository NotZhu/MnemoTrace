"""Optional backend contracts, run against local mode and a real CI Qdrant service."""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest

pytest.importorskip("qdrant_client")
from qdrant_client import QdrantClient, models

from mnemotrace import Config
from tests.model_fixtures import Memory, MemoryService
from mnemotrace.embed import HashingEmbedder
from mnemotrace.store import DimensionMismatchError, PersistenceError, StoreFormatError
from mnemotrace.store.memory_store import InMemoryVectorStore
from mnemotrace.store.qdrant_store import QdrantVectorStore, _point_id
from mnemotrace.types import Episode, Fact
from mnemotrace.util import DAY

# Capture before the suite's per-test environment isolation clears MNEMOTRACE_* .
QDRANT_TEST_URL = os.environ.get("MNEMOTRACE_TEST_QDRANT_URL")


@pytest.fixture(params=["local", "remote"])
def mode(request):
    if request.param == "remote" and not QDRANT_TEST_URL:
        pytest.skip("set MNEMOTRACE_TEST_QDRANT_URL for real remote integration")
    return request.param


@pytest.fixture
def store(mode):
    client = (QdrantClient(":memory:") if mode == "local" else QdrantClient(
        url=QDRANT_TEST_URL, timeout=5,
    ))
    store = QdrantVectorStore(client, "test_" + uuid.uuid4().hex, 2)
    try:
        yield store
    finally:
        store.drop()
        client.close()


@pytest.fixture
def config(mode):
    cfg = Config(storage="qdrant", embed_dim=8,
                 qdrant_collection_prefix="test_" + uuid.uuid4().hex)
    if mode == "remote":
        cfg.qdrant_url = QDRANT_TEST_URL
    return cfg


@pytest.fixture
def service_factory(config, monkeypatch, tmp_path):
    monkeypatch.setenv("MNEMOTRACE_STORAGE", "qdrant")
    monkeypatch.setenv("MNEMOTRACE_QDRANT_COLLECTION_PREFIX", config.qdrant_collection_prefix)
    monkeypatch.delenv("MNEMOTRACE_QDRANT_API_KEY", raising=False)
    if config.qdrant_url:
        monkeypatch.setenv("MNEMOTRACE_QDRANT_URL", config.qdrant_url)
    else:
        monkeypatch.delenv("MNEMOTRACE_QDRANT_URL", raising=False)
    services = []

    def create(**kwargs):
        svc = MemoryService(data_dir=str(tmp_path / "service"), embedder_name="hashing",
                            llm_name="", **kwargs)
        services.append(svc)
        return svc

    yield create
    for svc in services:
        svc.close()
    # Remote fixtures erase only namespaces they created, even after a test failure.
    if config.qdrant_url:
        for user in ("alice", "bob", "u"):
            cleanup = create()
            cleanup.forget(user)
            cleanup.close()


def test_crud_types_and_arbitrary_keys(store):
    assert store.get("missing") is None and store.values() == []
    store.delete("missing")
    key = "中文 key/'\\特殊"
    fact = Fact(subject="Synthetic", predicate="likes", object="tea", embedding=[1.0, 0.0])
    store.upsert(key, fact.embedding, fact)
    assert asdict(store.get(key)) == asdict(fact)
    fact.object = "coffee"
    store.upsert(key, fact.embedding, fact)
    assert len(store.values()) == 1 and store.get(key).object == "coffee"
    store.delete(key)
    assert store.get(key) is None and store.values() == []


def test_filter_scans_all_pages_before_topk(store):
    reference = InMemoryVectorStore()
    for i in range(260):
        vector = [1.0, 0.0] if i < 259 else [0.0, 1.0]
        payload = {"user": "other" if i < 259 else "target", "i": i}
        store.upsert(str(i), vector, payload)
        reference.upsert(str(i), vector, payload)
    where = lambda row: row["user"] == "target"
    assert len(store.values()) == 260
    assert store.search([1.0, 0.0], 1, where) == reference.search([1.0, 0.0], 1, where)
    assert store.search([1.0, 0.0], 1, lambda row: False) == []


def test_cosine_uses_original_non_normalized_vectors(store):
    reference = InMemoryVectorStore()
    for key, vector in [("best", [8.0, 1.0]), ("middle", [1.0, 3.0]), ("last", [-2.0, 0.0])]:
        store.upsert(key, vector, key)
        reference.upsert(key, vector, key)
    actual = store.search([3.0, 0.0], 3)
    expected = reference.search([3.0, 0.0], 3)
    assert [p for _, p in actual] == [p for _, p in expected]
    assert [s for s, _ in actual] == pytest.approx([s for s, _ in expected])


def test_empty_and_zero_vectors_are_retained_and_beat_negative_scores(store):
    store.upsert("negative", [-1.0, 0.0], "negative")
    store.upsert("empty", [], "empty")
    store.upsert("zero", [0.0, 0.0], "zero")
    hits = store.search([1.0, 0.0], 3)
    assert {payload for _, payload in hits[:2]} == {"empty", "zero"}
    assert hits[-1] == (-1.0, "negative")
    assert {p for _, p in store.search([], 10)} == {"empty", "zero", "negative"}
    assert all(s == 0 for s, _ in store.search([0.0, 0.0], 10))
    assert store.search([1.0, 0.0], 0) == []


def test_bad_vectors_and_payloads_fail_before_mutation(store):
    for vector in ([float("nan"), 0.0], [True, 0.0]):
        with pytest.raises(ValueError):
            store.upsert("bad", vector, {})
    with pytest.raises(DimensionMismatchError):
        store.upsert("bad", [1.0], {})
    assert store.values() == []
    fact = Fact(subject="Synthetic", predicate="likes", object="tea", embedding=[1.0, 0.0])
    store.upsert(fact.id, fact.embedding, fact)
    raw = asdict(fact)
    raw.pop("id")
    store.client.set_payload(store.collection, payload={"data": raw},
                             points=[_point_id(fact.id)], wait=True)
    with pytest.raises(StoreFormatError, match="id"):
        store.get(fact.id)


def test_existing_collection_schema_is_never_recreated_implicitly(store):
    store.upsert("keep", [1.0, 0.0], "keep")
    mismatch = QdrantVectorStore(store.client, store.collection, 3)
    with pytest.raises(DimensionMismatchError):
        mismatch.upsert("bad", [1.0, 0.0, 0.0], "bad")
    assert store.get("keep") == "keep"
    store.drop()
    store.client.create_collection(store.collection,
        vectors_config=models.VectorParams(size=2, distance=models.Distance.DOT))
    with pytest.raises(StoreFormatError, match="cosine"):
        store.values()


def test_restart_history_reinforcement_and_episode_state(config, tmp_path):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        memory.add("My name is Finn and I work at IBM.", user_id="u", event_time=1)
        memory.add("I now work at Oracle.", user_id="u", event_time=2)
        memory.consolidate()
        result = memory.search("Where does Finn work?", user_id="u")
        assert result.answer() == "Oracle"
        assert memory.fact_store.get(result.top().id).access_count == 1
        memory.summarize_episodes(memory.episodes_doc.values())
        assert all(ep.consolidated and ep.summary for ep in memory.episodes_vec.values())
        memory.save()
    assert config.data_path is None
    with Memory.open(root, config=config) as reopened:
        history = reopened.history("Finn", "works_at", user_id="u")
        assert {f.object for f in history} == {"IBM", "Oracle"}
        assert [f.object for f in history if f.is_live()] == ["Oracle"]
        assert reopened.fact_store.get(result.top().id).access_count == 1
        assert all(ep.summary for ep in reopened.summary_vec.values())
        if config.qdrant_url:
            reopened._qdrant_backend.erase()


def test_decay_focus_classification_and_cold_fact_writeback(config, tmp_path):
    with Memory.open(str(tmp_path / "snapshot"), config=config) as memory:
        old = memory.add_fact("user", "visited", "SyntheticCity", user_id="u", valid_at=1)
        memory.add_fact("user", "visited", "AnotherCity", user_id="u", valid_at=1 + 100 * DAY)
        memory.engine._decay_sweep()
        assert memory.fact_store.get(old.id).salience == pytest.approx(0.3)
        memory.focus["track"] = ["SyntheticCity"]
        memory.apply_focus()
        assert memory.fact_store.get(old.id).salience == pytest.approx(0.8)
        memory.evict_cold(1)
        assert memory.cold_store.get(old.id) is not None
        memory._reinforce_fact(memory.cold_store.get(old.id))
        assert memory.cold_store.get(old.id).access_count == 1
        assert memory.fact_store.get(old.id) is None
        if config.qdrant_url:
            memory._qdrant_backend.erase()


def test_invalid_snapshot_leaves_existing_indexes_and_releases_client(config, tmp_path):
    root = tmp_path / "snapshot"
    with Memory.open(str(root), config=config) as memory:
        fact = memory.add_fact("user", "likes", "tea", user_id="u")
        memory.save()
    manifest = root / "manifest.json"
    original = manifest.read_text(encoding="utf-8")
    invalid = json.loads(original)
    invalid["state"]["import_seen"] = 42
    manifest.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(StoreFormatError):
        Memory.open(str(root), config=config)
    manifest.write_text(original, encoding="utf-8")
    with Memory.open(str(root), config=config) as reopened:
        assert reopened.fact_store.get(fact.id).object == "tea"
        if config.qdrant_url:
            reopened._qdrant_backend.erase()


def test_explicit_reembed_changes_collection_dimensions(config, tmp_path):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        fact = memory.add_fact("user", "likes", "tea", user_id="u")
        memory.save()
    with pytest.raises(DimensionMismatchError):
        Memory.open(root, config=config, embedder=HashingEmbedder(16))
    with Memory.open(root, config=config, embedder=HashingEmbedder(16), allow_mismatch=True) as memory:
        memory.reembed()
        assert len(memory.fact_store.get(fact.id).embedding) == 16
        memory.save()
    with Memory.open(root, config=config, embedder=HashingEmbedder(16)) as memory:
        assert len(memory.fact_store.get(fact.id).embedding) == 16
        if config.qdrant_url:
            memory._qdrant_backend.erase()


def test_service_lru_isolation_erase_and_close(service_factory):
    svc = service_factory(max_hot_users=1)
    svc.add_fact("alice", "user", "likes", "apples")
    first = svc.get("alice")
    svc.add_fact("bob", "user", "likes", "bananas")
    assert first._qdrant_backend._closed
    assert svc.memories("alice")["facts"][0]["object"] == "apples"
    assert svc.forget("alice")["ok"]
    assert svc.memories("alice")["facts"] == []
    assert svc.memories("bob")["facts"][0]["object"] == "bananas"
    last = svc.get("bob")
    svc.close()
    svc.close()
    assert last._qdrant_backend._closed


def test_service_eviction_does_not_close_active_requests(service_factory):
    svc = service_factory(max_hot_users=1)
    svc.add_fact("alice", "user", "likes", "apples")
    entered = threading.Event()
    release = threading.Event()

    def hold_reader():
        with svc._read_scope("alice") as memory:
            entered.set()
            assert release.wait(10)
            assert not memory._qdrant_backend._closed
            assert memory.fact_store.values()[0].object == "apples"

    with ThreadPoolExecutor(max_workers=1) as pool:
        reader = pool.submit(hold_reader)
        try:
            assert entered.wait(10)
            svc.add_fact("bob", "user", "likes", "bananas")
        finally:
            release.set()
        reader.result(timeout=10)


def test_failed_erase_preserves_snapshot_and_can_retry(service_factory, monkeypatch):
    svc = service_factory()
    svc.add_fact("alice", "user", "likes", "apples")
    from pathlib import Path
    snapshot = Path(svc._path("alice")) / "manifest.json"
    before = snapshot.read_bytes()
    original = QdrantVectorStore.drop

    def unavailable(self):
        raise ConnectionError("synthetic Qdrant outage")

    monkeypatch.setattr(QdrantVectorStore, "drop", unavailable)
    with pytest.raises(ConnectionError):
        svc.forget("alice")
    assert snapshot.read_bytes() == before
    monkeypatch.setattr(QdrantVectorStore, "drop", original)
    assert svc.forget("alice")["ok"]
    assert svc.memories("alice")["facts"] == []


def test_failed_write_discards_client_and_restores_committed_state(service_factory, monkeypatch):
    svc = service_factory()
    svc.add_fact("alice", "user", "likes", "apples")
    memory = svc.get("alice")
    original = memory.fact_store.client.upsert

    def unavailable(*args, **kwargs):
        raise ConnectionError("synthetic write failure")

    monkeypatch.setattr(memory.fact_store.client, "upsert", unavailable)
    with pytest.raises(PersistenceError):
        svc.add_fact("alice", "user", "likes", "coffee")
    assert memory._qdrant_backend._closed
    monkeypatch.setattr(memory.fact_store.client, "upsert", original)
    assert {f["object"] for f in svc.memories("alice")["facts"]} == {"apples"}



def test_deployment_prefixes_keep_collections_independent(config, tmp_path):
    from dataclasses import replace
    # Same namespace and endpoint, different deployment prefixes.
    other = replace(config, qdrant_collection_prefix=config.qdrant_collection_prefix + "_other")
    with Memory.open(str(tmp_path / "a"), config=config) as a:
        a.add_fact("user", "likes", "apples", user_id="u")
        a.save()
    with Memory.open(str(tmp_path / "a"), config=other) as b:
        b.fact_store.upsert("marker", [1.0] * 8, {"other": True})
        if config.qdrant_url:
            b._qdrant_backend.erase()
    with Memory.open(str(tmp_path / "a"), config=config) as a:
        assert [f.object for f in a.fact_store.values()] == ["apples"]
        if config.qdrant_url:
            a._qdrant_backend.erase()


def test_local_path_requires_release_before_reopening(tmp_path):
    root = str(tmp_path / "snapshot")
    first = Memory.open(root, config=Config(storage="qdrant"))
    try:
        with pytest.raises(RuntimeError, match="already accessed"):
            Memory.open(root, config=Config(storage="qdrant"))
    finally:
        first.close()
    with Memory.open(root, config=Config(storage="qdrant")):
        pass


def test_async_worker_is_drained_before_service_releases_clients(service_factory, monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_ASYNC_CONSOLIDATION", "1")
    svc = service_factory()
    svc.remember("u", "I like Python.", scope="long")
    svc.close()
    reopened = service_factory()
    assert any(f["object"] == "Python" for f in reopened.memories("u")["facts"])


@pytest.mark.asyncio
async def test_mcp_uses_qdrant_service_and_worker_threads(service_factory):
    from mnemotrace.mcp.backends import LocalBackend
    svc = service_factory()
    backend = LocalBackend(namespace="u", service=svc)
    await backend.remember("I like Python.", scope="long")
    assert (await backend.stats())["storage"] == "qdrant"
    assert (await backend.memories())["facts"]
    assert (await backend.forget())["ok"]


def test_http_config_and_namespace_isolation(service_factory, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc = service_factory()
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    monkeypatch.setattr(appmod, "_svc", svc)
    with TestClient(appmod.app) as client:
        assert client.get("/health").json()["storage"] == "qdrant"
        for user, text in (("alice", "I like apples."), ("bob", "I like bananas.")):
            response = client.post("/v1/remember", headers={"Authorization": "Bearer " + user},
                                   json={"content": text, "scope": "long"})
            assert response.status_code == 200, response.text
        for user, expected in (("alice", "apples"), ("bob", "bananas")):
            response = client.get("/v1/memories", headers={"Authorization": "Bearer " + user})
            assert {f["object"] for f in response.json()["facts"]} == {expected}



def test_unsaved_dimension_migration_can_restore_original_snapshot(config, tmp_path):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        fact = memory.add_fact("user", "likes", "tea", user_id="u")
        memory.save()
        memory.reembed(HashingEmbedder(16))
        # Simulate interruption before committing the new embedder manifest.
    with Memory.open(root, config=config, allow_mismatch=True) as memory:
        assert len(memory.fact_store.get(fact.id).embedding) == 8
        memory.reembed(HashingEmbedder(16))
        memory.save()
        if config.qdrant_url:
            memory._qdrant_backend.erase()



def test_corrupt_original_vector_fails_instead_of_changing_scores(store):
    store.upsert("key", [1.0, 0.0], "value")
    store.client.set_payload(store.collection, payload={"vector": [0.0]},
                             points=[_point_id("key")], wait=True)
    with pytest.raises(StoreFormatError, match="dimension"):
        store.search([1.0, 0.0], 1)


def test_shutdown_waits_for_outermost_active_scope(service_factory):
    svc = service_factory()
    svc.add_fact("alice", "user", "likes", "apples")
    with svc._read_scope("alice") as memory:
        with svc._read_scope("alice"):
            svc.close()
        assert not memory._qdrant_backend._closed
        with svc._read_scope("alice"):
            assert memory.fact_store.values()[0].object == "apples"
    assert memory._qdrant_backend._closed


def test_interrupted_first_write_recovers_empty_snapshot(service_factory):
    svc = service_factory()
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        with svc.write_lock("alice"):
            svc.get("alice").add_fact("user", "likes", "uncommitted", user_id="alice")
            raise RuntimeError("synthetic interruption")
    assert svc.memories("alice")["facts"] == []


def test_remote_other_service_cache_recovers_failed_write(service_factory, mode):
    if mode != "remote":
        pytest.skip("local mode intentionally disallows multiple clients on one path")
    first = service_factory()
    other = service_factory()
    first.add_fact("alice", "user", "likes", "apples")
    assert other.memories("alice")["facts"]
    with pytest.raises(RuntimeError):
        with first.write_lock("alice"):
            first.get("alice").add_fact("user", "likes", "uncommitted", user_id="alice")
            raise RuntimeError("synthetic interruption")
    assert {f["object"] for f in other.memories("alice")["facts"]} == {"apples"}



def test_real_connection_failure_is_http_503_not_empty_success(monkeypatch, tmp_path):
    import socket
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        monkeypatch.setenv("MNEMOTRACE_STORAGE", "qdrant")
        monkeypatch.setenv("MNEMOTRACE_QDRANT_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("MNEMOTRACE_QDRANT_TIMEOUT", "1")
        monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
        svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
        monkeypatch.setattr(appmod, "_svc", svc)
        with TestClient(appmod.app) as client:
            response = client.get("/v1/memories", headers={"Authorization": "Bearer synthetic"})
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "store_unavailable"
            assert str(port) not in response.text



def test_shutdown_finishes_producer_paused_before_enqueue(service_factory, monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_ASYNC_CONSOLIDATION", "1")
    svc = service_factory()
    entered = threading.Event()
    release = threading.Event()
    original = svc._enqueue

    def paused(user):
        entered.set()
        assert release.wait(10)
        original(user)

    monkeypatch.setattr(svc, "_enqueue", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        producer = pool.submit(svc.remember, "u", "I like Python.", scope="long")
        try:
            assert entered.wait(10)
            svc.close()
        finally:
            release.set()
        assert producer.result(timeout=10)["ok"]
    svc.flush()
    reopened = service_factory()
    assert any(f["object"] == "Python" for f in reopened.memories("u")["facts"])


def test_async_close_inside_namespace_scope_does_not_join_its_waiting_worker(
    service_factory, monkeypatch,
):
    import subprocess
    import sys
    monkeypatch.setenv("MNEMOTRACE_ASYNC_CONSOLIDATION", "1")
    svc = service_factory()
    # An isolated process imposes a hard timeout on a lock/join regression.
    snippet = """
import sys
from tests.model_fixtures import MemoryService
svc = MemoryService(data_dir=sys.argv[1], embedder_name='hashing', llm_name='')
with svc.write_lock('u'):
    svc.remember('u', 'I like Python.', scope='long')
    svc.close()
svc.close()
svc.flush()
"""
    result = subprocess.run([sys.executable, "-c", snippet, svc.data_dir],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert any(f["object"] == "Python" for f in svc.memories("u")["facts"])



def test_consolidate_full_preserves_state_with_separate_episode_retrievals(config, tmp_path):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        original = memory.add("I like Python.", user_id="u")
        fact_episodes = memory.retrieve_episodes("Python", user_id="u")
        summary_episodes = memory.retrieve_episodes("Python", user_id="u")
        assert fact_episodes[0].id == summary_episodes[0].id == original.id
        assert fact_episodes[0] is not summary_episodes[0]

        stats = memory.consolidate_full(
            fact_episodes=fact_episodes, summary_episodes=summary_episodes,
        )
        assert stats["facts_added"] == stats["summaries"] == 1
        assert memory.ingestor.pending() == []
        for episode in (memory.episodes_doc.get(original.id),
                        memory.episodes_vec.get(original.id),
                        memory.summary_vec.get(original.id)):
            assert episode.consolidated and episode.summary
        # Preserve summarize_episodes' existing update of the supplied objects.
        assert summary_episodes[0].summary
        assert memory.consolidate()["duplicates"] == 0
        memory.save()

    with Memory.open(root, config=config) as reopened:
        assert reopened.ingestor.pending() == []
        assert reopened.episodes_doc.get(original.id).summary
        if config.qdrant_url:
            reopened._qdrant_backend.erase()


@pytest.mark.parametrize("summary_text", ["Python preference", ""])
def test_reembed_summary_survives_retrieval_consolidation_and_restart(
    config, tmp_path, monkeypatch, summary_text,
):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        original = memory.add("I like Python.", user_id="u")
        monkeypatch.setattr(memory.summarizer, "summarize", lambda episode: summary_text)
        memory.summarize_episodes([original])
        memory.save()
        memory.reembed(HashingEmbedder(16))

        episodes = memory.retrieve_episodes("Python", user_id="u")
        for episode in (memory.episodes_doc.get(original.id), episodes[0],
                        memory.summary_vec.get(original.id)):
            assert len(episode.embedding) == len(episode.summary_embedding) == 16
        assert memory.consolidate(episodes)["facts_added"] == 1
        assert memory.ingestor.pending() == []
        memory.save()

    with Memory.open(root, config=config, embedder=HashingEmbedder(16)) as reopened:
        for episode in (reopened.episodes_doc.get(original.id),
                        reopened.episodes_vec.get(original.id),
                        reopened.summary_vec.get(original.id)):
            assert episode.consolidated
            assert len(episode.embedding) == len(episode.summary_embedding) == 16
        assert reopened.ingestor.pending() == []
        if config.qdrant_url:
            reopened._qdrant_backend.erase()



def test_consolidate_stale_retrieval_keeps_newer_summary_and_metadata(config, tmp_path):
    root = str(tmp_path / "snapshot")
    with Memory.open(root, config=config) as memory:
        original = memory.add("I like Python.", user_id="u")
        stale = memory.retrieve_episodes("Python", user_id="u")
        assert stale[0].summary == "" and stale[0].summary_embedding is None
        original.metadata["note"] = "latest authoritative metadata"
        memory.summarize_episodes(memory.episodes_doc.values())
        latest = memory.episodes_doc.get(original.id)
        summary, summary_embedding = latest.summary, list(latest.summary_embedding)

        assert memory.consolidate(stale)["facts_added"] == 1
        assert stale[0].consolidated  # Successful consolidation still updates supplied objects.
        assert memory.ingestor.pending() == []
        for episode in (memory.episodes_doc.get(original.id),
                        memory.episodes_vec.get(original.id),
                        memory.summary_vec.get(original.id)):
            assert episode.consolidated
            assert episode.summary == summary
            assert episode.summary_embedding == summary_embedding
            assert episode.metadata == latest.metadata
        assert len(memory.summary_vec.values()) == 1
        memory.save()

    with Memory.open(root, config=config) as reopened:
        assert reopened.ingestor.pending() == []
        assert len(reopened.summary_vec.values()) == 1
        for episode in (reopened.episodes_doc.get(original.id),
                        reopened.episodes_vec.get(original.id),
                        reopened.summary_vec.get(original.id)):
            assert episode.consolidated and episode.summary == summary
            assert episode.summary_embedding == summary_embedding
            assert episode.metadata["note"] == "latest authoritative metadata"
        if config.qdrant_url:
            reopened._qdrant_backend.erase()


def test_consolidate_accepts_episode_without_existing_document(config, tmp_path):
    with Memory.open(str(tmp_path / "snapshot"), config=config) as memory:
        episode = Episode(content="I like Python.", user_id="u",
                          embedding=memory.embedder.embed("I like Python."))
        assert memory.episodes_doc.get(episode.id) is None
        assert memory.consolidate([episode])["facts_added"] == 1
        assert episode.consolidated
        assert memory.episodes_doc.get(episode.id).consolidated
        assert memory.retrieve_episodes("Python", user_id="u")[0].id == episode.id
        memory.save()
        if config.qdrant_url:
            memory._qdrant_backend.erase()
