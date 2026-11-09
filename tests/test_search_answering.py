from __future__ import annotations

from tests.model_fixtures import MemoryEngine, seed_summaries
from mnemotrace.types import Fact



def _fact(subject, predicate, obj, t, emb=None, source="extracted"):
    return Fact(subject=subject, predicate=predicate, object=obj, user_id="u", source=source,
                valid_at=float(t), created_at=float(t), embedding=emb, cardinality="single")


# ---------------- #1 固定事实保护 ----------------
def test_manual_fact_does_not_supersede_unrelated_slot():
    mem = MemoryEngine()
    old = mem.add_fact("Ana", "works_at", "Orbit", user_id="u", valid_at=1)
    mem.add_fact("Ana", "knowledge_base", "Library", user_id="u", valid_at=2, cardinality="single")
    assert old.is_live()


def test_explicit_single_manual_fact_updates_its_slot():
    mem = MemoryEngine()
    old = mem.add_fact("Ana", "works_at", "Orbit", user_id="u", valid_at=1)
    new = mem.add_fact("Ana", "works_at", "Nadir", user_id="u", valid_at=2, cardinality="single")
    assert not old.is_live()
    assert new.supersedes == old.id


# ---------------- #2/#3 答案类型对齐 ----------------


def test_search_prefers_type_matching_fact():
    mem = MemoryEngine()
    mem.add_fact("project alpha", "owner", "Alice Wang", user_id="u1")
    mem.add_fact("project alpha", "id", "PROJ-1024", user_id="u1")
    ans = mem.search("What is the project alpha ID?", user_id="u1").answer()
    assert "PROJ-1024" in ans, f"ID question must return the ID, got: {ans!r}"


# ---------------- #3b 摘要回退 ----------------
def test_summary_fallback_answers_from_summary():
    mem = MemoryEngine()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_summaries(mem, [ep])
    res = mem._summary_fallback("How do I reset the PAT?", "u1")
    assert res is not None, "a relevant summary should be surfaced when no fact answers"
    assert "pat" in res.answer().lower() or "token" in res.answer().lower()


def test_search_uses_summary_fallback_when_no_fact_ranked():
    mem = MemoryEngine()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_summaries(mem, [ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")
    assert res.via == "summary"
    assert "token" in res.answer().lower()
    assert f"(episode: {ep.id}; space: u1; session: s1)" in res.answer()


def test_summary_fallback_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(summary_fallback=False))
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    seed_summaries(mem, [ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")

    assert res.abstained
    assert res.via == "abstain"


def test_summary_fallback_selects_best_source_backed_summary():
    mem = MemoryEngine()
    weak = mem.add("PAT policy note: keep credentials private.",
                   user_id="u1", session_id="weak")
    strong = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                     user_id="u1", session_id="strong")
    seed_summaries(mem, [weak, strong])

    res = mem._summary_fallback("How do I reset the PAT?", "u1")

    assert res is not None
    assert f"(episode: {strong.id}; space: u1; session: strong)" in res.answer()
    assert "regenerate the token" in res.answer().lower()


def test_summary_fallback_respects_as_of():
    mem = MemoryEngine()
    old = mem.add("To reset the PAT: use the legacy token page.",
                  user_id="u1", session_id="old", event_time=1_700_000_000.0)
    new = mem.add("To reset the PAT: use the new security console.",
                  user_id="u1", session_id="new", event_time=1_702_592_000.0)
    seed_summaries(mem, [old, new])

    res = mem._summary_fallback("How do I reset the PAT?", "u1", as_of=1_700_864_000.0)
    assert res is not None
    ans = res.answer().lower()
    assert "legacy token page" in ans
    assert "new security console" not in ans


# ---------------- derived procedural memory ----------------
def test_search_prefers_source_backed_procedural_memory_for_runbook_queries():
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
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    res = mem.search("PAT security settings", user_id="u1")

    assert res.via == "hybrid"
    assert res.facts[0].provenance == [ep.id]
    assert "security settings" in res.answer()


def test_procedural_extraction_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(procedural_extraction=False))
    mem.add(
        "PAT runbook source: rotate the PAT by opening security settings and updating CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
    )
    mem.consolidate()

    assert [f for f in mem.fact_store.values() if f.predicate == "procedure"] == []


def test_explicit_preference_extraction_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(explicit_preference_extraction=False))
    mem.add("I prefer aisle seats and avoid red-eye flights.", user_id="u1")
    mem.consolidate()

    assert [
        f for f in mem.fact_store.values()
        if f.predicate in {"prefers", "avoids"}
    ] == []


# ---------------- #7 档案权威性 ----------------
def test_profile_build_prefers_authoritative_and_recent():
    from mnemotrace.consolidate.summarizer import ProfileBuilder
    pb = ProfileBuilder()
    extracted = _fact("Wei", "works_at", "a tech company", 1)               # 模糊的，抽取的
    pinned = _fact("Wei", "works_at", "Moonshot AI", 2, source="user")      # 精确的，固定的，更新
    assert pb.build("Wei", [extracted, pinned])["works_at"] == "Moonshot AI"
    assert pb.build("Wei", [pinned, extracted])["works_at"] == "Moonshot AI", "must be order-independent"
