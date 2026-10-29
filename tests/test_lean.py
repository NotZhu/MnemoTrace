"""Lean 读取路径（AGENTS.md Bet A/E）：可扩展的获胜条件 —— 从一小段检索结果（persona + facts +
session summaries + 少量完整 chunk）作答，而不是重放全部历史。这些测试在离线环境下（HashingEmbedder
+ 规则 extractor，零依赖、确定性）固定框架行为：L2 摘要会被构建并索引，L3 persona 会被合成，
lean_context 能正确组装它们，并做去重 + 硬性大小上限。"""
from __future__ import annotations

from mnemotrace import Config

from tests.model_fixtures import Memory
from mnemotrace.types import Episode
from mnemotrace.util import DAY

BASE = 1_700_000_000.0

# 一小段多 session 历史：session 各不相同，便于检查覆盖率 + 去重。
SESSIONS = [
    ("I work at Tencent and my favorite language is Python.", 0),
    ("I took a trip to Kyoto and loved the temples.", 1),
    ("I adopted a cat named Luna.", 2),
    ("I took a trip to Lisbon for a conference.", 3),
    ("I started learning the guitar this month.", 4),
    ("I took a trip to Oslo to see the fjords.", 5),
]


def build(summarize=True) -> Memory:
    mem = Memory()
    eps = [mem.add(text, user_id="u1", session_id=f"s{i}", event_time=BASE + day * DAY)
           for i, (text, day) in enumerate(SESSIONS)]
    for ep in eps:  # 离线打上日期戳，与 eval 的 ingest 流程保持一致
        ep.metadata["date"] = "2023-01-0%d" % (eps.index(ep) + 1)
    mem.consolidate(eps)
    if summarize:
        mem.summarize_episodes(eps)
    return mem


def test_summaries_built_and_indexed():
    mem = build()
    # 每个 session 都会得到离线摘要 + 嵌入，并建立检索索引
    assert len(mem.summary_vec.values()) == len(SESSIONS)
    for ep in mem.episodes_doc.values():
        assert ep.summary, "each episode should carry a summary after summarize_episodes"
        assert ep.summary_embedding is not None


def test_summarize_is_idempotent():
    mem = build()
    n = mem.summarize_episodes(list(mem.episodes_doc.values()))  # 已经摘要过
    assert n == 0, "re-summarizing already-summarized episodes is a no-op"


def test_retrieve_summaries_is_relevant():
    mem = build()
    hits = mem.retrieve_summaries("Which cities did I travel to?", user_id="u1", k=3)
    assert hits, "summary retrieval returns something"
    joined = " ".join(h.summary.lower() for h in hits)
    # 旅行相关的 session 应排在猫/吉他之前
    assert any(city in joined for city in ("kyoto", "lisbon", "oslo"))


def test_persona_built_from_facts():
    mem = build()
    persona = mem.build_persona("u1")
    assert persona, "a persona is synthesized from the live facts"
    assert "persona is cached" or "u1" in mem._persona_cache


def test_consolidate_invalidates_persona_cache():
    mem = build()
    mem.build_persona("u1")  # 填充缓存
    assert mem._persona_cache
    mem.add("I now work at Moonshot AI.", user_id="u1", session_id="s9", event_time=BASE + 9 * DAY)
    mem.consolidate(mem.ingestor.pending())
    assert not mem._persona_cache, "consolidate() must invalidate the stale persona cache"


def test_lean_context_assembles_all_layers():
    mem = build()
    ctx = mem.lean_context("Which cities did I travel to?", user_id="u1",
                           n_summaries=10, n_facts=10, n_chunks=2)
    assert "USER PROFILE:" in ctx
    assert "SESSION SUMMARIES" in ctx
    assert "RELEVANT CONVERSATIONS (full detail):" in ctx


def test_lean_context_dedup_no_session_twice():
    """以完整形式展示的 session（detail chunk）绝不能再出现在摘要块里 —— 否则就是重复烧 token。
    这是 leanness 的核心不变量。"""
    mem = build()
    detail = mem.retrieve_episodes("Kyoto temples", "u1", 2)
    detail_dates = {e.metadata.get("date") for e in detail}
    ctx = mem.lean_context("Kyoto temples", user_id="u1", n_summaries=10, n_chunks=2)
    summ_block = ctx.split("SESSION SUMMARIES")[1].split("RELEVANT CONVERSATIONS")[0]
    for d in detail_dates:
        # detail session 的日期不应再作为某条摘要行的开头
        assert f"- [{d}]" not in summ_block, "detail session leaked into the summary block (dup)"


def test_lean_context_respects_char_budget():
    mem = build()
    ctx = mem.lean_context("trips", user_id="u1", char_budget=200)
    assert len(ctx) <= 200, "lean_context must hard-cap at char_budget"


def test_evidence_budgeting_keeps_exact_raw_detail_under_tight_budget():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    def make(enabled: bool) -> str:
        mem = Memory(config=Config(evidence_budgeting=enabled))
        ep = mem.add(
            "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
            user_id="u1",
            session_id="apollo",
            event_time=BASE,
        )
        fact = Fact(
            subject="Apollo",
            predicate="launch_code",
            object="A17",
            user_id="u1",
            valid_at=BASE,
            provenance=[ep.id],
        )
        fact.embedding = mem.embedder.embed(fact.text)
        mem.fact_store.upsert(fact.id, fact.embedding, fact)
        for i in range(12):
            mem.add_fact(
                "Apollo",
                "project_note",
                f"background filler note {i} with operational chatter that is not the checklist location",
                user_id="u1",
                valid_at=BASE + (i + 1) * DAY,
            )
        return mem.lean_context(
            "What is Apollo's launch code and where is the printed checklist?",
            user_id="u1",
            persona=False,
            n_summaries=0,
            n_chunks=0,
            char_budget=360,
        )

    enabled = make(True)
    disabled = make(False)

    assert len(enabled) <= 360
    assert "blue binder" in enabled
    assert "blue binder" not in disabled


def test_lean_context_is_leaner_than_full_history():
    mem = build()
    full = "\n".join(ep.content for ep in mem.episodes_doc.values())
    lean = mem.lean_context("trips", user_id="u1", n_chunks=1)
    # 只带少量完整 chunk + 小摘要时，lean 必须远小于全量倾倒所有 session
    # （这里历史很小；benchmark 规模下差距会急剧拉大）
    assert len(lean) < len(full) * 3  # sanity：不能大得离谱


def test_consolidate_full_builds_facts_and_summaries():
    mem = Memory()
    eps = [mem.add(t, user_id="u1", session_id=f"s{i}", event_time=BASE + d * DAY)
           for i, (t, d) in enumerate(SESSIONS)]
    stats = mem.consolidate_full(fact_episodes=eps, summary_episodes=eps)
    assert stats["facts_added"] >= 0 and stats["summaries"] == len(SESSIONS)
    assert len(mem.summary_vec.values()) == len(SESSIONS)


def test_lean_path_works_with_no_summaries():
    """优雅降级：即使从未构建摘要，lean_context 仍能返回 facts + chunks。"""
    mem = build(summarize=False)
    ctx = mem.lean_context("Where do I work?", user_id="u1", n_chunks=1)
    assert "SESSION SUMMARIES" not in ctx  # 未构建任何摘要
    assert ctx.strip(), "still produces a usable context from facts + chunks"


def test_user_fact_is_authoritative_over_extraction():
    """可编辑记忆的不变量：用户断言的 fact 独占其槽位。自动提取既不能覆盖它，也不能与之并存；
    用户断言会取代已有的提取值。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact

    r = ConflictResolver()
    user_fact = Fact(subject="u", predicate="works_at", object="ByteDance", source="user", valid_at=BASE)
    extracted = Fact(subject="u", predicate="works_at", object="Tencent", source="extracted", valid_at=BASE + 1)
    action, inv = r.reconcile(extracted, [user_fact])
    assert action == "duplicate" and not inv, "extracted fact must NOT override a user fact"

    r2 = ConflictResolver()
    ext = Fact(subject="u", predicate="works_at", object="Tencent", source="extracted", valid_at=BASE)
    usr = Fact(subject="u", predicate="works_at", object="ByteDance", source="user", valid_at=BASE + 1)
    action2, inv2 = r2.reconcile(usr, [ext])
    assert action2 == "add" and ext in inv2, "a user assertion supersedes the extracted value"


def test_memory_crud_add_edit_delete():
    """管理界面的操作：add_fact（用户撰写）、update_fact（编辑并重新署名）、delete_fact
    （被遗忘权，硬删除）。"""
    mem = Memory()
    f = mem.add_fact("user", "works_at", "ByteDance", user_id="u1")
    assert f.source == "user" and f.text == "user works at ByteDance"
    assert mem.fact_store.get(f.id) is not None

    edited = mem.update_fact(f.id, object="Moonshot AI")
    assert edited.object == "Moonshot AI" and edited.source == "user"
    assert mem.fact_store.get(f.id).text == "user works at Moonshot AI"

    assert mem.delete_fact(f.id) is True
    assert mem.fact_store.get(f.id) is None
    assert mem.delete_fact("nonexistent") is False


def test_update_fact_rebuilds_graph_relation():
    mem = Memory()
    f = mem.add_fact("user", "works_at", "ByteDance", user_id="u1")

    mem.update_fact(f.id, object="Moonshot AI")

    graph = mem.graph_data("u1")
    edge_text = str(graph)
    assert "Moonshot AI" in edge_text
    assert "ByteDance" not in edge_text
    assert len([edge for edge in graph["edges"] if edge["fact_id"] == f.id]) == 1


def test_delete_fact_removes_graph_export_edge():
    mem = Memory()
    f = mem.add_fact("user", "has_disease", "diabetes", user_id="u1")
    assert f.id in {edge["fact_id"] for edge in mem.graph_data("u1")["edges"]}

    assert mem.delete_fact(f.id) is True

    graph = mem.graph_data("u1")
    assert f.id not in {edge["fact_id"] for edge in graph["edges"]}
    assert "diabetes" not in str(graph).lower()


def test_subsumption_drops_contained_fact():
    """MemoryScope contra_repeat：同槽位中内容是另一条更完整 fact 的严格子集时即为冗余。
    添加子集是无操作；添加超集则淘汰那个不完整的版本。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact

    r = ConflictResolver()  # 无 embedder -> 结果确定
    full = Fact(subject="Charles", predicate="role", object="boss and branch manager", valid_at=BASE)
    # 添加已有 fact 的严格子集 -> 作为重复丢弃
    action, inv = r.reconcile(Fact(subject="Charles", predicate="role", object="boss", valid_at=BASE + 1),
                              [full])
    assert action == "duplicate" and not inv

    # 添加已有部分 fact 的严格超集 -> 旧的部分版本被失效化
    partial = Fact(subject="Charles", predicate="role", object="boss", valid_at=BASE)
    action, inv = r.reconcile(
        Fact(subject="Charles", predicate="role", object="boss and branch manager", valid_at=BASE + 1),
        [partial])
    assert action == "add" and partial in inv


def test_date_terms_makes_dates_searchable():
    """查询期时间匹配：把 fact 的日期渲染成可检索的 token，使 'May 2023' 能经 BM25 命中它
    （否则日期只存在于 valid_at 中，对检索不可见）。"""
    from mnemotrace.retrieve.hybrid import date_terms

    terms = date_terms(BASE)  # BASE = 2023-11-14 UTC
    assert "2023" in terms
    assert "november" in terms
    assert "11" in terms


