"""Strict event evidence: digests replace raw text across every automatic read."""
from dataclasses import FrozenInstanceError

import pytest

from mnemotrace import MemoryEngine
from mnemotrace.store.contracts import FactQuery
from mnemotrace.util import now
from tests.embedder_fixtures import MockEmbedder
from mnemotrace.types import Fact


def summarized(memory, content="RAW_ONLY_8844 was disclosed. " * 100):
    ep = memory.add(content, user_id="u", session_id="s", event_time=100)
    ep.digest = "A connection issue was investigated and resolved."
    ep.digest_embedding = memory.embedder.embed(ep.digest)
    memory._sync_episode(ep)
    return ep


def test_digest_replaces_raw_index_and_retrieval_payload():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    assert mem.digest_vec.get(ep.id) is not None
    hit, = mem.retrieve_digests("connection issue", "u")
    assert hit.text == ep.digest and hit.representation == "digest"
    assert not hasattr(hit, "content")
    with pytest.raises(FrozenInstanceError):
        hit.text = "changed"
    with pytest.raises(TypeError):
        hit.metadata["date"] = "changed"
    assert mem.get_episode(ep.id, "u")["content"] == ep.content
    assert mem.get_episode(ep.id, "other") is None


def test_automatic_recall_returns_source_ids_without_original_text():
    mem = MemoryEngine()
    ep = mem.add("ARCHIVE_ONLY_8844", user_id="u", session_id="s")
    fact = mem.add_fact("我", "finding", "connection issue", user_id="u", sensitive=False,
                        text="user finding connection issue")
    fact.provenance = [ep.id]
    mem._upsert_fact(fact)
    result = mem.search("connection issue", "u")
    assert result.source_ids == (ep.id,) and not result.sources
    assert mem.search("connection issue", "u", facts=FactQuery(exclude_sensitive=True)).source_ids == ()
    assert not mem.search("ARCHIVE_ONLY_8844", "u").facts
    assert not mem.retrieve_digests("ARCHIVE_ONLY_8844", "u")
    assert "ARCHIVE_ONLY_8844" not in mem.lean_context("connection issue", "u")
    assert "ARCHIVE_ONLY_8844" not in mem.context_for("connection issue", "u")
    assert mem.get_episode(result.source_ids[0], "u")["content"] == ep.content


def test_digest_survives_reembedding_without_raw_index():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    assert mem.digest_vec.get(ep.id) is not None
    mem.reembed()
    assert mem.get_episode(ep.id, "u")["content"] == ep.content


@pytest.mark.parametrize("written_at", [3, None])
def test_snapshot_import_preserves_digest_write_time_and_queries_by_event_time(written_at):
    mem = MemoryEngine(embedder=MockEmbedder())
    mem.import_snapshot({"format_version": 6, "facts": [], "episodes": [{
        "id": "synthetic", "content": "Synthetic original", "session_id": "test", "role": "user",
        "event_time": 1, "ingested_at": 2, "digest": "An orchid was planted.",
        "digest_written_at": written_at,
    }]})
    mem.reembed()
    assert not mem.retrieve_digests("orchid", as_of=0.5)
    for at in (1, 2.5, 4, None):
        hit, = mem.retrieve_digests("orchid", as_of=at)
        assert hit.text == "An orchid was planted."
    restored, = mem.episodes_doc.values()
    assert restored.digest_written_at == written_at


def test_digest_keyword_matching_excludes_original_text():
    mem = MemoryEngine()
    ep = mem.add("RAW_ONLY_8844", user_id="u")
    before = now()
    ep.digest = "A connection issue was resolved."
    ep.digest_written_at = before + 1
    mem._sync_episode(ep)
    assert not mem.retrieve_digests("RAW_ONLY_8844", "u")
    assert not mem.retrieve_digests("connection issue", "u", as_of=before)
    hit, = mem.retrieve_digests("connection issue", "u")
    assert (hit.id, hit.text) == (ep.id, ep.digest)


def test_supersession_does_not_rewrite_event_digest():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    previous = ep.digest
    old = Fact(subject="我", predicate="works_at", object="A", user_id="u", provenance=[ep.id], text='user works at A')
    new = Fact(subject="我", predicate="works_at", object="B", user_id="u", supersedes=[old.id], text='user works at B')
    old.invalid_at = old.expired_at = new.valid_at
    for fact in (old, new):
        mem.fact_store.upsert(fact.id, mem.embedder.embed(fact.text), fact)
    assert mem.consolidate_full([], [ep])["digests"] == 0
    assert ep.digest == previous


def test_reembed_never_embeds_summarized_original():
    class RecordingEmbedder(MockEmbedder):
        def __init__(self):
            super().__init__()
            self.inputs = []
        def embed_batch(self, texts):
            self.inputs.extend(texts)
            return super().embed_batch(texts)
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    embedder = RecordingEmbedder()
    mem.reembed(embedder)
    assert ep.content not in embedder.inputs
    assert ep.digest in embedder.inputs


def test_default_context_budget_does_not_inspect_summarized_original_length():
    class UnreadableOriginal(str):
        def __len__(self):
            raise AssertionError('retrieval must not inspect original length')
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    ep.content = UnreadableOriginal(ep.content)
    context = mem.lean_context('connection issue', 'u')
    assert ep.digest in context and ep.id in context


def test_reembed_discards_orphaned_digest_vector_without_embedding_an_excerpt():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = mem.add('A synthetic original without a digest.', user_id='u')
    ep.digest_embedding = [1.0] * mem.embedder.dim
    mem.reembed(MockEmbedder(dim=16))
    assert ep.digest_embedding is None
    assert mem.digest_vec.get(ep.id) is None
