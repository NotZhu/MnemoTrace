from __future__ import annotations

from tests.model_fixtures import seed_timeline
from tests.model_fixtures import MemoryEngine, seed_digests
from mnemotrace.types import Fact


def _fact(subject, predicate, obj, t, emb=None, source="extracted"):
    return Fact(subject=subject, predicate=predicate, object=obj, user_id="u", source=source,
                valid_at=float(t), created_at=float(t), embedding=emb, text=' '.join((subject, predicate.replace('_', ' '), obj)))


# ---------------- #1 固定事实保护 ----------------
def test_manual_fact_does_not_supersede_unrelated_slot():
    mem = MemoryEngine()
    old = mem.add_fact("Ana", "works_at", "Orbit", user_id="u", valid_at=1, text='Ana works at Orbit')
    seed_timeline(mem, "Ana", "knowledge_base", "Library", user_id="u", valid_at=2, text='Ana knowledge base Library')
    assert old.is_live()


def test_explicit_single_manual_fact_updates_its_slot():
    mem = MemoryEngine()
    old = mem.add_fact("Ana", "works_at", "Orbit", user_id="u", valid_at=1, text='Ana works at Orbit')
    new = seed_timeline(mem, "Ana", "works_at", "Nadir", user_id="u", valid_at=2, text='Ana works at Nadir')
    assert not old.is_live()
    assert new.supersedes == [old.id]


# ---------------- #2/#3 答案类型对齐 ----------------


def test_search_prefers_type_matching_fact():
    mem = MemoryEngine()
    mem.add_fact("project alpha", "owner", "Alice Wang", user_id="u1", text='project alpha owner Alice Wang')
    mem.add_fact("project alpha", "id", "PROJ-1024", user_id="u1", text='project alpha id PROJ-1024')
    result = mem.search("What is the project alpha ID?", user_id="u1")
    assert result.status == "evidence_only" and not result.answer()
    assert result.top().object == "PROJ-1024"


# ---------------- #3b 摘要回退 ----------------
def test_digest_fallback_answers_from_digest():
    mem = MemoryEngine()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_digests(mem, [ep])
    res = mem._digest_fallback("How do I reset the PAT?", "u1")
    assert res is not None, "a relevant digest should be surfaced when no fact answers"
    assert res.status == "evidence_only" and "token" in res.sources[0].text.lower()


def test_search_uses_digest_fallback_when_no_fact_ranked():
    mem = MemoryEngine()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_digests(mem, [ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")
    assert res.via == "digest"
    assert "token" in res.sources[0].text.lower()
    assert (res.sources[0].id, res.sources[0].session_id) == (ep.id, "s1")


def test_digest_fallback_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(digest_fallback=False))
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_digests(mem, [ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")

    assert res.abstained
    assert res.via == "abstain"


def test_digest_fallback_selects_best_source_backed_digest():
    mem = MemoryEngine()
    weak = mem.add("PAT policy note: keep credentials private.",
                   user_id="u1", session_id="weak")
    strong = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                     user_id="u1", session_id="strong")
    seed_digests(mem, [weak, strong])

    res = mem._digest_fallback("How do I reset the PAT?", "u1")

    assert res is not None
    assert res.sources[0].id == strong.id
    assert "regenerate the token" in res.sources[0].text.lower()


def test_digest_fallback_respects_as_of():
    mem = MemoryEngine()
    old = mem.add("To reset the PAT: use the legacy token page.",
                  user_id="u1", session_id="old", event_time=1_700_000_000.0)
    new = mem.add("To reset the PAT: use the new security console.",
                  user_id="u1", session_id="new", event_time=1_702_592_000.0)
    seed_digests(mem, [old, new])

    old.digest_written_at = old.ingested_at = old.event_time
    new.digest_written_at = new.ingested_at = new.event_time
    cutoff = 1_700_864_000.0
    res = mem._digest_fallback("How do I reset the PAT?", "u1", as_of=cutoff)
    assert res is not None
    ans = res.sources[0].text.lower()
    assert "legacy token page" in ans
    assert "new security console" not in ans


# ---------------- runbook evidence ----------------
def test_search_prefers_source_backed_literal_facts_for_runbook_queries():
    from mnemotrace.types import Fact

    mem = MemoryEngine()
    ep = mem.add(
        "PAT runbook source: rotate the PAT by opening security settings and updating CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
    )
    fact = Fact(
        subject="PAT",
        predicate="procedure",
        object="open security settings and update CI secrets",
        user_id="u1",
        provenance=[ep.id], text='PAT procedure open security settings and update CI secrets',
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    res = mem.search("PAT security settings", user_id="u1")

    assert res.via == "hybrid"
    assert res.facts[0].provenance == [ep.id]
    assert res.status == "evidence_only" and res.source_ids == (ep.id,)
    assert not res.sources



def test_free_text_is_candidate_evidence_and_slot_answer_uses_full_view():
    from mnemotrace.store.contracts import FactQuery

    mem = MemoryEngine()
    bob = mem.add_fact("Bob", "favorite_language", "Ab1", text='Bob favorite language Ab1')
    result = mem.search("What is Alice's favorite language?")
    assert result.status == "evidence_only" and result.top().id == bob.id and result.answer() == ""
    missing = mem.search("", facts=FactQuery(subject="Alice", predicate="favorite_language"))
    assert missing.status == "no_evidence"
    bob_slot = FactQuery(subject="Bob", predicate="favorite_language")
    assert mem.search("", facts=bob_slot).answer() == "Ab1"
    mem.add_fact("Bob", "favorite_language", "ab1", text='Bob favorite language ab1')
    result = mem.search("", facts=bob_slot, top_k=1)
    assert len(result.facts) == 1 and result.status == "evidence_only"


def test_raw_source_is_explicit_only_and_digest_respects_redaction():
    from mnemotrace.store.contracts import FactQuery

    mem = MemoryEngine()
    source = mem.add("Synthetic violet passphrase")
    result = mem.search("violet passphrase")
    assert result.status == "no_evidence"
    assert mem.get_episode(source.id)["content"] == source.content
    seed_digests(mem, [source])
    assert mem.search("violet passphrase").sources[0].id == source.id
    assert mem.search("violet passphrase", facts=FactQuery(exclude_sensitive=True)).status == "no_evidence"


def test_context_budget_preserves_whole_assertions_and_prefers_evidence():
    from mnemotrace.retrieve.context import pack_sections, render_sections

    fact = "- User does not use Java unless approved."
    sections, limited = pack_sections([
        ("facts", "FACTS", ["- " + "oversize " * 20, fact]),
        ("profile", "PROFILE", ["- Unrelated profile"]),
    ], len("FACTS\n" + fact), "synthetic")
    assert limited and [section.kind for section in sections] == ["facts"]
    assert render_sections(sections) == "FACTS\n" + fact
    assert sections[0].space_id == "synthetic"