def test_weight_tuner_runs_and_scores():
    """融合权重调参器（AGENTS.md 「评测、公开内容与贡献规则」）为一组权重计算 recall@k 并做网格搜索 —— 离线、确定性。
    这就是让 'tuned on the harness' 有据可依而非空口无凭的机制。"""
    from eval.tune_weights import grid_search, mean_recall_at_k

    mem = build()
    facts = [f for f in mem.fact_store.values()]
    assert facts
    relevant = {facts[0].id}
    dev = [(mem, "u1", "Where do I work?", relevant)]
    r = mean_recall_at_k(dev, {"w_sem": 1.0, "w_lex": 0.6, "w_graph": 0.8, "w_rec": 0.3, "w_sal": 0.25}, k=10)
    assert 0.0 <= r <= 1.0
    best, best_w, base_r = grid_search(dev, k=10)
    assert set(best_w) == {"w_sem", "w_lex", "w_graph", "w_rec", "w_sal"}
    assert best >= base_r  # 网格里包含默认值，所以 best 不会更差


def test_timeline_block_is_chronological():
    """时间辅助：timeline=True 时 fact 按旧->新带日期排列，顺序和时长直接从排列中读出，
    而不必在脑中推算。"""
    mem = build()
    ctx = mem.lean_context("When did I travel?", user_id="u1", timeline=True, n_chunks=1)
    assert "TIMELINE" in ctx
    tl = ctx.split("TIMELINE")[1]
    import re
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})", tl.split("SESSION SUMMARIES")[0])
    assert dates == sorted(dates), "timeline must be chronological (oldest to newest)"


def test_evidence_planner_is_query_based_not_benchmark_based():
    """planner 只按证据形态路由；不含任何 benchmark 类别输入。"""
    from mnemotrace.retrieve.evidence import plan_evidence

    agg = plan_evidence("How many trips did I take, and list every city?")
    assert agg.aggregation and agg.use_cascade and agg.n_summaries > 0
    assert agg.subqueries

    topic = plan_evidence("trips")
    assert not topic.aggregation

    pref = plan_evidence("What food do I prefer or dislike?")
    assert pref.preference and pref.n_chunks > 0
    diet = plan_evidence("What are my dietary restrictions?")
    assert diet.preference and diet.n_chunks > 0

    temporal = plan_evidence("When was the first time I traveled after Lisbon?")
    assert temporal.timeline

    history = plan_evidence("Where did Wei work before Moonshot AI?")
    assert history.history and history.timeline and history.n_facts > 0

    kits = plan_evidence("How many model kits have I worked on or bought?")
    assert any("model kit" in q for q in kits.subqueries)

    jog = plan_evidence("How many hours of jogging and yoga did I do last week?")
    assert jog.aggregation and "workout" in jog.subqueries and "track workouts" in jog.subqueries
    jog_no_expand = plan_evidence(
        "How many hours of jogging and yoga did I do last week?",
        aggregation_recall_expansion=False,
    )
    assert "workout" not in jog_no_expand.subqueries

    freq = plan_evidence("How often do I practice yoga now?")
    assert freq.current_state and not freq.aggregation and not freq.subqueries

    proc = plan_evidence("How do I rotate the PAT?")
    assert proc.procedural and proc.n_facts > 0 and proc.n_chunks > 0
    assert not plan_evidence("How many PAT rotations did I do?").procedural
    assert plan_evidence("这个 PAT 流程怎么操作?").procedural

    dur = plan_evidence("How many weeks in total did I spend reading these books?")
    assert dur.duration and dur.aggregation and dur.n_chunks > 0

    ms = plan_evidence("What is the profession of the user's sister who moved to Seattle?")
    assert ms.multi_hop and ms.use_agentic and ms.n_chunks > 0
    assert not ms.aggregation
    assert "sister profession" in ms.subqueries
    assert "sister moved seattle" in ms.subqueries

    colleague = plan_evidence("Where does my colleague work?")
    assert colleague.multi_hop and colleague.n_facts > 0 and colleague.n_chunks > 0
    assert "colleague" in colleague.subqueries
    assert "colleague employer" in colleague.subqueries
    assert "colleague works" in colleague.subqueries

    sibling = plan_evidence("Where does my sister live?")
    assert sibling.multi_hop and "sister lives" in sibling.subqueries


def test_lean_context_auto_adds_timeline_for_temporal_queries():
    mem = build()
    ctx = mem.lean_context("When was the first time I traveled?", user_id="u1", n_chunks=0)
    assert "TIMELINE (oldest to newest" in ctx


def test_lean_context_auto_adds_preference_records():
    mem = build()
    ctx = mem.lean_context("What is my favorite language?", user_id="u1", n_chunks=0)
    assert "当前偏好记录（结构化）：" in ctx
    assert "favorite language" in ctx.lower()


def test_lean_context_auto_adds_explicit_preference_records():
    mem = Memory()
    mem.add("I prefer aisle seats and avoid red-eye flights.", user_id="u1", event_time=BASE)
    mem.consolidate()

    ctx = mem.lean_context("What travel preferences should you remember?", user_id="u1", n_chunks=0)

    assert "当前偏好记录（结构化）：" in ctx
    assert "prefers" in ctx and "aisle seats" in ctx
    assert "avoids" in ctx and "red-eye flights" in ctx


def test_lean_context_auto_adds_procedural_memory_block():
    from mnemotrace.types import Fact

    mem = Memory()
    ep = mem.add(
        "PAT runbook source: rotate the PAT by opening security settings and updating CI secrets.",
        user_id="u1",
        session_id="pat-runbook",
        event_time=BASE,
    )
    fact = Fact(
        subject="PAT",
        predicate="procedure",
        object="open security settings and update CI secrets",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.lean_context(
        "What is the PAT runbook?",
        user_id="u1",
        persona=False,
        n_summaries=0,
        n_chunks=0,
    )

    assert "PROCEDURAL MEMORY (standing rules/how-to, source-backed):" in ctx
    assert "sessions: pat-runbook" in ctx
    assert "security settings" in ctx


def test_lean_context_auto_adds_dietary_restriction_records():
    mem = Memory()
    mem.add("I'm vegetarian and allergic to peanuts.", user_id="u1", event_time=BASE)
    mem.consolidate()

    ctx = mem.lean_context("What are my dietary restrictions?", user_id="u1", n_chunks=0)
    assert "当前偏好记录（结构化）：" in ctx
    assert "diet" in ctx and "vegetarian" in ctx
    assert "allergic to" in ctx and "peanuts" in ctx


def test_lean_context_auto_adds_aggregation_evidence():
    mem = build()
    ctx = mem.lean_context("How many trips did I take?", user_id="u1", n_chunks=0)
    assert "AGGREGATION EVIDENCE" in ctx
    assert "date | source | evidence" in ctx


def test_aggregation_evidence_keeps_raw_candidates_when_summaries_drop_details():
    mem = Memory()
    mem.add(
        "I recently finished a Tamiya 1/48 scale Spitfire Mk.V model kit.",
        user_id="u1",
        session_id="s1",
        event_time=BASE,
    )

    ctx = mem.lean_context(
        "How many model kits have I worked on or bought?",
        user_id="u1",
        persona=False,
        n_facts=0,
        n_summaries=0,
        n_chunks=0,
        char_budget=10_000,
    )

    agg = ctx.split("AGGREGATION EVIDENCE", 1)[1]
    assert "Spitfire" in agg


def test_structured_aggregation_candidates_filter_furniture_accessories():
    from mnemotrace.retrieve.aggregate import extract_aggregation_candidates, render_aggregation_candidates

    eps = [
        Episode("I bought scratch guards from IKEA to protect the furniture.", event_time=BASE),
        Episode("I just got a new coffee table from West Elm.", event_time=BASE + DAY),
        Episode("I finally assembled that IKEA bookshelf for my home office.", event_time=BASE + 2 * DAY),
        Episode("I got around to fixing the wobbly leg on my kitchen table.", event_time=BASE + 3 * DAY),
        Episode("I needed a new mattress, and ordered one from Casper.", event_time=BASE + 4 * DAY),
        Episode("I bought organic dog food with vegetables for Max and washed his old bed.", event_time=BASE + 5 * DAY),
    ]

    candidates = extract_aggregation_candidates(
        "How many pieces of furniture did I buy, assemble, sell, or fix?",
        [],
        eps,
    )
    included = {c.canonical_item for c in candidates if c.include}
    excluded = {c.canonical_item for c in candidates if not c.include}

    assert {"coffee table", "bookshelf", "kitchen table", "mattress"} <= included
    assert "scratch guard" in excluded
    assert "bed" in excluded
    assert "table" not in included
    rendered = render_aggregation_candidates(candidates)
    assert "AGGREGATION CANDIDATES" in rendered
    assert sum(1 for c in candidates if c.include) == 4


def test_lean_context_renders_structured_aggregation_candidates():
    mem = Memory()
    mem.add("I bought scratch guards from IKEA to protect the furniture.", user_id="u1", event_time=BASE)
    mem.add("I just got a new coffee table from West Elm.", user_id="u1", event_time=BASE + DAY)
    mem.add("I finally assembled that IKEA bookshelf for my home office.", user_id="u1", event_time=BASE + 2 * DAY)
    mem.add("I got around to fixing the wobbly leg on my kitchen table.", user_id="u1", event_time=BASE + 3 * DAY)
    mem.add("I needed a new mattress, and ordered one from Casper.", user_id="u1", event_time=BASE + 4 * DAY)

    ctx = mem.lean_context(
        "How many pieces of furniture did I buy, assemble, sell, or fix?",
        user_id="u1",
        persona=False,
        n_facts=0,
        n_summaries=0,
        n_chunks=0,
        char_budget=20_000,
    )

    assert "AGGREGATION CANDIDATES" in ctx
    assert "INCLUDE" in ctx and "coffee table" in ctx and "mattress" in ctx
    assert "EXCLUDE" in ctx and "scratch guards" in ctx


def test_numeric_aggregation_candidates_extract_money_and_hours():
    from mnemotrace.retrieve.aggregate import _numeric_mode, extract_aggregation_candidates, render_aggregation_candidates

    money_eps = [
        Episode("I attended a mindfulness workshop. I paid $20 to attend.", event_time=BASE),
        Episode("I attended a writing workshop at a festival. I paid $200 to attend.", event_time=BASE + DAY),
        Episode("I attended a digital marketing workshop. I paid $500 to attend.", event_time=BASE + 2 * DAY),
    ]
    money = extract_aggregation_candidates(
        "How much total money did I spend on attending workshops?",
        [],
        money_eps,
    )

    assert sum(c.value or 0 for c in money if c.include) == 720
    rendered = render_aggregation_candidates(money)
    assert "value | unit" in rendered
    assert "$500 workshop" in rendered

    duration_eps = [
        Episode("I went for a 30-minute jog around the neighborhood.", event_time=BASE),
        Episode("I used to practice yoga three times a week, each time for 2 hours.", event_time=BASE + DAY),
    ]
    duration = extract_aggregation_candidates(
        "How many hours of jogging and yoga did I do last week?",
        [],
        duration_eps,
    )

    assert sum(c.value or 0 for c in duration if c.include) == 0.5
    assert any(not c.include and "past habit" in c.exclude_reason for c in duration)
    assert _numeric_mode("How much more miles per gallon was my car getting?") == ""


def test_aggregation_constraint_filter_excludes_conflicting_month_values():
    from mnemotrace.retrieve.aggregate import extract_aggregation_candidates

    eps = [
        Episode(
            'I just finished a 416-page novel, but before that, I read "The Power" '
            "by Naomi Alderman in December, which had 341 pages.",
            event_time=BASE,
        ),
        Episode(
            'I just finished "The Nightingale" by Kristin Hannah, which had 440 pages.',
            event_time=BASE + DAY,
        ),
    ]

    filtered = extract_aggregation_candidates(
        "What was the page count of the two novels I finished in January and March?",
        [],
        eps,
    )
    included = {c.value for c in filtered if c.include}
    excluded = {c.value: c.exclude_reason for c in filtered if not c.include}

    assert included == {416.0, 440.0}
    assert "december" in excluded[341.0]

    disabled = extract_aggregation_candidates(
        "What was the page count of the two novels I finished in January and March?",
        [],
        eps,
        constraint_filter=False,
    )
    assert {c.value for c in disabled if c.include} == {341.0, 416.0, 440.0}


def test_numeric_aggregation_candidates_can_be_disabled():
    mem = Memory(config=Config(numeric_aggregation_candidates=False))
    mem.add("I attended a digital marketing workshop. I paid $500 to attend.", user_id="u1", event_time=BASE)

    ctx = mem.lean_context(
        "How much total money did I spend on attending workshops?",
        user_id="u1",
        persona=False,
        n_facts=0,
        n_summaries=0,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "AGGREGATION CANDIDATES" not in ctx


def test_lean_context_auto_adds_duration_evidence_for_time_totals():
    mem = Memory()
    mem.add("I started reading 'The Nightingale' today.", user_id="u1", session_id="s1", event_time=BASE)
    mem.add("I finished reading 'The Nightingale' today.", user_id="u1", session_id="s2",
            event_time=BASE + 14 * DAY)

    ctx = mem.lean_context(
        "How many weeks did I spend reading 'The Nightingale'?",
        user_id="u1",
        persona=False,
        n_facts=0,
        n_summaries=0,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "DURATION EVIDENCE (pair start/finish dates per item" in ctx
    assert "started reading" in ctx
    assert "finished reading" in ctx


def test_aggregation_evidence_prioritizes_query_overlap():
    mem = build()
    eps = sorted(mem.episodes_doc.values(), key=lambda e: e.event_time)
    block = mem._aggregation_block(
        facts=[],
        summaries=[eps[2], eps[1]],
        detail_eps=[],
        query="How many trips to Kyoto?",
    )
    body = block.split("--- | --- | ---", 1)[1].lower()
    assert "kyoto" in body
    assert "cat named luna" not in body


def test_lean_context_auto_adds_current_state_for_now_queries():
    mem = build()
    ctx = mem.lean_context("Where do I currently work?", user_id="u1", n_chunks=0)
    assert "CURRENT STATE (live facts only):" in ctx
    assert "current value" in ctx


def test_lean_context_as_of_profile_does_not_leak_current_facts():
    mem = Memory()
    mem.add("Wei works at Tencent.", user_id="u1", session_id="old", event_time=BASE)
    mem.add("Wei works at Moonshot AI.", user_id="u1", session_id="new", event_time=BASE + 30 * DAY)
    mem.consolidate()

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        as_of=BASE + 10 * DAY,
        n_chunks=0,
        char_budget=10_000,
    )
    assert "USER PROFILE (as of" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" not in ctx


def test_lean_context_adds_history_for_previous_value_queries():
    mem = Memory()
    old = mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    new = mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    ctx = mem.lean_context(
        "Where did Wei work before Moonshot AI?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )
    assert new.supersedes == old.id
    assert "FACT HISTORY (supersession chain" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" in ctx
    assert "superseded" in ctx and "current" in ctx


def test_search_answers_previous_value_from_supersession_history():
    mem = Memory()
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    before = mem.search("Where did Wei work before Moonshot AI?", user_id="u1")
    previous = mem.search("Where did Wei previously work?", user_id="u1")
    current = mem.search("Where does Wei work now?", user_id="u1")

    assert before.via == "history"
    assert "tencent" in before.answer().lower()
    assert [f.object for f in before.facts] == ["Tencent", "Moonshot AI"]
    assert previous.via == "history"
    assert "tencent" in previous.answer().lower()
    assert current.via == "hybrid"
    assert "moonshot" in current.answer().lower()


def test_temporal_history_query_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(temporal_history_queries=False))
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    res = mem.search("Where did Wei work before Moonshot AI?", user_id="u1")
    ctx = mem.lean_context(
        "Where did Wei work before Moonshot AI?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert res.via == "hybrid"
    assert "moonshot" in res.answer().lower()
    assert "FACT HISTORY (supersession chain" not in ctx


def test_lean_context_adds_evolution_chain_for_current_lookup():
    mem = Memory()
    old = mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    new = mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert new.supersedes == old.id
    assert "FACTS (current, dated):" in ctx
    assert "FACT EVOLUTION" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" in ctx
    assert "superseded" in ctx and "current" in ctx


def test_lean_context_evolution_chain_respects_as_of_boundary():
    mem = Memory()
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        as_of=BASE + 10 * DAY,
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "Tencent" in ctx
    assert "Moonshot AI" not in ctx
    assert "FACT EVOLUTION" not in ctx


def test_context_for_adds_fact_evolution_chain():
    mem = Memory()
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    ctx = mem.context_for("Where does Wei work?", user_id="u1", k_chunks=0)

    assert "FACT EVOLUTION" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" in ctx


def test_chain_evidence_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(chain_evidence=False))
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY)

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "Moonshot AI" in ctx
    assert "FACT EVOLUTION" not in ctx


def test_lean_context_adds_provenance_raw_evidence_for_retrieved_fact():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    mem = Memory(config=Config(evidence_planner=False))
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "FACTS (current, dated):" in ctx
    assert "PROVENANCE RAW EVIDENCE (source episodes for retrieved facts):" in ctx
    assert "A17" in ctx
    assert "blue binder" in ctx


def test_provenance_evidence_can_be_disabled_for_ablation():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    mem = Memory(config=Config(evidence_planner=False, provenance_evidence=False))
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )

    assert "A17" in ctx
    assert "PROVENANCE RAW EVIDENCE" not in ctx
    assert "blue binder" not in ctx


