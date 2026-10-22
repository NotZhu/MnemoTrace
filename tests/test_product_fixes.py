"""来自一线缺陷报告的读路径产品修复（对 LongMemEval harness 零影响——这些路径是
search()/answer()/add_fact 时的冲突处理，benchmark 的 lean_context 从不触达）：

  #1  固定（source="user"）的事实绝不能被跨槽位的模糊匹配取代（即“打地鼠”bug）。
  #2/#3  search() 将答案对齐到问题所要求的类型（id/date/number/email/...），做不到时
         诚实拒答，而不是返回一个类型不匹配的顶部事实（问“项目 ID?”却给出负责人的名字）。
  #3b  当原子化事实回答不了时，回退到相关的会话摘要（抽取器从未提炼出的信息——
       某个操作方法、某条规则、某条安装命令）。
"""
from __future__ import annotations

from mnemotrace import Memory
from mnemotrace.consolidate.conflict import ConflictResolver
from mnemotrace.memory import _ANSWER_TYPE_MATCH, _expected_answer_type
from mnemotrace.types import Fact

_SENTINEL_EMBEDDER = object()  # 非 None 即可启用语义路径；不会真正调用 .embed()


def _fact(subject, predicate, obj, t, emb=None, source="extracted"):
    return Fact(subject=subject, predicate=predicate, object=obj, user_id="u", source=source,
                valid_at=float(t), created_at=float(t), embedding=emb)


# ---------------- #1 固定事实保护 ----------------
def test_pinned_fact_never_fuzzy_supersedes_across_slots():
    # 一条手工（source="user"）事实在嵌入空间里靠近同主体的另一条无关单值事实
    # （相似度被主体词主导）。没有这道保护时，语义路径会失效化那条无关事实。
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.80)
    old = _fact("王", "works_at", "字节跳动", 1, emb=[1.0, 0.0])                      # 抽取的，单值
    new = _fact("王", "knowledge_base", "某知识库", 2, emb=[1.0, 0.0], source="user")  # 固定的，不同槽位，cos=1
    action, inv = r.reconcile(new, [old])
    assert action == "add"
    assert inv == [] and old.invalid_at is None, "a pinned fact must not whack the unrelated works_at fact"


def test_pinned_fact_still_updates_its_own_slot():
    # 保护范围是收窄的：固定事实仍必须能更新同一个槽位（走精确槽位路径，而非模糊路径）。
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.80)
    old = _fact("王", "works_at", "字节跳动", 1, emb=[1.0, 0.0], source="user")
    new = _fact("王", "works_at", "月之暗面", 2, emb=[1.0, 0.0], source="user")
    action, inv = r.reconcile(new, [old])
    assert inv == [old] and new.supersedes == old.id, "same-slot pinned update must still supersede"


def test_extracted_cross_slot_supersession_unchanged():
    # 保护是按来源区分的：抽取的同属性更新照常生效（该行为不变）。
    r = ConflictResolver(_SENTINEL_EMBEDDER, sim_threshold=0.80)
    old = _fact("user", "current_city", "Beijing", 1, emb=[1.0, 0.0])
    new = _fact("user", "lives_in", "Shanghai", 2, emb=[1.0, 0.0])  # 抽取的，同属性，自由格式谓词
    _, inv = r.reconcile(new, [old])
    assert inv == [old], "extracted same-attribute update should still supersede (unchanged)"


# ---------------- #2/#3 答案类型对齐 ----------------
def test_type_helpers():
    assert _expected_answer_type("What is the project ID?") == "id"
    assert _expected_answer_type("When did I start?") is None           # 单独的 'when' 刻意不作为线索
    assert _expected_answer_type("Did I consider it?") is None          # 'id' 不能匹配到 'did' 内部
    assert _expected_answer_type("我的工单号是多少?") == "id"
    assert _ANSWER_TYPE_MATCH["id"]("PROJ-1024") and not _ANSWER_TYPE_MATCH["id"]("Alice Wang")
    assert _ANSWER_TYPE_MATCH["email"]("a@b.com") and not _ANSWER_TYPE_MATCH["email"]("Alice")


