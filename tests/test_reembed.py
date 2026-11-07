"""Regenerating an in-memory work set updates all retrieval vectors."""
from mnemotrace.embed import HashingEmbedder
from tests.model_fixtures import MemoryEngine, seed_summaries


def test_reembed_migrates_every_vector():
    memory = MemoryEngine(embedder=HashingEmbedder(64))
    memory.add("My name is Wei and I live in Shenzhen.", user_id="u", consolidate=True)
    seed_summaries(memory, list(memory.episodes_doc.values()))
    raw = memory.add("Synthetic raw event", user_id="u")
    counts = memory.reembed(HashingEmbedder(128))
    assert counts["facts"] >= 1 and counts["episodes"] >= 1 and counts["summaries"] >= 1
    assert all(len(fact.embedding) == 128 for fact in memory.fact_store.values())
    raw_result, = memory.episodes_vec.values()
    assert raw_result.id == raw.id and len(raw_result.embedding) == 128
    assert all(len(episode.summary_embedding) == 128 for episode in memory.summary_vec.values())
    assert memory.search("where do I live", user_id="u").facts