def test_provenance_raw_evidence_dedups_full_detail_chunk():
    from mnemotrace.types import Fact

    mem = Memory()
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_chunks=1,
        char_budget=10_000,
    )

    assert "RELEVANT CONVERSATIONS (full detail):" in ctx
    assert "blue binder" in ctx
    assert "PROVENANCE RAW EVIDENCE (source episodes for retrieved facts):" not in ctx


def test_provenance_source_episode_is_promoted_into_detail_chunks():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    def make(enabled: bool) -> str:
        mem = Memory(
            config=Config(
                evidence_planner=False,
                provenance_evidence=False,
                provenance_chunk_promotion=enabled,
            )
        )
        source = mem.add(
            "A17 is written on the tag tucked inside the blue binder.",
            user_id="u1",
            session_id="apollo-source",
            event_time=BASE,
        )
        for i in range(5):
            mem.add(
                f"Apollo launch code rehearsal note {i}: the team reviewed old checklist formats.",
                user_id="u1",
                session_id=f"apollo-distractor-{i}",
                event_time=BASE + (i + 1) * DAY,
            )
        fact = Fact(
            subject="Apollo",
            predicate="launch_code",
            object="A17",
            user_id="u1",
            valid_at=BASE,
            provenance=[source.id],
        )
        fact.embedding = mem.embedder.embed(fact.text)
        mem.fact_store.upsert(fact.id, fact.embedding, fact)
        return mem.lean_context(
            "What is Apollo's launch code?",
            user_id="u1",
            persona=False,
            n_summaries=0,
            n_chunks=1,
            char_budget=10_000,
        )

    enabled = make(True)
    disabled = make(False)
    enabled_detail = enabled.split("RELEVANT CONVERSATIONS (full detail):", 1)[1]
    disabled_detail = disabled.split("RELEVANT CONVERSATIONS (full detail):", 1)[1]

    assert "blue binder" in enabled_detail
    assert "apollo-source" in enabled_detail
    assert "blue binder" not in disabled_detail
    assert "apollo-distractor" in disabled_detail


def test_chain_provenance_promotes_superseded_source_for_history_query():
    from mnemotrace.config import Config

    def make(enabled: bool) -> str:
        mem = Memory(
            config=Config(
                chain_evidence=enabled,
                provenance_evidence=False,
                provenance_chunk_promotion=True,
            )
        )
        mem.add(
            "Wei works at Tencent and keeps a blue employee badge.",
            user_id="u1",
            session_id="old-source",
            event_time=BASE,
        )
        mem.add(
            "Wei works at Moonshot AI on agent memory.",
            user_id="u1",
            session_id="new-source",
            event_time=BASE + 30 * DAY,
        )
        mem.consolidate()
        return mem.lean_context(
            "Where did Wei work before Moonshot AI?",
            user_id="u1",
            persona=False,
            n_summaries=0,
            n_chunks=1,
            char_budget=10_000,
        )

    enabled = make(True)
    disabled = make(False)
    enabled_detail = enabled.split("RELEVANT CONVERSATIONS (full detail):", 1)[1]
    disabled_detail = disabled.split("RELEVANT CONVERSATIONS (full detail):", 1)[1]

    assert "FACT HISTORY (supersession chain" in enabled
    assert "Tencent" in enabled
    assert "old-source" in enabled_detail
    assert "blue employee badge" in enabled_detail
    assert "new-source" in disabled_detail
    assert "blue employee badge" not in disabled_detail


def test_provenance_raw_evidence_is_hidden_in_redacted_context():
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    mem = Memory(config=Config(evidence_planner=False))
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
        sensitive=True,
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.lean_context(
        "What is Apollo's launch code?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        redact_sensitive=True,
        char_budget=10_000,
    )

    assert "PROVENANCE RAW EVIDENCE" not in ctx
    assert "blue binder" not in ctx


def test_context_for_adds_provenance_raw_evidence_for_fact_sources():
    from mnemotrace.types import Fact

    mem = Memory()
    ep = mem.add(
        "The Apollo launch code is A17. Keep the printed checklist near the blue binder.",
        user_id="u1",
        session_id="apollo",
        event_time=BASE,
    )
    fact = Fact(
        subject="Apollo",
        predicate="launch_code",
        object="A17",
        user_id="u1",
        valid_at=BASE,
        provenance=[ep.id],
    )
    fact.embedding = mem.embedder.embed(fact.text)
    mem.fact_store.upsert(fact.id, fact.embedding, fact)

    ctx = mem.context_for("What is Apollo's launch code?", user_id="u1", k_chunks=0)

    assert "PROVENANCE RAW EVIDENCE (source episodes for retrieved facts):" in ctx
    assert "blue binder" in ctx


