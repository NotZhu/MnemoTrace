"""M0 的保证，以测试的形式固化下来。这些行为让 忆痕 成为一个*记忆*系统而不只是 RAG：
通过非破坏性取代（supersession）实现知识更新、双时间轴 as-of 查询、multi-hop 图推理、拒答、
用户归属隔离——全部在零依赖的离线环境运行。"""
from __future__ import annotations
from mnemotrace.connectors.dialogue import import_data
import pytest

from mnemotrace.config import Config
from tests.model_fixtures import MemoryEngine, link_synthetic_facts
from mnemotrace.types import Fact
from mnemotrace.util import DAY

BASE = 1_700_000_000.0

SCRIPT = [
    ("My name is Wei and I work at Tencent.", 0),
    ("I live in Shenzhen.", 1),
    ("My favorite programming language is Python.", 2),
    ("Actually I just switched jobs — I now work at Moonshot AI.", 30),
    ("My colleague Lin works at Moonshot AI too.", 31),
]


def build() -> MemoryEngine:
    mem = MemoryEngine()
    for text, day in SCRIPT:
        mem.add(text, user_id="u1", event_time=BASE + day * DAY)
    mem.consolidate()
    return mem


def test_zero_dependency_defaults():
    from mnemotrace import MemoryEngine as NativeMemory

    assert NativeMemory().embedder is None


def test_single_hop_qa():
    mem = build()
    assert "shenzhen" in mem.search("Which city do I live in?", user_id="u1").top().object.lower()
    assert "python" in mem.search("What is my favorite programming language?", user_id="u1").top().object.lower()


def test_knowledge_update_supersedes_current_answer():
    mem = build()
    # 当前答案必须反映换工作这件事，而不是过期的旧 fact
    assert "moonshot" in mem.search("Where do I work?", user_id="u1").top().object.lower()


def test_same_time_contradictions_preserve_only_original_episode():
    mem = MemoryEngine()
    mem.add("Wei works at Tencent.\nWei works at Moonshot AI.", user_id="u1", event_time=BASE)
    mem.consolidate()

    assert not mem.fact_store.values()
    assert len(mem.episodes_doc.values()) == 1


@pytest.mark.history_import
def test_records_import_same_session_extracts_multiple_messages():
    mem = MemoryEngine()
    stats = import_data(mem, [
        {"session_id": "career", "content": "Wei works at Tencent.", "event_time": BASE},
        {"session_id": "career", "content": "Wei works at Moonshot AI.", "event_time": BASE + DAY},
    ], format="records", user_id="u1", generate_digests=False)

    assert stats["sessions"] == 1
    assert stats["episodes"] == 2
    assert stats["facts_added"] == 2
    assert "tencent" in mem.as_of("Where do I work?", BASE + 1, user_id="u1").top().object.lower()
    assert "moonshot" in mem.search("Where do I work?", user_id="u1").top().object.lower()


def test_same_slot_identity_facts_remain_eligible_when_both_are_current():
    mem = MemoryEngine(config=Config(w_sem=0.0, w_lex=0.0, w_graph=0.0, w_rec=0.0, w_sal=1.0))
    old = Fact(
        "Wei",
        "works_at",
        "Tencent",
        user_id="u1",
        valid_at=BASE,
        salience=100.0,
        embedding=mem.embedder.embed("Wei works at Tencent"), text='Wei works at Tencent',
    )
    new = Fact(
        "Wei",
        "works_at",
        "Moonshot AI",
        user_id="u1",
        valid_at=BASE + DAY,
        salience=1.0,
        supersedes=[old.id],
        embedding=mem.embedder.embed("Wei works at Moonshot AI"), text='Wei works at Moonshot AI',
    )
    mem.fact_store.upsert(old.id, old.embedding or [], old)
    mem.fact_store.upsert(new.id, new.embedding or [], new)

    result = mem.search("Where do I work?", user_id="u1")
    assert {f.object for f in result.facts} == {"Tencent", "Moonshot AI"}


