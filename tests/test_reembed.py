"""Regenerating an in-memory work set updates all retrieval vectors."""
from tests.embedder_fixtures import MockEmbedder
from tests.model_fixtures import MemoryEngine, seed_digests


def test_reembed_migrates_every_vector():
    memory = MemoryEngine(embedder=MockEmbedder(64))
    memory.add("My name is Wei and I live in Shenzhen.", user_id="u", consolidate=True)
    seed_digests(memory, list(memory.episodes_doc.values()))
    raw = memory.add("Synthetic raw event", user_id="u")
    counts = memory.reembed(MockEmbedder(128))
    assert counts["facts"] >= 1 and counts["episodes"] >= 1 and counts["digests"] >= 1
    assert all(len(fact.embedding) == 128 for fact in memory.fact_store.values())
    raw_result, = memory.episodes_vec.values()
    assert raw_result.id == raw.id and len(raw_result.embedding) == 128
    assert all(len(episode.digest_embedding) == 128 for episode in memory.digest_vec.values())
    assert memory.search("where do I live", user_id="u").facts
