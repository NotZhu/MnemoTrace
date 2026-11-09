"""Strict event evidence: summaries replace raw text across every automatic read."""
from dataclasses import FrozenInstanceError

import pytest

from mnemotrace import MemoryEngine
from mnemotrace.retrieve.events import event_evidence
from tests.embedder_fixtures import MockEmbedder
from mnemotrace.types import Fact


def summarized(memory, content="RAW_ONLY_8844 was disclosed. " * 100):
    ep = memory.add(content, user_id="u", session_id="s", event_time=100)
    ep.summary = "A connection issue was investigated and resolved."
    ep.summary_embedding = memory.embedder.embed(ep.summary)
    memory._sync_episode(ep)
    return ep


def test_summary_replaces_raw_index_and_retrieval_payload():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    assert mem.episodes_vec.get(ep.id) is None
    assert mem.summary_vec.get(ep.id) is not None
    hit, = mem.retrieve_episodes("connection issue", "u")
    assert hit.text == ep.summary and hit.representation == "summary"
    assert not hasattr(hit, "content")
    with pytest.raises(FrozenInstanceError):
        hit.text = "changed"
    with pytest.raises(TypeError):
        hit.metadata["date"] = "changed"
    assert mem.get_episode(ep.id, "u")["content"] == ep.content
    assert mem.get_episode(ep.id, "other") is None


@pytest.mark.parametrize("cascade", [False, True])
def test_contexts_do_not_expand_summary_even_for_provenance(cascade):
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    fact = mem.add_fact("user", "finding", "connection issue resolved", user_id="u", sensitive=False)
    fact.provenance = [ep.id]
    mem._upsert_fact(fact)
    for text in (
        mem.lean_context("connection issue", "u", persona=False, cascade=cascade),
        mem.context_for("connection issue", "u"),
        mem._provenance_raw_block(mem._all_facts(), "connection issue", "u", None, set()),
    ):
        assert "RAW_ONLY_8844" not in text
        assert ep.id in text


def test_summary_survives_reembedding_without_raw_index():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    assert mem.episodes_vec.get(ep.id) is None
    assert mem.summary_vec.get(ep.id) is not None
    mem.reembed()
    assert mem.episodes_vec.get(ep.id) is None
    assert mem.get_episode(ep.id, "u")["content"] == ep.content


def test_raw_keyword_match_returns_only_the_summary():
    outputs = []
    for marker in ("RAW_ONLY_8844", "OTHER_PRIVATE_MARKER"):
        mem = MemoryEngine(embedder=MockEmbedder())
        ep = summarized(mem, marker * 100)
        hits = mem.retrieve_episodes("RAW_ONLY_8844", "u")
        outputs.append([(h.text, h.representation) for h in hits])
        assert event_evidence(ep).text == ep.summary
    assert outputs == [[(ep.summary, 'summary')], []]


def test_aggregation_and_duration_use_summary_not_original():
    from mnemotrace.retrieve.aggregate import extract_aggregation_candidates
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem, "I bought a Tamiya Spitfire model kit. Started RAW_ONLY_8844 today.")
    ep.summary = "No purchases were made."
    assert not extract_aggregation_candidates("How many model kits did I buy?", [], [ep])
    assert "RAW_ONLY_8844" not in mem._duration_block("RAW_ONLY_8844", [], [ep])


def test_supersession_does_not_rewrite_event_summary():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    previous = ep.summary
    old = Fact(subject="user", predicate="works_at", object="A", user_id="u", provenance=[ep.id])
    new = Fact(subject="user", predicate="works_at", object="B", user_id="u", supersedes=old.id)
    old.invalid_at = old.expired_at = new.valid_at
    for fact in (old, new):
        mem.fact_store.upsert(fact.id, mem.embedder.embed(fact.text), fact)
    assert mem.consolidate_full([], [ep])["reflected"] == 0
    assert ep.summary == previous


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
    assert ep.summary in embedder.inputs


def test_default_context_budget_does_not_inspect_summarized_original_length():
    class UnreadableOriginal(str):
        def __len__(self):
            raise AssertionError('retrieval must not inspect original length')
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = summarized(mem)
    ep.content = UnreadableOriginal(ep.content)
    context = mem.lean_context('connection issue', 'u', persona=False)
    assert ep.summary in context and ep.id in context


def test_reembed_discards_orphaned_summary_vector_without_embedding_an_excerpt():
    mem = MemoryEngine(embedder=MockEmbedder())
    ep = mem.add('A synthetic original without a summary.', user_id='u')
    ep.summary_embedding = [1.0] * mem.embedder.dim
    mem.reembed(MockEmbedder(dim=16))
    assert ep.summary_embedding is None
    assert mem.summary_vec.get(ep.id) is None and mem.episodes_vec.get(ep.id) is not None