def test_same_slot_non_identity_facts_remain_eligible_when_both_are_current():
    mem = MemoryEngine(config=Config(w_sem=0.0, w_lex=0.0, w_graph=0.0, w_rec=0.0, w_sal=1.0))
    old = Fact(
        "Wei",
        "major",
        "physics",
        user_id="u1",
        valid_at=BASE,
        salience=100.0,
        embedding=mem.embedder.embed("Wei major physics"), text='Wei major physics',
    )
    new = Fact(
        "Wei",
        "major",
        "computer science",
        user_id="u1",
        valid_at=BASE + DAY,
        salience=1.0,
        supersedes=[old.id],
        embedding=mem.embedder.embed("Wei major computer science"), text='Wei major computer science',
    )
    mem.fact_store.upsert(old.id, old.embedding or [], old)
    mem.fact_store.upsert(new.id, new.embedding or [], new)

    ranked, _ = mem.retriever.retrieve(
        "What is Wei's major?", "u1", top_k=5
    )
    objs = {f.object for f, _ in ranked}
    assert "computer science" in objs
    assert "physics" in objs


def test_multi_valued_identity_facts_are_not_slot_deduped():
    mem = MemoryEngine()
    mem.add_fact("Wei", "owns", "a bike", user_id="u1", text='Wei owns a bike')
    mem.add_fact("Wei", "owns", "a camera", user_id="u1", text='Wei owns a camera')

    ranked, _ = mem.retriever.retrieve("What does Wei own?", "u1", top_k=5)
    assert {f.object for f, _ in ranked} >= {"a bike", "a camera"}


def test_zero_score_evidence_signals_do_not_boost_irrelevant_salient_facts():
    """RRF 应该融合证据，而不是任意的零分位次。

    target 是唯一的 lexical 命中。distractor 的显著度极高但没有任何 lexical 证据。如果 lexical
    排序把零分文档也纳入，distractor 几乎能借到与真实命中相同的 lexical RRF 贡献，从而反超它。
    过滤掉零证据才能让相关性优先。
    """
    mem = MemoryEngine(config=Config(w_sem=0.0, w_lex=1.0, w_graph=0.0, w_rec=0.0, w_sal=0.6))
    target = Fact(
        "Wei",
        "ticket_code",
        "alpha-42",
        user_id="u1",
        salience=0.1,
        embedding=mem.embedder.embed("Wei ticket code alpha 42"), text='Wei ticket code alpha-42',
    )
    distractor = Fact(
        "Wei",
        "likes",
        "jazz",
        user_id="u1",
        salience=100.0,
        embedding=mem.embedder.embed("Wei likes jazz"), text='Wei likes jazz',
    )
    mem.fact_store.upsert(target.id, target.embedding or [], target)
    mem.fact_store.upsert(distractor.id, distractor.embedding or [], distractor)

    ranked, diag = mem.retriever.retrieve("ticket code alpha", "u1", top_k=2)

    assert ranked[0][0] is target
    assert diag["lex"][target.id] > 0
    assert diag["lex"][distractor.id] == 0


def test_project_rules_are_structured_multi_value_memory():
    mem = MemoryEngine()
    mem.add(
        "Project rule: benchmark claims require committed raw logs before public copy changes.",
        user_id="codex-e2e",
        session_id="codex:repo:thread",
    )
    mem.add(
        "Project preference: Codex should close MnemoTrace sessions when tasks end.",
        user_id="codex-e2e",
        session_id="codex:repo:thread",
    )
    mem.consolidate()

    live = [f for f in mem.fact_store.values() if f.is_live()]
    assert {f.predicate for f in live} >= {"rule", "preference"}
    assert any(f.text.startswith("project rule ") for f in live)
    assert any(f.text.startswith("project preference ") for f in live)
    assert any("raw logs" in f.object for f in live)
    assert any("close MnemoTrace sessions" in f.object for f in live)
    assert len(live) == 2