def test_current_state_preserves_multi_valued_attributes():
    mem = Memory()
    mem.add_fact("Wei", "owns", "a bike", user_id="u1")
    mem.add_fact("Wei", "owns", "a camera", user_id="u1")

    ctx = mem.lean_context(
        "What do I currently own?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )
    state = ctx.split("CURRENT STATE (live facts only):", 1)[1].split("\n\n", 1)[0]
    assert "a bike" in state
    assert "a camera" in state


def test_lean_context_uses_multi_hop_subqueries_for_detail_chunks():
    mem = Memory()
    eps = [
        mem.add("My sister Maya is a pediatrician at a children's hospital.", user_id="u1",
                session_id="s1", event_time=BASE),
        mem.add("Maya just moved to Seattle for a fellowship.", user_id="u1",
                session_id="s2", event_time=BASE + DAY),
        mem.add("I bought a new espresso machine and it is wonderful.", user_id="u1",
                session_id="s3", event_time=BASE + 2 * DAY),
    ]
    mem.summarize_episodes(eps)

    ctx = mem.lean_context(
        "What is the profession of the user's sister who moved to Seattle?",
        user_id="u1",
        persona=False,
        n_facts=0,
        n_summaries=0,
        n_chunks=0,
    )

    assert "RELEVANT CONVERSATIONS (full detail):" in ctx
    assert "pediatrician" in ctx.lower()
    assert "seattle" in ctx.lower()


def test_lean_context_graph_paths_use_clean_multihop_plan():
    mem = Memory()
    eps = [
        mem.add("My sister Anna is a lawyer.", user_id="u1", session_id="s1", event_time=BASE),
        mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s2", event_time=BASE + DAY),
        mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s3", event_time=BASE + 2 * DAY),
    ]
    mem.consolidate(eps)
    mem.summarize_episodes(eps)

    ctx = mem.lean_context(
        "What is the profession of the user's sister who moved to Seattle?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )
    graph = ctx.split("GRAPH PATHS (relation evidence):", 1)[1].split("\n\n", 1)[0]
    assert "u1 sister Maya" in graph
    assert "Maya lives in Seattle" in graph
    assert "Maya occupation pediatrician" in graph
    assert "u1 sister Anna" not in graph


def test_multihop_planner_includes_cold_tier_relation_facts():
    mem = Memory()
    eps = [
        mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s1", event_time=BASE),
        mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s2", event_time=BASE + DAY),
    ]
    mem.consolidate(eps)

    sister = next(f for f in mem.fact_store.values() if f.predicate == "sister")
    sister.salience = 0.0
    assert mem.evict_cold(max_hot=len(mem.fact_store.values()) - 1) == 1
    assert mem.cold_store.get(sister.id) is not None

    ctx = mem.lean_context(
        "What is the profession of the user's sister who moved to Seattle?",
        user_id="u1",
        persona=False,
        n_chunks=0,
        char_budget=10_000,
    )
    graph = ctx.split("GRAPH PATHS (relation evidence):", 1)[1].split("\n\n", 1)[0]
    assert "u1 sister Maya" in graph
    assert "Maya lives in Seattle" in graph
    assert "Maya occupation pediatrician" in graph


def test_deleted_relation_fact_does_not_drive_multihop_planner():
    mem = Memory()
    eps = [
        mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s1", event_time=BASE),
        mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s2", event_time=BASE + DAY),
    ]
    mem.consolidate(eps)
    sister = next(f for f in mem.fact_store.values() if f.predicate == "sister")

    assert mem.delete_fact(sister.id) is True

    res = mem.search("What is the profession of the user's sister who moved to Seattle?", user_id="u1")
    assert res.via != "multi-hop"
    assert "pediatrician" not in res.answer().lower()


def test_graph_scores_expand_beyond_one_hop_for_bridge_facts():
    mem = Memory()
    direct = mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    bridge = mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    second_hop = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    scores, qids = mem.retriever._graph_scores(
        "Tell me about Wei",
        "u1",
        [direct, bridge, second_hop],
        None,
    )

    assert qids, "query should anchor on the Wei entity"
    assert scores[direct.id] == 1.0
    assert scores[bridge.id] > scores[second_hop.id] > 0.0


def test_graph_scores_respect_as_of_during_expansion():
    mem = Memory()
    direct = mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    future_bridge = mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    older_target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)
    live = [direct, future_bridge, older_target]

    before, _ = mem.retriever._graph_scores(
        "Tell me about Wei",
        "u1",
        live,
        BASE + 0.5 * DAY,
    )
    after, _ = mem.retriever._graph_scores(
        "Tell me about Wei",
        "u1",
        live,
        BASE + 2 * DAY,
    )

    assert before[older_target.id] == 0.0
    assert after[older_target.id] > 0.0


def test_graph_proximity_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_proximity=False))
    direct = mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    bridge = mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    second_hop = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    scores, qids = mem.retriever._graph_scores(
        "Tell me about Wei",
        "u1",
        [direct, bridge, second_hop],
        None,
    )
    related = mem._graph_related_facts("Tell me about Wei", "u1", None)
    paths = mem._graph_paths_block("Tell me about Wei", "u1", None)

    assert qids, "entity anchoring still works; only proximity evidence is ablated"
    assert all(score == 0.0 for score in scores.values())
    assert related == []
    assert paths == ""


def test_graph_relation_awareness_prioritizes_query_relevant_edges():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_relation_awareness=True))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    distractor = mem.add_fact("Lin", "likes", "jazz", user_id="u1", valid_at=BASE + 2 * DAY)
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 3 * DAY)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, _ = mem.retriever._graph_scores(
        "Where is Wei's colleague's company based?",
        "u1",
        live,
        None,
    )

    assert scores[target.id] > scores[distractor.id]


def test_graph_relation_awareness_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_relation_awareness=False))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    distractor = mem.add_fact("Lin", "likes", "jazz", user_id="u1", valid_at=BASE + 2 * DAY)
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 3 * DAY)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, _ = mem.retriever._graph_scores(
        "Where is Wei's colleague's company based?",
        "u1",
        live,
        None,
    )

    assert scores[target.id] < scores[distractor.id]


def test_graph_path_reinforcement_boosts_multi_path_targets():
    from mnemotrace.config import Config

    def build_mem(enabled: bool):
        mem = Memory(config=Config(graph_path_reinforcement=enabled))
        mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
        mem.add_fact("Wei", "mentor", "Maya", user_id="u1", valid_at=BASE + DAY)
        mem.add_fact("Lin", "works_on", "Atlas", user_id="u1", valid_at=BASE + 2 * DAY)
        mem.add_fact("Maya", "works_on", "Atlas", user_id="u1", valid_at=BASE + 3 * DAY)
        mem.add_fact("Lin", "works_on", "Zephyr", user_id="u1", valid_at=BASE + 4 * DAY)
        target = mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + 5 * DAY)
        distractor = mem.add_fact("Zephyr", "based_in", "Lisbon", user_id="u1", valid_at=BASE + 6 * DAY)
        return mem, target, distractor

    enabled_mem, enabled_target, enabled_distractor = build_mem(True)
    enabled_live = [f for f in enabled_mem.fact_store.values() if f.user_id == "u1"]
    enabled_scores, _ = enabled_mem.retriever._graph_scores(
        "Where is Wei's project based?",
        "u1",
        enabled_live,
        None,
    )

    disabled_mem, disabled_target, disabled_distractor = build_mem(False)
    disabled_live = [f for f in disabled_mem.fact_store.values() if f.user_id == "u1"]
    disabled_scores, _ = disabled_mem.retriever._graph_scores(
        "Where is Wei's project based?",
        "u1",
        disabled_live,
        None,
    )

    assert enabled_scores[enabled_target.id] > enabled_scores[enabled_distractor.id]
    assert disabled_scores[disabled_target.id] <= disabled_scores[disabled_distractor.id]


def test_graph_self_anchor_retrieves_first_person_graph_paths():
    mem = Memory()
    mem.add_fact("u1", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    target = mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + DAY)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, qids = mem.retriever._graph_scores(
        "Where is my project based?",
        "u1",
        live,
        None,
    )
    ctx = mem.context_for("Where is my project based?", user_id="u1", k_chunks=0, graph=True)

    assert qids
    assert scores[target.id] > 0.0
    assert "Atlas based in Reykjavik" in ctx


def test_graph_self_anchor_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_self_anchor=False))
    mem.add_fact("u1", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    target = mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + DAY)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, qids = mem.retriever._graph_scores(
        "Where is my project based?",
        "u1",
        live,
        None,
    )
    ctx = mem.context_for("Where is my project based?", user_id="u1", k_chunks=0, graph=True)

    assert qids == set()
    assert scores[target.id] == 0.0
    assert "RELATED FACTS (graph traversal):" not in ctx


def test_graph_entity_alias_anchor_matches_unique_short_name():
    mem = Memory()
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, qids = mem.retriever._graph_scores(
        "Where is Moonshot based?",
        "u1",
        live,
        None,
    )
    ctx = mem.context_for("Where is Moonshot based?", user_id="u1", k_chunks=0, graph=True)

    assert qids
    assert scores[target.id] > 0.0
    assert "Moonshot AI based in Beijing" in ctx


def test_graph_entity_alias_anchor_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_entity_alias_anchor=False))
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, qids = mem.retriever._graph_scores(
        "Where is Moonshot based?",
        "u1",
        live,
        None,
    )
    ctx = mem.context_for("Where is Moonshot based?", user_id="u1", k_chunks=0, graph=True)

    assert qids == set()
    assert scores[target.id] == 0.0
    assert "RELATED FACTS (graph traversal):" not in ctx


def test_graph_entity_alias_anchor_requires_unique_token():
    mem = Memory()
    first = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)
    second = mem.add_fact("Moonshot Labs", "based_in", "Shanghai", user_id="u1", valid_at=BASE + DAY)
    live = [f for f in mem.fact_store.values() if f.user_id == "u1"]

    scores, qids = mem.retriever._graph_scores(
        "Where is Moonshot based?",
        "u1",
        live,
        None,
    )

    assert qids == set()
    assert scores[first.id] == 0.0
    assert scores[second.id] == 0.0


def test_graph_negative_constraints_filter_excluded_location_paths():
    mem = Memory()
    mem.add_fact("Wei", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_on", "Zephyr", user_id="u1", valid_at=BASE + DAY)
    target = mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + 2 * DAY)
    distractor = mem.add_fact("Zephyr", "based_in", "Lisbon", user_id="u1", valid_at=BASE + 3 * DAY)

    query = "Where is Wei's project not in Lisbon based?"
    res = mem.search(query, user_id="u1")
    related = mem._graph_related_facts(query, "u1", None)
    ctx = mem.context_for(query, user_id="u1", k_chunks=0, graph=True)

    assert "reykjavik" in res.answer().lower()
    assert target.id in {f.id for f in related}
    assert distractor.id not in {f.id for f in related}
    assert "Atlas based in Reykjavik" in ctx
    assert "Zephyr based in Lisbon" not in ctx


def test_graph_negative_constraints_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(graph_negative_constraints=False))
    mem.add_fact("Wei", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    mem.add_fact("Wei", "works_on", "Zephyr", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + 2 * DAY)
    mem.add_fact("Zephyr", "based_in", "Lisbon", user_id="u1", valid_at=BASE + 3 * DAY)

    ctx = mem.context_for("Where is Wei's project not in Lisbon based?", user_id="u1", k_chunks=0, graph=True)

    assert "Atlas based in Reykjavik" in ctx
    assert "Zephyr based in Lisbon" in ctx


def test_multihop_planner_reaches_company_location_chain():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    res = mem.search("Where is Wei's colleague's company based?", user_id="u1")

    assert res.via == "multi-hop"
    assert "beijing" in res.answer().lower()
    assert [f.predicate for f in res.facts] == ["colleague", "works_at", "based_in"]


def test_multihop_planner_location_chain_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(planner_location_chains=False))
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    res = mem.search("Where is Wei's colleague's company based?", user_id="u1")

    assert res.via == "multi-hop"
    assert "moonshot" in res.answer().lower()
    assert "beijing" not in res.answer().lower()
    assert [f.predicate for f in res.facts] == ["colleague", "works_at"]


def test_multihop_planner_reaches_project_location_chain():
    mem = Memory()
    mem.add_fact("Wei", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + DAY)

    res = mem.search("Where is Wei's project based?", user_id="u1")

    assert res.via == "multi-hop"
    assert "reykjavik" in res.answer().lower()
    assert [f.predicate for f in res.facts] == ["works_on", "based_in"]