def test_search_prefers_type_matching_fact():
    mem = Memory()
    mem.add_fact("project alpha", "owner", "Alice Wang", user_id="u1")
    mem.add_fact("project alpha", "id", "PROJ-1024", user_id="u1")
    ans = mem.search("What is the project alpha ID?", user_id="u1").answer()
    assert "PROJ-1024" in ans, f"ID question must return the ID, got: {ans!r}"


def test_search_abstains_instead_of_fabricating_wrong_type():
    mem = Memory()
    mem.add_fact("project alpha", "owner", "Alice Wang", user_id="u1")  # 只有名字；没有存储任何 id
    res = mem.search("What is the project alpha ID?", user_id="u1")
    assert res.abstained, "must abstain when the demanded type isn't in memory"
    assert "Alice" not in res.answer(), "must NOT pass the owner name off as the ID"


# ---------------- #3b 摘要回退 ----------------
def test_summary_fallback_answers_from_summary():
    mem = Memory()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    mem.summarize_episodes([ep])
    res = mem._summary_fallback("How do I reset the PAT?", "u1")
    assert res is not None, "a relevant summary should be surfaced when no fact answers"
    assert "pat" in res.answer().lower() or "token" in res.answer().lower()


def test_search_uses_summary_fallback_when_no_fact_ranked():
    mem = Memory()
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    mem.summarize_episodes([ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")
    assert res.via == "summary"
    assert "token" in res.answer().lower()
    assert "(session: s1)" in res.answer()


def test_summary_fallback_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(summary_fallback=False))
    ep = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                 user_id="u1", session_id="s1")
    mem.summarize_episodes([ep])

    res = mem.search("How do I reset the PAT?", user_id="u1")

    assert res.abstained
    assert res.via == "abstain"


def test_summary_fallback_selects_best_source_backed_summary():
    mem = Memory()
    weak = mem.add("PAT policy note: keep credentials private.",
                   user_id="u1", session_id="weak")
    strong = mem.add("To reset the PAT: open settings, regenerate the token, then update CI.",
                     user_id="u1", session_id="strong")
    mem.summarize_episodes([weak, strong])

    res = mem._summary_fallback("How do I reset the PAT?", "u1")

    assert res is not None
    assert "(session: strong)" in res.answer()
    assert "regenerate the token" in res.answer().lower()


def test_summary_fallback_respects_as_of():
    mem = Memory()
    old = mem.add("To reset the PAT: use the legacy token page.",
                  user_id="u1", session_id="old", event_time=1_700_000_000.0)
    new = mem.add("To reset the PAT: use the new security console.",
                  user_id="u1", session_id="new", event_time=1_702_592_000.0)
    mem.summarize_episodes([old, new])

    res = mem._summary_fallback("How do I reset the PAT?", "u1", as_of=1_700_864_000.0)
    assert res is not None
    ans = res.answer().lower()
    assert "legacy token page" in ans
    assert "new security console" not in ans


# ---------------- derived procedural memory ----------------
def test_search_prefers_source_backed_procedural_memory_for_runbook_queries():
    from mnemotrace.types import Fact

    mem = Memory()
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

    res = mem.search("What is the PAT runbook?", user_id="u1")

    assert res.via == "procedural"
    assert "sessions: pat-runbook" in res.answer()
    assert "security settings" in res.answer()


def test_procedural_memory_can_be_disabled_for_ablation():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    mem = Memory(config=Config(procedural_memory=False, abstain_threshold=2.0))
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

    res = mem.search("What is the PAT runbook?", user_id="u1")

    assert res.abstained
    assert res.via == "abstain"


def test_procedural_extraction_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(procedural_extraction=False))
    mem.add(
        "PAT runbook source: rotate the PAT by opening security settings and updating CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
    )
    mem.consolidate()

    assert [f for f in mem.fact_store.values() if f.predicate == "procedure"] == []


def test_explicit_preference_extraction_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(explicit_preference_extraction=False))
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