def test_runbook_source_extracts_literal_fact_with_full_steps():
    mem = MemoryEngine()
    mem.add(
        "PAT runbook source: rotate the PAT by opening security settings, regenerating the token, "
        "then updating CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
        event_time=BASE,
    )
    mem.consolidate()

    facts = [f for f in mem.fact_store.values() if f.predicate == "procedure"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact.subject == "PAT"
    assert fact.object_id is None
    assert "security settings" in fact.object
    assert "regenerating the token" in fact.object
    assert "updating CI secrets" in fact.object
    assert all("security settings" not in ent.name for ent in mem.graph.entities.values())
    assert mem.get_episode(fact.provenance[0], "u1") is not None


def test_to_colon_howto_extracts_literal_fact():
    mem = MemoryEngine()
    mem.add(
        "To rotate the PAT: open security settings, regenerate the token, then update CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
        event_time=BASE,
    )
    mem.consolidate()

    fact, = mem.fact_store.values()
    assert fact.object_id is None
    assert "security settings" in fact.object
    assert fact.provenance


def test_short_project_rule_context_uses_extracted_facts():
    mem = MemoryEngine()
    user = "codex-e2e"
    session = "codex:repo:thread"
    mem.add(
        "Project rule: MnemoTrace benchmark claims must be backed by committed raw logs before public copy changes.",
        user_id=user,
        session_id=session,
    )
    source = mem.add(
        "Project preference: Codex should use MnemoTrace as cross-agent memory and call mnemotrace_close_session when a task ends.",
        user_id=user,
        session_id=session,
    )
    mem.consolidate()

    ctx = mem.lean_context(
        "What should Codex remember about benchmark claims and session closing?",
        user_id=user,
    )
    assert "raw logs" in ctx
    assert "mnemotrace_close_session" not in ctx
    assert "mnemotrace_close_session" in mem.get_episode(source.id, user)["content"]


def test_no_hard_delete_history_preserved():
    mem = build()
    chain = mem.history("我", "works_at", user_id="u1")
    objects = {f.object for f in chain}
    assert objects == {"Tencent", "Moonshot AI"}
    live = [f for f in chain if f.is_live()]
    assert len(live) == 1 and live[0].object == "Moonshot AI"
    superseded = [f for f in chain if not f.is_live()]
    assert len(superseded) == 1 and superseded[0].object == "Tencent"
    # 新 fact 指回它取代的旧 fact（演化链）
    assert live[0].supersedes == [superseded[0].id]


def test_bitemporal_as_of_query():
    mem = build()
    before = mem.as_of("Where do I work?", BASE + 10 * DAY, user_id="u1")
    assert "tencent" in before.top().object.lower()
    after = mem.search("Where do I work?", user_id="u1")
    assert "moonshot" in after.top().object.lower()


def test_multi_hop_reasoning():
    mem = build()
    link_synthetic_facts(mem, {"colleague", "works_at"})
    path, = mem.follow_path("我", ["colleague", "works_at"], user_id="u1").paths
    assert "moonshot" in path.endpoint.lower()


def test_family_profession_multi_session_reasoning():
    mem = MemoryEngine()
    mem.add("My sister Maya is a pediatrician at a children's hospital.", user_id="u1",
            session_id="s1", event_time=BASE)
    mem.add("Maya just moved to Seattle for a fellowship.", user_id="u1",
            session_id="s2", event_time=BASE + DAY)
    mem.consolidate()

    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("我", "sister", "Maya") in facts
    assert ("Maya", "occupation", "pediatrician") in facts
    assert ("Maya", "lives_in", "Seattle") in facts

    link_synthetic_facts(mem, {"sister", "occupation"})
    path, = mem.follow_path("我", ["sister", "occupation"], user_id="u1").paths
    assert path.endpoint == "pediatrician"


def test_multiple_family_relations_accumulate_and_disambiguate():
    mem = MemoryEngine()
    mem.add("My sister Anna is a lawyer.", user_id="u1", session_id="s1", event_time=BASE)
    mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s2", event_time=BASE + DAY)
    mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s3", event_time=BASE + 2 * DAY)
    mem.consolidate()

    sisters = [f for f in mem.history("我", "sister", user_id="u1") if f.is_live()]
    assert {f.object for f in sisters} == {"Anna", "Maya"}

    link_synthetic_facts(mem, {"sister", "occupation"})
    paths = mem.follow_path("我", ["sister", "occupation"], user_id="u1").paths
    assert {path.endpoint: path.facts[0].object for path in paths} == {
        "lawyer": "Anna", "pediatrician": "Maya",
    }


def test_abstention_unknown_attribute():
    mem = build()
    # 已有本人事实，但尚无喜欢的食物。
    r = mem.search("What is my favorite food?", user_id="u1")
    assert r.abstained
    # 完全无关的查询
    assert mem.search("What is the capital of France?", user_id="u1").abstained


def test_first_person_occupation_reaches_profile():
    mem = MemoryEngine()
    mem.add("I am a pediatrician.", user_id="u1", event_time=BASE)
    mem.consolidate()

    assert ("我", "occupation", "pediatrician") in {
        (f.subject, f.predicate, f.object) for f in mem.fact_store.values()
    }
    assert mem.profile("u1")["pending_fact_count"] == 1


def test_first_person_works_as_role_is_atomic():
    mem = MemoryEngine()
    mem.add("I work as a data engineer at Spotify.", user_id="u1", event_time=BASE)
    mem.consolidate()

    assert ("我", "occupation", "data engineer") in {
        (f.subject, f.predicate, f.object) for f in mem.fact_store.values()
    }


def test_first_person_fan_of_is_preference_not_occupation():
    mem = MemoryEngine()
    mem.add("I'm a fan of jazz.", user_id="u1", event_time=BASE)
    mem.add("I'm into synthwave.", user_id="u1", event_time=BASE + DAY)
    mem.consolidate()

    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("我", "likes", "jazz") in facts
    assert ("我", "likes", "synthwave") in facts
    assert "occupation" not in {p for _, p, _ in facts}


def test_explicit_preference_verbs_extract_profile_facts():
    mem = MemoryEngine()
    mem.add("I prefer aisle seats and avoid red-eye flights.", user_id="u1", event_time=BASE)
    mem.add("I don't like crowded lounges.", user_id="u1", event_time=BASE + DAY)
    mem.consolidate()

    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("我", "prefers", "aisle seats") in facts
    assert ("我", "avoids", "red-eye flights") in facts
    assert ("我", "dislikes", "crowded lounges") in facts


def test_names_are_facts_and_do_not_rewrite_the_owner_subject():
    mem = MemoryEngine()
    mem.add("My name is Wei.", user_id="u1", event_time=BASE)
    mem.add("I like jazz.", user_id="u1", event_time=BASE + DAY)
    mem.consolidate()
    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("我", "name", "Wei") in facts
    assert ("我", "likes", "jazz") in facts


def test_preference_objects_are_preserved_as_returned_by_the_model():
    mem = MemoryEngine()
    mem.add("I like the sound of the avocado sauce.", user_id="u1", event_time=BASE)
    mem.consolidate()
    assert [(f.subject, f.object) for f in mem.fact_store.values()] == [
        ("我", "sound of the avocado sauce")]


def test_pure_withdrawal_is_saved_as_source_without_a_negative_fact():
    mem = MemoryEngine()
    mem.add("I like jazz.", user_id="u1", event_time=BASE)
    source = mem.add("I no longer like jazz.", user_id="u1", event_time=BASE + DAY)
    mem.consolidate()
    assert mem.episodes_doc.get(source.id).content == "I no longer like jazz."
    fact, = mem.fact_store.values()
    assert (fact.subject, fact.predicate, fact.object, fact.is_live()) == ("我", "likes", "jazz", True)


def test_import_style_moving_and_dietary_restrictions_extract_cleanly():
    mem = MemoryEngine()
    mem.add("I'm moving to Berlin next month for a job at Acme.", user_id="u1", event_time=BASE)
    mem.add("By the way, I'm vegetarian and allergic to peanuts.", user_id="u1", event_time=BASE + DAY)
    mem.consolidate()

    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("我", "lives_in", "Berlin") in facts
    assert ("我", "works_at", "Acme") in facts
    assert ("我", "diet", "vegetarian") in facts
    assert ("我", "allergic_to", "peanuts") in facts

    assert mem.profile("u1")["needs_refresh"]


def test_assistant_small_talk_is_not_profile_occupation():
    mem = MemoryEngine()
    mem.add("Exciting! Berlin is great.", user_id="u1", role="assistant", event_time=BASE)
    mem.consolidate()

    facts = {(f.subject, f.predicate, f.object) for f in mem.fact_store.values()}
    assert ("Berlin", "occupation", "great") not in facts
    assert "occupation" not in {p for _, p, _ in facts}


def test_stemmer_aligns_inflections_for_lexical_retrieval():
    """BM25 lexical 打分器使用英文词干。复数以及 -y/-ies 的形态
    必须归约到共同的词干，否则 lexical 检索会静默漏掉匹配。"""
    from mnemotrace.retrieve.lexical import lexical_terms, stem

    assert stem("works") == stem("work")
    assert stem("colleagues") == stem("colleague")
    assert stem("studies") == stem("study")
    assert stem("cities") == stem("city")
    assert stem("salaries") == stem("salary")
    assert stem("companies") == stem("company")
    assert stem("carries") == stem("carry")

    # 以 -ies 结尾的短基础词不是 -y 形态（tie/lies/dies/pies）——只剥掉裸的 -s，让它们与单数保持
    # 一致，而不是被折叠成 -y 词干。
    assert stem("ties") == stem("tie")
    assert stem("lies") == stem("lie")
    assert stem("dies") == stem("die")

    # Sanity 检查：lexical_terms() 把同一规则应用到自由格式的查询上
    q_stems = set(lexical_terms("Which cities did Wei study in?"))
    assert "city" in q_stems and "studi" not in q_stems  # 只有 -ies 规则失效时才会出现 studi