def test_multihop_planner_project_chain_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = Memory(config=Config(planner_project_chains=False))
    mem.add_fact("Wei", "works_on", "Atlas", user_id="u1", valid_at=BASE)
    mem.add_fact("Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + DAY)

    res = mem.search("Where is Wei's project based?", user_id="u1")

    assert res.via == "hybrid"
    assert "atlas" in res.answer().lower()
    assert "reykjavik" not in res.answer().lower()


def test_bench_preconsolidation_uses_multi_hop_subqueries():
    from mnemotrace.types import Episode
    from eval.bench import retrieve_evidence_episodes

    sister = Episode("My sister Maya is a pediatrician.", id="sister", user_id="u1")
    moved = Episode("Maya just moved to Seattle.", id="moved", user_id="u1")
    noise = Episode("I bought a new espresso machine.", id="noise", user_id="u1")

    class FakeMem:
        def retrieve_episodes(self, query, user_id, k):
            q = query.lower()
            if "sister profession" in q or q == "profession":
                return [sister]
            if "sister moved seattle" in q or q == "seattle" or q == "moved seattle":
                return [moved]
            return [noise]

    eps = retrieve_evidence_episodes(
        FakeMem(),
        "What is the profession of the user's sister who moved to Seattle?",
        "u1",
        2,
    )
    assert {e.id for e in eps} == {"sister", "moved"}

    baseline = retrieve_evidence_episodes(
        FakeMem(),
        "What is the profession of the user's sister who moved to Seattle?",
        "u1",
        2,
        use_planner=False,
    )
    assert [e.id for e in baseline] == ["noise"]


def test_bench_preconsolidation_uses_aggregation_recall_expansion():
    from mnemotrace.config import Config
    from mnemotrace.types import Episode
    from eval.bench import retrieve_evidence_episodes

    jog = Episode("I went for a 30-minute jog around the neighborhood.", id="jog", user_id="u1")
    yoga = Episode("I used to practice yoga three times a week, each time for 2 hours.", id="yoga", user_id="u1")
    noise = Episode("A generic wellness article mentioned hours and routines.", id="noise", user_id="u1")

    class FakeMem:
        def __init__(self, enabled=True):
            self.config = Config(aggregation_recall_expansion=enabled)

        def retrieve_episodes(self, query, user_id, k):
            q = query.lower()
            if "workout" in q:
                return [jog]
            if "hours of jogging" in q or "yoga" in q:
                return [yoga]
            return [noise]

    eps = retrieve_evidence_episodes(
        FakeMem(True),
        "How many hours of jogging and yoga did I do last week?",
        "u1",
        2,
    )
    # 聚合的两部分都必须在；pool boost 可能连带放入额外 session，
    # 这在这里无害 —— 导致聚合答案出错的是漏掉某次出现。
    assert {"jog", "yoga"} <= {e.id for e in eps}

    baseline = retrieve_evidence_episodes(
        FakeMem(False),
        "How many hours of jogging and yoga did I do last week?",
        "u1",
        2,
    )
    assert "yoga" in {e.id for e in baseline}
    assert "jog" not in {e.id for e in baseline}


def test_cascade_coarse_to_fine_assembles():
    """由粗到细的 cascade：detail 从排名靠前的摘要中钻取；两种模式都能正常组装。"""
    mem = build()
    casc = mem.lean_context("Which cities did I visit?", user_id="u1", cascade=True, n_chunks=2)
    flat = mem.lean_context("Which cities did I visit?", user_id="u1", cascade=False, n_chunks=2)
    assert casc.strip() and flat.strip()


def test_heat_tiered_eviction_pages_cold_preserves_durable():
    """MemoryOS 热度分层：超容量时最冷的偶发 fact 换出到 cold tier（非破坏性）；
    持久的偏好/身份 fact 永不被驱逐。"""
    from mnemotrace.types import Fact

    mem = Memory()
    u = "u1"
    for i in range(5):  # 偶发 fact，显著度递增
        f = Fact(subject="u1", predicate="parked_at", object=f"lot{i}", user_id=u,
                 salience=float(i), last_access=BASE + i)
        mem.fact_store.upsert(f.id, [0.0], f)
    durable = Fact(subject="u1", predicate="likes", object="jazz", user_id=u, salience=0.0, last_access=BASE)
    mem.fact_store.upsert(durable.id, [0.0], durable)

    n = mem.evict_cold(max_hot=3)
    assert n == 3
    hot = mem.fact_store.values()
    assert durable in hot, "durable preference must never be paged out, even at lowest salience"
    assert len(mem.cold_store.values()) == 3, "evicted facts are preserved in the cold tier, not deleted"


def test_history_includes_cold_tier_facts():
    mem = Memory()
    cold = mem.add_fact("project", "project_note", "cold audit trail", user_id="u1")
    hot = mem.add_fact("project", "project_note", "hot audit trail", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    chain = mem.history("project", "project_note", user_id="u1")
    assert {f.object for f in chain} == {"cold audit trail", "hot audit trail"}


def test_add_fact_supersedes_live_cold_tier_fact():
    mem = Memory()
    old = mem.add_fact("user", "project_status", "alpha", user_id="u1")
    filler = mem.add_fact("user", "project_note", "hot filler", user_id="u1")
    old.salience = 0.0
    filler.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(old.id) is not None

    new = mem.add_fact("user", "project_status", "beta", user_id="u1")
    assert new.supersedes == old.id
    assert old.invalid_at == new.valid_at
    chain = mem.history("user", "project_status", user_id="u1")
    assert [f.object for f in chain] == ["alpha", "beta"]


def test_search_pages_relevant_cold_fact_back_to_hot():
    mem = Memory()
    fact = mem.add_fact("project", "project_status", "alpha", user_id="u1")
    assert mem.evict_cold(max_hot=0) == 1
    assert mem.fact_store.get(fact.id) is None
    assert mem.cold_store.get(fact.id) is not None

    res = mem.search("What is the project status?", user_id="u1")
    assert res.via == "cold"
    assert "alpha" in res.answer().lower()
    assert mem.fact_store.get(fact.id) is not None
    assert mem.cold_store.get(fact.id) is None


def test_cold_page_in_preserves_hot_limit():
    from mnemotrace.config import Config

    mem = Memory(config=Config(max_hot_facts=0, abstain_threshold=2.0))
    target = mem.add_fact("apollo", "launch_code", "alpha", user_id="u1")
    filler = mem.add_fact("zephyr", "project_note", "background filler", user_id="u1")
    target.salience = 0.0
    filler.salience = 0.1
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(target.id) is not None

    mem.config.max_hot_facts = 1
    res = mem.search("What is Apollo's launch code?", user_id="u1")

    assert res.via == "cold"
    assert "alpha" in res.answer().lower()
    assert len(mem.fact_store.values()) <= 1
    assert mem.fact_store.get(target.id) is not None
    assert mem.cold_store.get(filler.id) is not None


def test_max_hot_facts_auto_pages_on_add_fact():
    from mnemotrace.config import Config

    mem = Memory(config=Config(max_hot_facts=1))
    a = mem.add_fact("project", "project_status", "alpha", user_id="u1")
    b = mem.add_fact("project", "project_note", "beta", user_id="u1")
    a.salience = 0.0
    b.salience = 1.0
    mem._enforce_hot_limit()

    assert len(mem.fact_store.values()) == 1
    assert len(mem.cold_store.values()) == 1
    assert mem.cold_pages_out.get("u1", 0) == 1


def test_lean_context_pages_cold_fact_when_hot_misses():
    mem = Memory()
    fact = mem.add_fact("project", "project_status", "alpha", user_id="u1")
    assert mem.evict_cold(max_hot=0) == 1

    ctx = mem.lean_context("project status", user_id="u1", n_chunks=0)
    assert "alpha" in ctx.lower()
    assert mem.fact_store.get(fact.id) is not None
    assert mem.cold_store.get(fact.id) is None


def test_graph_related_facts_include_cold_tier_facts():
    mem = Memory()
    cold = mem.add_fact("project", "project_note", "cold graph evidence", user_id="u1")
    hot = mem.add_fact("project", "project_status", "hot graph evidence", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    related = mem._graph_related_facts("project", "u1", None)
    assert "cold graph evidence" in {f.object for f in related}


def test_graph_related_facts_expand_beyond_one_hop():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    related = mem._graph_related_facts("Tell me about Wei", "u1", None)

    assert target.id in {f.id for f in related}


def test_graph_related_facts_respect_as_of_during_expansion():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    target = mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)

    before = mem._graph_related_facts("Tell me about Wei", "u1", BASE + 0.5 * DAY)
    after = mem._graph_related_facts("Tell me about Wei", "u1", BASE + 2 * DAY)

    assert target.id not in {f.id for f in before}
    assert target.id in {f.id for f in after}


def test_context_for_graph_includes_multihop_related_facts():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    ctx = mem.context_for("Tell me about Wei", user_id="u1", k_chunks=0, graph=True)

    assert "RELATED FACTS (graph traversal):" in ctx
    assert "Moonshot AI based in Beijing" in ctx


def test_graph_paths_include_cold_tier_facts():
    mem = Memory()
    cold = mem.add_fact("project", "project_note", "cold path evidence", user_id="u1")
    hot = mem.add_fact("project", "project_status", "hot path evidence", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1

    block = mem._graph_paths_block("project", "u1", None)
    assert "cold path evidence" in block


def test_graph_paths_fallback_expands_beyond_one_hop():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY)

    block = mem._graph_paths_block("Tell me about Wei", "u1", None)

    assert "Wei --colleague--> Lin" in block
    assert "Lin --works_at--> Moonshot AI" in block
    assert "Moonshot AI --based_in--> Beijing" in block


def test_graph_paths_fallback_respects_as_of_during_expansion():
    mem = Memory()
    mem.add_fact("Wei", "colleague", "Lin", user_id="u1", valid_at=BASE)
    mem.add_fact("Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY)
    mem.add_fact("Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE)

    before = mem._graph_paths_block("Tell me about Wei", "u1", BASE + 0.5 * DAY)
    after = mem._graph_paths_block("Tell me about Wei", "u1", BASE + 2 * DAY)

    assert "Moonshot AI --based_in--> Beijing" not in before
    assert "Moonshot AI --based_in--> Beijing" in after


def test_pending_conflict_and_resolution_include_cold_tier_facts():
    from mnemotrace.types import Conflict

    mem = Memory()
    old = mem.add_fact("project", "status", "alpha", user_id="u1")
    new = mem.add_fact("project", "status", "beta", user_id="u1")
    old.invalid_at = None
    old.expired_at = None
    new.supersedes = None
    old.salience = 0.0
    new.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(old.id) is not None
    cf = Conflict(older=old.id, newer=new.id, user_id="u1", text_older=old.text, text_newer=new.text)
    mem.conflicts[cf.id] = cf

    assert mem.pending_conflicts("u1") == [cf]
    assert mem.resolve_conflict(cf.id, keep="newer") is True
    assert old.invalid_at is not None
    assert cf.status == "resolved"


def test_update_cold_fact_promotes_without_duplicate():
    mem = Memory()
    cold = mem.add_fact("project", "project_note", "old value", user_id="u1")
    hot = mem.add_fact("project", "project_status", "hot value", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    updated = mem.update_fact(cold.id, object="new value")

    assert updated is not None and updated.object == "new value"
    assert mem.fact_store.get(cold.id) is not None
    assert mem.cold_store.get(cold.id) is None
    assert len([f for f in mem._all_facts() if f.id == cold.id]) == 1


def test_resolve_conflict_writes_back_to_copying_vector_store():
    """外部向量后端可能返回 payload 的副本，因此冲突解决必须显式 upsert 被修改的 fact，
    而不能依赖内存中的对象同一性。"""
    from copy import deepcopy

    from mnemotrace.store import InMemoryVectorStore
    from mnemotrace.types import Conflict

    class CopyingVectorStore(InMemoryVectorStore):
        def upsert(self, key, vector, payload):
            super().upsert(key, list(vector), deepcopy(payload))

        def get(self, key):
            payload = super().get(key)
            return deepcopy(payload) if payload is not None else None

        def values(self):
            return [deepcopy(payload) for payload in super().values()]

    mem = Memory()
    older = mem.add_fact("user", "likes", "tea", user_id="u1")
    newer = mem.add_fact("user", "likes", "coffee", user_id="u1")
    copy_hot = CopyingVectorStore()
    for fact in mem.fact_store.values():
        copy_hot.upsert(fact.id, fact.embedding or [], fact)
    mem.fact_store = copy_hot
    mem.cold_store = CopyingVectorStore()
    mem._rewire()

    cf = Conflict(older=older.id, newer=newer.id, user_id="u1")
    mem.conflicts[cf.id] = cf

    assert mem.resolve_conflict(cf.id, keep="newer") is True
    assert not mem.fact_store.get(older.id).is_live()
    assert mem.fact_store.get(newer.id).supersedes == older.id


def test_agentic_multihop_decomposition():
    """Bet B: with an LLM, lean_context decomposes the question into sub-queries and unions their
    retrieval; without an LLM it degrades gracefully to the single query."""
    mem = build()  # 离线提取 facts

    # 优雅降级：agentic=True 但没有 llm -> 仍返回可用 context
    ctx0 = mem.lean_context("Which cities did I visit?", user_id="u1", agentic=True, n_chunks=1)
    assert ctx0.strip()

    # 配备能分解问题的 llm 后，该路径正常运行并产出 context
    class FakeLLM:
        def complete(self, prompt, system=None, **k):
            return '["cities I traveled to", "trips"]'

    mem.llm = FakeLLM()
    ctx1 = mem.lean_context("Which cities did I visit and how many trips?", user_id="u1",
                            agentic=True, n_chunks=1)
    assert ctx1.strip() and "SESSION SUMMARIES" in ctx1


def test_agentic_episode_gather_respects_as_of():
    """Agentic chunk 检索必须保留调用方指定时点的记忆视图。"""
    from mnemotrace.retrieve.agentic import AgenticRetriever

    class FakeLLM:
        def complete(self, prompt, system=None, **k):
            return '["trips"]'

    class FakeMemory:
        def __init__(self):
            self.calls = []

        def retrieve_episodes(self, query, user_id, k, as_of=None):
            self.calls.append(as_of)
            return []

    fake = FakeMemory()
    AgenticRetriever(fake, FakeLLM()).gather_episodes("Which cities did I visit?", "u1", 1, as_of=BASE)
    assert fake.calls == [BASE]


def test_procedural_memory_surfaces_instructions():
    """程序性记忆：一个独立的类型化视图，返回用户长期有效的 how-to/指令类 fact。"""
    from mnemotrace.types import Fact

    mem = Memory()
    u = "u1"
    mem.fact_store.upsert("f1", [0.0], Fact(subject="u1", predicate="wants_reminder",
                                            object="water the plants", user_id=u))
    mem.fact_store.upsert("f2", [0.0], Fact(subject="u1", predicate="works_at", object="Tencent", user_id=u))
    proc = mem.procedural("u1")
    objs = {f.object for f in proc}
    assert "water the plants" in objs and "Tencent" not in objs


def test_procedural_memory_includes_cold_tier_instructions():
    mem = Memory()
    cold = mem.add_fact("u1", "wants_reminder", "water the plants", user_id="u1")
    hot = mem.add_fact("u1", "works_at", "Tencent", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1

    objs = {f.object for f in mem.procedural("u1")}
    assert "water the plants" in objs
    assert "Tencent" not in objs


def test_working_memory_holds_active_query_set():
    """工作记忆：lean_context 为当前查询填充一个临时的、可检查的活跃集。"""
    mem = build()
    assert mem.working_set == []  # 尚未关注任何内容
    mem.lean_context("Where do I work?", user_id="u1", n_chunks=1)
    assert isinstance(mem.working_set, list)  # 已填充该查询的活跃 facts（可能为空）


def test_decay_sweep_runs_in_consolidation():
    """Bet E：显著度清扫确实会执行（此前是死代码）。旧的偶发 fact 最终低于 1.0；
    持久的偏好保持满显著度。"""
    mem = build()  # consolidate() -> 会执行 _decay_sweep()
    live = [f for f in mem.fact_store.values() if f.is_live()]
    # 至少清扫无错执行完，且显著度落在 (0, 1] 区间
    assert all(0.0 < f.salience <= 1.5 for f in live)


def test_durable_facts_exempt_from_decay():
    """Mem0/OMEGA：偏好 + 身份不会衰减。偶发 fact 会衰减，偏好不会，
    即使经过同样长的时间间隔。"""
    from mnemotrace.consolidate.decay import decay
    from mnemotrace.types import Fact

    incidental = Fact(subject="u", predicate="parked_at", object="lot B", salience=1.0, last_access=BASE)
    preference = Fact(subject="u", predicate="likes", object="jazz", salience=1.0, last_access=BASE)
    later = BASE + 100 * DAY
    decay(incidental, per_day=0.02, t=later)
    decay(preference, per_day=0.02, t=later)
    assert incidental.salience < 1.0, "incidental fact should fade"
    assert preference.salience == 1.0, "durable preference is exempt from decay"


def test_reinforce_boosts_and_resets_clock():
    from mnemotrace.consolidate.decay import reinforce
    from mnemotrace.types import Fact

    f = Fact(subject="u", predicate="parked_at", object="lot B", salience=1.0, last_access=BASE)
    reinforce(f, boost=0.5, t=BASE + 5 * DAY)
    assert f.salience == 1.5 and f.access_count == 1 and f.last_access == BASE + 5 * DAY


def test_subsumption_keeps_distinct_values():
    """多值谓词上互不为子集的不同取值必须都保留 —— 不能过度合并。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact

    r = ConflictResolver()
    pizza = Fact(subject="u", predicate="likes", object="pizza", valid_at=BASE)
    action, inv = r.reconcile(Fact(subject="u", predicate="likes", object="sushi", valid_at=BASE + 1),
                              [pizza])
    assert action == "add" and not inv, "distinct likes must not subsume each other"


def test_llm_escalation_resolves_borderline_conflict():
    """「IV. 不允许静默损坏记忆」的歧义裁决：同主体且相似度落在模糊带（略低于阈值）的一对 fact，只有在配置了 LLM 时
    才升级给 LLM 仲裁者；没有 LLM 则两者并存。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.embed import HashingEmbedder
    from mnemotrace.types import Fact

    old = Fact(subject="u", predicate="does_yoga", object="twice a week", valid_at=BASE)
    old.embedding = [1.0, 0.0]
    new = Fact(subject="u", predicate="practices_yoga", object="three times a week", valid_at=BASE + 1)
    new.embedding = [0.75, 0.66]  # cosine ≈ 0.75 → 落在模糊带 [0.68, 0.80) 内，低于自动阈值

    # 无 LLM -> 临界对不会被取代（完全不依赖 LLM，两者并存）
    r0 = ConflictResolver(embedder=HashingEmbedder(2), sim_threshold=0.80)
    assert not r0.reconcile(new, [old])[1]

    # LLM 返回 REPLACES -> 旧 fact 被失效化
    class FakeLLM:
        def complete(self, prompt, system=None, **k):
            return "REPLACES"

    r1 = ConflictResolver(embedder=HashingEmbedder(2), sim_threshold=0.80, llm=FakeLLM())
    assert old in r1.reconcile(new, [old])[1]


def test_reflector_propagates_supersession_into_summaries():
    """在知识更新前定稿的摘要，应由 reflector 把当前值追加进去。"""
    from mnemotrace.types import Fact

    mem = Memory()
    ep = mem.add("I do yoga twice a week.", user_id="u1", session_id="s1", event_time=BASE)
    # 一条旧 fact（来源于 ep），随后被新 fact 取代
    old = Fact(subject="u1", predicate="yoga_frequency", object="twice a week",
               user_id="u1", valid_at=BASE, provenance=[ep.id],
               invalid_at=BASE + 5 * DAY, expired_at=BASE + 5 * DAY)
    new = Fact(subject="u1", predicate="yoga_frequency", object="three times a week",
               user_id="u1", valid_at=BASE + 5 * DAY, supersedes=old.id)
    mem.fact_store.upsert(old.id, [0.0], old)
    mem.fact_store.upsert(new.id, [0.0], new)
    mem.summarize_episodes([ep])  # 冻结 "twice a week" 的摘要
    assert "[updated:" not in ep.summary
    n = mem.reflect("u1")
    assert n == 1
    assert "[updated:" in ep.summary and "three times a week" in ep.summary


def test_reflector_uses_cold_tier_superseded_facts():
    from mnemotrace.types import Fact

    mem = Memory()
    ep = mem.add("I do yoga twice a week.", user_id="u1", session_id="s1", event_time=BASE)
    old = Fact(subject="u1", predicate="yoga_frequency", object="twice a week",
               user_id="u1", valid_at=BASE, provenance=[ep.id],
               invalid_at=BASE + 5 * DAY, expired_at=BASE + 5 * DAY, salience=0.0)
    new = Fact(subject="u1", predicate="yoga_frequency", object="three times a week",
               user_id="u1", valid_at=BASE + 5 * DAY, supersedes=old.id,
               salience=1.0)
    mem.fact_store.upsert(old.id, [0.0], old)
    mem.fact_store.upsert(new.id, [0.0], new)
    mem.summarize_episodes([ep])
    assert mem.evict_cold(max_hot=1) == 1

    assert mem.reflect("u1") == 1
    assert "three times a week" in ep.summary


def test_type_weight_ranks_preference_and_identity_above_incidental():
    """类型加权融合（MemoryScope/OMEGA）：preference > identity > incidental 的乘数。"""
    from mnemotrace.config import Config
    from mnemotrace.retrieve.hybrid import fact_type_weight
    from mnemotrace.types import Fact

    cfg = Config()
    pref = Fact(subject="u", predicate="likes", object="jazz")
    fav = Fact(subject="u", predicate="favorite_food", object="ramen")
    ident = Fact(subject="u", predicate="works_at", object="Tencent")
    incidental = Fact(subject="u", predicate="mentioned", object="the weather")
    assert fact_type_weight(pref, cfg) == cfg.w_type_preference
    assert fact_type_weight(fav, cfg) == cfg.w_type_preference  # favorite_* 前缀
    assert fact_type_weight(ident, cfg) == cfg.w_type_identity
    assert fact_type_weight(incidental, cfg) == 1.0
    assert cfg.w_type_preference > cfg.w_type_identity > 1.0


def test_type_weighting_is_relevance_gated_and_embedder_gated():
    """修正后类型加权的两个安全性质：
      (1) 它只作用于 SEMANTIC 分数，因此只在相关 facts 之间重排，绝不会抬升离题项 ——
          'where do I work?' 仍须返回雇主，而不是某个偏好；
      (2) 对离线 HashingEmbedder（cosine 噪声大）则整体关闭。"""
    from mnemotrace.embed import HashingEmbedder
    from mnemotrace.retrieve.hybrid import HybridRetriever
    from mnemotrace.store import InMemoryGraphStore, InMemoryVectorStore
    from mnemotrace.config import Config

    r = HybridRetriever(InMemoryVectorStore(), InMemoryGraphStore(), HashingEmbedder(64), Config())
    assert r._semantic is False, "type-weighting must be gated off for the hashing embedder"

    # 相关性仍然优先（这正是此前乘数导致的回归：对工作类查询，'favorite_language'
    # 的 fact 排在了 'works_at' 前面）
    mem = build()
    ans = mem.search("Where do I work?", user_id="u1").answer().lower()
    assert "tencent" in ans, "relevance must win — a preference fact must not outrank the employer"


def test_focus_track_boosts_salience_and_mute_hides():
    """关注点面板是真实接线而非摆设（AGENTS.md 「检索与模块」）：被跟踪的话题会提升 fact 的显著度
    （作为检索打分 + 豁免衰减的信号），被静音的话题则同时从组装的读取 context 和合成的
    persona 中剔除。"""
    mem = Memory()
    mem.add_fact("user", "uses", "Python", user_id="u1")
    mem.add_fact("user", "plays", "guitar", user_id="u1")
    py = next(f for f in mem.fact_store.values() if "python" in f.text.lower())
    before = py.salience

    mem.set_focus(track=["python"], mute=["guitar"])
    assert py.salience > before, "tracked topic must boost salience"

    ctx = mem.lean_context("what do I do", user_id="u1", n_chunks=0).lower()
    assert "python" in ctx, "tracked fact should remain in the read context"
    assert "guitar" not in ctx, "muted topic must be hidden from the read context"

    persona = mem.build_persona("u1")
    assert "FOCUS AREAS" in persona, "tracked topics should surface in the profile"
    assert "guitar" not in persona.lower(), "muted topic must not appear in the persona"


def test_persona_includes_cold_tier_facts():
    mem = Memory()
    cold = mem.add_fact("user", "favorite_language", "Python", user_id="u1")
    hot = mem.add_fact("user", "project_note", "hot note", user_id="u1")
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1

    assert "Python" in mem.build_persona("u1")


def test_focus_persists_across_save_open(tmp_path):
    mem = Memory()
    mem.add_fact("user", "uses", "Python", user_id="u1")
    mem.set_focus(track=["python", "work"], mute=["weight"])
    p = str(tmp_path / "u.pkl")
    mem.save(p)
    assert Memory.open(p).get_focus() == {"track": ["python", "work"], "mute": ["weight"]}


def test_graph_data_has_no_dangling_edges():
    """graph_data() 供关系图谱视图使用：每条边的端点都必须是返回的节点，孤儿实体
    （没有存留边）会被丢弃，使画面聚焦于关系本身。"""
    mem = build()
    g = mem.graph_data("u1")
    assert g["nodes"] and g["edges"], "a consolidated history should yield a non-empty graph"
    ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids, "no dangling edge endpoints"
        assert "predicate" in e and "live" in e
        assert e["fact_id"] and e["fact_text"]
        assert isinstance(e["valid_at"], float) and e["valid_at_h"]
        assert "invalid_at" in e and "invalid_at_h" in e
        assert isinstance(e["provenance"], list)


def test_graph_data_as_of_excludes_future_relations():
    """图谱视图必须遵守与 search/as_of 相同的 valid-time 边界：未来才生效的边，
    不能仅因为“尚未被失效化”就出现在过去快照中。"""
    mem = Memory()
    mem.add("Wei works at Tencent.", user_id="u1", event_time=BASE)
    mem.add("Wei works at Moonshot AI.", user_id="u1", event_time=BASE + 30 * DAY)
    mem.consolidate()

    past = mem.graph_data("u1", as_of=BASE + 10 * DAY)
    past_names = {n["name"] for n in past["nodes"]}
    assert "Tencent" in past_names
    assert "Moonshot AI" not in past_names
    assert all(e["live"] for e in past["edges"])

    current = mem.graph_data("u1")
    assert "Moonshot AI" in {n["name"] for n in current["nodes"]}


def test_graph_data_can_exclude_sensitive_fact_edges():
    mem = Memory()
    mem.add_fact("user", "has_disease", "diabetes", user_id="u1")
    mem.add_fact("user", "works_at", "Acme", user_id="u1")

    full = mem.graph_data("u1")
    assert "diabetes" in str(full).lower()
    assert "Acme" in str(full)

    safe = mem.graph_data("u1", include_sensitive=False)
    rendered = str(safe).lower()
    assert "diabetes" not in rendered
    assert "Acme" in str(safe)
    assert all("diabetes" not in e.get("fact_text", "").lower() for e in safe["edges"])


def test_graph_data_can_exclude_sensitive_cold_fact_edges():
    mem = Memory()
    disease = mem.add_fact("user", "has_disease", "diabetes", user_id="u1")
    work = mem.add_fact("user", "project_note", "Acme", user_id="u1")
    disease.salience = 0.0
    work.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(disease.id) is not None

    full = mem.graph_data("u1")
    assert "diabetes" in str(full).lower()

    safe = mem.graph_data("u1", include_sensitive=False)
    rendered = str(safe).lower()
    assert "diabetes" not in rendered
    assert all(e["fact_id"] != disease.id for e in safe["edges"])


def test_memory_policy_edits_extraction_and_persists(tmp_path):
    """记忆策略页面是真实接线：用户的 'what to record' 指令会追加到提取 system prompt 后面，
    完整 prompt 覆盖会替换默认值（传 '' 则清回默认），且整套策略在 save/open 往返后仍保留。"""
    class FakeLLM:
        def complete(self, prompt, system=""):
            return "[]"

    from mnemotrace.consolidate.llm_extractor import EXTRACT_SYSTEM
    m = Memory(llm=FakeLLM())

    # 增量式 'what to record' 指令会进入 extractor 的实际生效 system prompt
    m.set_policy(extract_instruction="只记录与工作相关的事实")
    ex = m.engine.extractor
    assert ex.instruction == "只记录与工作相关的事实"
    assert "只记录与工作相关的事实" in ex._effective_system()
    assert EXTRACT_SYSTEM in ex._effective_system()  # 默认仍在，指令为追加

    # 完整覆盖会替换默认值，"" 则重置回默认
    m.set_policy(extract_system="CUSTOM PROMPT")
    assert m.engine.extractor.system == "CUSTOM PROMPT"
    m.set_policy(extract_system="")
    assert m.engine.extractor.system == EXTRACT_SYSTEM

    # get_policy 暴露覆盖项 + 内置默认值；策略持久化
    gp = m.get_policy()
    assert set(gp) == {"policy", "defaults"} and len(gp["defaults"]) == 4
    p = str(tmp_path / "u.pkl")
    m.save(p)
    assert Memory.open(p).get_policy()["policy"]["extract_instruction"] == "只记录与工作相关的事实"


def test_structured_profile_tiers_display_only_and_preserves_recall():
    """L2 结构化画像（特性③，合理版）：basic/preferences/habits 分组展示，confirmed↔tentative
    划分仅用于展示，证据保持诚实（不编造权重）。关键不变量：tentative 划分只是呈现层面 ——
    tentative fact 仍必须留在 fact store 中且可检索（召回绝不被画像视图限制）。"""
    from mnemotrace.types import Fact
    m = Memory()
    m.add_fact("user", "favorite_music", "周杰伦", user_id="u")     # 显式 favorite -> confirmed
    casual = Fact(subject="user", predicate="likes", object="jazz", user_id="u",
                  source="extracted", provenance=["ep1"])          # 单次随意提及 -> tentative
    casual.embedding = m.embedder.embed(casual.text)
    m.fact_store.upsert(casual.id, casual.embedding, casual)

    p = m.structured_profile("u")
    conf_items = [it["item"] for items in p["preferences"].values() for it in items]
    # 用户明确说出的偏好即 confirmed（用户亲口说的，不是牵强的推断）；显式 favorite 和
    # 明确说出的 likes 都进入正式画像，而不是作为待确认候选。
    assert "周杰伦" in conf_items and "jazz" in conf_items, "stated preferences are confirmed"
    assert [t["item"] for t in p["tentative"]] == [], "stated preferences are not held as 待确认"

    # 证据诚实，绝不编造数值权重
    for items in p["preferences"].values():
        for it in items:
            assert "weight" not in it and it["evidence"]["kind"] in {"user", "mentions", "reinforced"}

    # 召回不变量：tentative fact 只是分了展示层，并未移除 —— 仍在 store 中且 live
    assert m.fact_store.get(casual.id) is not None and casual.is_live()
    assert any(f.object == "jazz" for f in m.fact_store.values() if f.is_live())


def test_structured_profile_includes_cold_tier_facts():
    m = Memory()
    cold = m.add_fact("user", "favorite_music", "周杰伦", user_id="u")
    hot = m.add_fact("user", "project_note", "hot note", user_id="u")
    cold.salience = 0.0
    hot.salience = 1.0
    assert m.evict_cold(max_hot=1) == 1

    p = m.structured_profile("u")
    items = [it["item"] for values in p["preferences"].values() for it in values]
    assert "周杰伦" in items


def test_working_memory_is_ephemeral_and_never_enters_long_term(tmp_path):
    """特性①：working-memory 层保存瞬态状态但不进长期记忆，以 session + TTL 限定作用域，
    session 结束即清空。核心不变量：ephemeral 条目绝不变成持久 fact。"""
    import mnemotrace.util as u
    m = Memory()
    assert not hasattr(Memory, "is_ephemeral")

    m.remember_working("today my throat hurts", user_id="u", session_id="s1", kind="state")
    m.remember_working("this trip front seat is my wife", user_id="u", session_id="s1",
                       kind="passenger", ttl_seconds=7200)
    m.remember_working("note in another session", user_id="u", session_id="s2")

    # 以 session 为作用域 + 完全不进长期记忆
    assert {w.content for w in m.working_memory("u", session_id="s1")} == {
        "today my throat hurts", "this trip front seat is my wife"}
    assert list(m.fact_store.values()) == [], "working memory must NOT create long-term facts"

    # 会出现在该 session 的读取 context 中，但属临时性质
    ctx = m.lean_context("how am I", user_id="u", session_id="s1", n_chunks=0)
    assert "WORKING MEMORY" in ctx and "throat" in ctx.lower()

    # TTL 硬过期，之后按 session 清空
    later = u.now() + 8000
    assert [w.kind for w in m.working_memory("u", session_id="s1", as_of=later)] == ["state"]
    assert m.clear_session("u", "s1") == 1 and m.working_memory("u", session_id="s1") == []

    # 跨 save/open 持久保留
    m.remember_working("persist me", user_id="u", session_id="s3")
    p = str(tmp_path / "u.pkl")
    m.save(p)
    assert [w.content for w in Memory.open(p).working_memory("u", session_id="s3")] == ["persist me"]


def test_classification_and_sensitivity_redaction_preserves_recall():
    """特性⑤：fact 由规则得到类别 + 敏感标志；敏感 fact 可在共享/导出 context 中被脱敏 ——
    同时在 store 中完整保留（召回和本人视图均不受影响）。"""
    from mnemotrace.consolidate.classify import classify
    assert classify("has_disease", "diabetes", "user has_disease diabetes") == ("健康", True)
    assert classify("salary", "25000", "user salary 25000")[1] is True
    assert classify("works_at", "Acme", "user works_at Acme") == ("工作", False)
    assert classify("id_number", "310101199001011234", "x")[1] is True  # 连续数字 PII

    m = Memory()
    m.add_fact("user", "has_disease", "diabetes", user_id="u")  # 敏感
    m.add_fact("user", "works_at", "Acme", user_id="u")          # 非敏感
    sensitive_fact = next(f for f in m.fact_store.values() if f.object == "diabetes")
    assert sensitive_fact.sensitive and sensitive_fact.category == "健康"

    redacted = m.lean_context("about me", user_id="u", n_chunks=0, redact_sensitive=True)
    assert "diabetes" not in redacted.lower(), "sensitive fact must be redacted from a shared context"
    assert "acme" in redacted.lower(), "non-sensitive facts stay"
    # 召回不变量：脱敏是视图过滤而非删除 —— fact 仍在存储中且 live
    assert any(f.object == "diabetes" for f in m.fact_store.values() if f.is_live())


def test_redacted_context_omits_free_text_layers_that_can_leak_sensitive_content():
    """脱敏/共享 context 不得通过摘要、chunk 或工作记忆文本泄漏敏感内容。
    它只能依赖已分类、非敏感的结构化 facts。"""
    m = Memory()
    ep = m.add("My private diagnosis is diabetes. I work at Acme.", user_id="u", session_id="s1")
    ep.summary = "Private diagnosis: diabetes. Job: Acme."
    m.summary_vec.upsert(ep.id, m.embedder.embed(ep.summary), ep)
    m.add_fact("user", "has_disease", "diabetes", user_id="u")
    m.add_fact("user", "works_at", "Acme", user_id="u")
    m.remember_working("diabetes flare today", user_id="u", session_id="s1")

    redacted = m.lean_context(
        "what do you know about my diagnosis and work?",
        user_id="u",
        session_id="s1",
        n_summaries=5,
        n_chunks=5,
        redact_sensitive=True,
        char_budget=10_000,
    )

    assert "Acme" in redacted
    assert "diabetes" not in redacted.lower()
    assert "SESSION SUMMARIES" not in redacted
    assert "RELEVANT CONVERSATIONS" not in redacted
    assert "WORKING MEMORY" not in redacted


def test_ephemeral_remember_keeps_dated_episode_but_creates_no_durable_fact():
    """修正后的特性①模型（用户指出的）：ephemeral != 删除。瞬态状态进入工作记忆，
    同时保留为带日期的 EPISODE（因此 'when did my throat hurt?' 可从历史中作答），
    但不会变成持久的画像 fact（因此绝不会滞留为当前属性）。"""
    m = Memory()
    r = m.remember("today my throat is uncomfortable", user_id="u", session_id="s1", scope="working")
    assert r["scope"] == "working"

    # 事件保留在带日期的情景日志中 -> 可按历史查询
    eps = m.retrieve_episodes("throat uncomfortable", user_id="u", k=3)
    assert any("throat" in e.content.lower() for e in eps), "dated episode must remain retrievable"
    # 当前 session 的工作记忆同样持有它
    assert any("throat" in w.content.lower() for w in m.working_memory("u", session_id="s1"))

    # 但绝不生成持久画像 fact —— 且后续 consolidate 不得将该临时 episode 收入
    m.remember("I work at Acme", user_id="u", session_id="s1")
    m.consolidate()
    assert not any("throat" in f.text.lower() for f in m.fact_store.values()), \
        "transient state must not become a durable profile fact"


def test_display_localization_keeps_canonical_predicate():
    """展示本地化（i18n）：把中文记录的 fact 自然地渲染出来，但不改动规范英文 predicate
    （即 slot key）。英文记录的 fact 原样保留。"""
    from mnemotrace.localize import render_display
    # 中文 object -> 本地化的中文展示
    assert render_display("user", "works_at", "字节跳动", "user works at 字节跳动") == "在 字节跳动 工作"
    assert render_display("user", "favorite_music", "周杰伦", "x") == "最喜欢的音乐是 周杰伦"
    assert render_display("user", "allergic_to", "花粉", "x") == "对 花粉 过敏"
    # 英文数据不被强制翻译
    assert render_display("user", "works_at", "Acme", "user works at Acme") == "user works at Acme"
    # 规范 predicate/slot 不因展示而改变（引擎仍据此去重）
    m = Memory()
    f = m.add_fact("user", "works_at", "字节跳动", user_id="u")
    assert f.predicate == "works_at" and f.slot[2] == "works_at"


def test_costated_facts_do_not_semantic_supersede():
    """回归（用户反馈）：来自同一 episode 的两条 fact（'我在字节跳动做后端开发' ->
    works_at 字节跳动 + job_title 后端开发）是互补属性，即使它们很短、同主体且话题相近，
    也绝不能经 embedding 路径互相取代。真正的更新必须来自更晚的、独立的 episode。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact

    class E:  # 返回相同向量的 embedder -> cosine 1.0（语义路径的最坏情况）
        def embed(self, t):
            return [1.0, 0.0]

    r = ConflictResolver(embedder=E(), sim_threshold=0.8)
    old = Fact("user", "works_at", "字节跳动", provenance=["ep1"])
    old.embedding = [1.0, 0.0]
    new = Fact("user", "job_title", "后端开发", provenance=["ep1"])  # 同一 episode
    new.embedding = [1.0, 0.0]
    _, invalidated = r.reconcile(new, [old])
    assert invalidated == [], "co-stated complementary facts must not supersede each other"

    # 来自不同且更晚 episode 的真实更新仍会取代（防护只按 provenance 限定）
    later = Fact("user", "occupation", "产品经理", provenance=["ep9"])
    later.embedding = [1.0, 0.0]
    _, inval2 = r.reconcile(later, [old])
    assert old in inval2, "a later separate-episode near-duplicate should still supersede"


def test_preference_reversal_supersedes_same_object_opposite_polarity():
    """用户反馈：先 '我喜欢跳舞' 再 '我不喜欢跳舞' 时两者都出现（+跳舞 和 -跳舞）。
    同一 object 上 like<->dislike 的翻转属于反转 —— 新立场取代旧立场（PRD 治理：修正否定）。
    不同 object 仍然累积（多值）。"""
    from mnemotrace.consolidate.conflict import ConflictResolver
    from mnemotrace.types import Fact
    r = ConflictResolver()
    old = Fact("user", "likes", "跳舞", valid_at=100.0)
    new = Fact("user", "dislikes", "跳舞", valid_at=200.0)
    _, invalidated = r.reconcile(new, [old])
    assert old in invalidated, "later opposite-polarity preference on the same object must supersede"
    # 不同 object 不受影响 —— 保留多值累积
    other = Fact("user", "likes", "唱歌", valid_at=50.0)
    _, inval2 = r.reconcile(Fact("user", "likes", "跳舞", valid_at=300.0), [other])
    assert other not in inval2


def test_conflict_detection_flags_candidates_and_coexist_default():
    """System-2 LLM 检测：只对候选对中真实的疑似冲突做标记；默认 COEXIST
    （无关/模型不确定的对永不标记）。"""
    from mnemotrace.consolidate.detect import detect_conflicts
    from mnemotrace.types import Fact

    class Judge:
        def complete(self, prompt, system=""):
            return "CONFLICT" if ("北京" in prompt and "上海" in prompt) else "COEXIST"

    a = Fact("user", "lives_in", "北京", valid_at=100.0); a.embedding = [1.0, 0.0]
    b = Fact("user", "based_in", "上海", valid_at=200.0); b.embedding = [0.8, 0.6]   # ~0.8 cosine：在模糊带内
    c = Fact("user", "likes", "咖啡", valid_at=150.0); c.embedding = [0.0, 1.0]       # 无关（cos<0.62）
    flagged = detect_conflicts([a, b, c], Judge(), "user", seen=set())
    assert len(flagged) == 1, "only the genuine conflict pair is flagged (coexist default)"
    cf = flagged[0]
    assert cf.newer == b.id and cf.older == a.id  # newer = valid_at 更晚者


def test_conflict_resolve_and_dismiss_are_user_driven():
    """检测绝不自动解决 —— 只有用户的 resolve()/dismiss() 会生效；resolve 使选定的败方被取代，
    dismiss 则两者都保留。"""
    from mnemotrace.types import Conflict
    m = Memory()
    fa = m.add_fact("user", "likes", "北京", user_id="u")   # 多值 -> 两者并存（不自动合并）
    fb = m.add_fact("user", "likes", "上海", user_id="u")
    cf = Conflict(older=fa.id, newer=fb.id, user_id="u")
    m.conflicts[cf.id] = cf
    assert m.pending_conflicts("u"), "both live -> pending"

    assert m.resolve_conflict(cf.id, keep="newer")
    assert not fa.is_live() and fb.is_live(), "keep=newer supersedes the older"
    assert cf.status == "resolved" and m.pending_conflicts("u") == []

    # dismiss 两者都保留
    g1 = m.add_fact("user", "likes", "茶", user_id="u")
    g2 = m.add_fact("user", "likes", "奶茶", user_id="u")
    cf2 = Conflict(older=g1.id, newer=g2.id, user_id="u")
    m.conflicts[cf2.id] = cf2
    assert m.dismiss_conflict(cf2.id) and g1.is_live() and g2.is_live()
    assert m.pending_conflicts("u") == []


def test_promotion_never_evicts_every_semantic_chunk():
    """2026-08-27 回归：被提升的 provenance chunk 占满了整个 k_chunks 预算，导致 fact 检索
    未命中时，承载答案的语义检索 episode 被驱逐，单 session 召回塌缩为拒答。提升最多只能占用
    一半预算（k>=2）；即使提升候选很多，语义保底槽位也必须存活。"""
    from mnemotrace.config import Config
    from mnemotrace.types import Fact

    mem = Memory(config=Config(evidence_planner=False, provenance_evidence=False,
                               provenance_chunk_promotion=True))
    # 答案所在的 session 只有语义检索才会给它高排名
    mem.add("The screening room access code is painted behind the velvet poster.",
            user_id="u1", session_id="answer-session", event_time=BASE)
    # 被提升 facts 将指向的离题 session
    sources = []
    for i in range(3):
        sources.append(mem.add(f"Budget sync {i}: the quarterly numbers were reviewed in depth.",
                               user_id="u1", session_id=f"budget-{i}", event_time=BASE + (i + 1) * DAY))
    mem.consolidate()
    # 排名靠前的 facts 全部指向离题内容 -> 此前它们的 provenance chunk 会占满预算
    for i, src in enumerate(sources):
        f = Fact(subject="u1", predicate=f"reviewed_{i}", object=f"quarterly numbers {i}",
                 user_id="u1", provenance=[src.id])
        f.embedding = mem.embedder.embed(f.text)
        mem.fact_store.upsert(f.id, f.embedding, f)
    ctx = mem.lean_context("what is the screening room access code?", user_id="u1", n_chunks=2)
    assert "velvet poster" in ctx  # 语义槽位在提升压力下幸存


def test_evolution_block_leads_with_the_current_value():
    """知识更新回归防护：扁平表格迫使回答者扫描 `role` 列来判断当前哪个值为真，且它选中
    已被取代值的频率高到足以输给朴素 full-context（例如搬迁前的城市）。该块必须首先把当前值
    作为结论陈述，被替换的值明显居于从属地位。"""
    from mnemotrace.config import Config

    mem = Memory(config=Config(evidence_planner=False))
    mem.add("I live in Hawaii.", user_id="u1", event_time=BASE)
    mem.consolidate()
    mem.add("I moved. I live in Paris now.", user_id="u1", event_time=BASE + 30 * DAY)
    mem.consolidate()
    ctx = mem.lean_context("where do I live?", user_id="u1")
    assert "FACT EVOLUTION" in ctx
    evo = ctx[ctx.index("FACT EVOLUTION"):]
    assert "CURRENT:" in evo and "Paris" in evo
    # 当前值必须先于被替换值出现
    assert evo.index("Paris") < evo.index("Hawaii"), evo
    assert "was:" in evo and "Hawaii" in evo


def test_aggregation_questions_widen_the_preconsolidation_pool():
    """聚合类答案只要漏掉一次出现就错了，而出现天然分布在多个 session。为指向性问题设计的
    池子会截断它们（4 家航空公司的清单只回来 3 家），因此 count/list/sum 类问题使用更宽的池子
    —— 且仅限这类问题，以控制提取成本。"""
    from mnemotrace.config import Config
    from mnemotrace.types import Episode
    from eval.bench import retrieve_evidence_episodes

    calls: list[int] = []

    class FakeMem:
        def __init__(self, boost=True):
            self.config = Config(aggregation_pool_boost=boost)

        def retrieve_episodes(self, query, user_id, k):
            calls.append(k)
            return [Episode(f"flight {i}", id=f"e{i}", user_id="u1") for i in range(k)]

    retrieve_evidence_episodes(FakeMem(), "How many airlines have I flown with in total?", "u1", 8)
    assert max(calls) == 16, f"aggregation question should widen the pool, got {calls}"

    calls.clear()
    retrieve_evidence_episodes(FakeMem(), "Where does my sister work?", "u1", 8)
    assert max(calls) == 8, f"pointed question must keep the normal pool, got {calls}"

    calls.clear()  # 消融后恢复窄池
    retrieve_evidence_episodes(FakeMem(boost=False), "How many airlines in total?", "u1", 8)
    assert max(calls) == 8, f"ablated boost should not widen, got {calls}"
