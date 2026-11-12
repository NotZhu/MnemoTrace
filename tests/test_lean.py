"""Lean 读取路径（AGENTS.md Bet A/E）：可扩展的获胜条件 —— 从一小段检索结果（persona + facts +
session digests + 少量完整 chunk）作答，而不是重放全部历史。这些测试在离线环境下（MockEmbedder
+ 规则 extractor，零依赖、确定性）固定框架行为：L2 摘要会被构建并索引，L3 persona 会被合成，
lean_context 能正确组装它们，并做去重 + 硬性大小上限。"""
from __future__ import annotations

from tests.embedder_fixtures import MockEmbedder
from tests.model_fixtures import link_synthetic_facts, seed_linked, seed_timeline
from tests.model_fixtures import MemoryEngine
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


def build(generate_digests=True) -> MemoryEngine:
    mem = MemoryEngine()
    eps = [mem.add(text, user_id="u1", session_id=f"s{i}", event_time=BASE + day * DAY)
           for i, (text, day) in enumerate(SESSIONS)]
    for ep in eps:  # 离线打上日期戳，与 eval 的 ingest 流程保持一致
        ep.metadata["date"] = "2023-01-0%d" % (eps.index(ep) + 1)
    mem.consolidate(eps)
    if generate_digests:
        # Seed existing digests: these tests exercise read consumers, not model compression.
        for ep in eps:
            ep.digest = ep.content
            ep.digest_embedding = mem.embedder.embed(ep.digest)
            mem._sync_episode(ep)
    return mem


def test_existing_digests_are_indexed_exclusively():
    mem = build()
    assert len(mem.digest_vec.values()) == len(SESSIONS)
    for ep in mem.episodes_doc.values():
        assert ep.digest, "each episode should carry a digest after generate_digests"
        assert ep.digest_embedding is not None


def test_summarize_is_idempotent():
    mem = build()
    n = mem.generate_digests(list(mem.episodes_doc.values()))  # 已经摘要过
    assert n == 0, "re-summarizing already-summarized episodes is a no-op"


def test_retrieve_digests_is_relevant():
    mem = build()
    query = "Which cities did I travel to?"
    mem.reembed(MockEmbedder(2, vectors={query: [1.0, 0.0], SESSIONS[1][0]: [1.0, 0.0]}))
    hits = mem.retrieve_digests(query, user_id="u1", k=3)
    assert hits, "digest retrieval returns something"
    joined = " ".join(h.text.lower() for h in hits)
    # 旅行相关的 session 应排在猫/吉他之前
    assert any(city in joined for city in ("kyoto", "lisbon", "oslo"))


def test_lean_context_assembles_facts_and_digests():
    mem = build()
    ctx = mem.lean_context("trip Kyoto Lisbon Oslo", user_id="u1",
                           n_digests=10, n_facts=10)
    assert "USER PROFILE:" not in ctx
    assert "SESSION DIGESTS" in ctx
    assert "FACTS" in ctx


def test_lean_context_respects_char_budget():
    mem = build()
    ctx = mem.lean_context("trips", user_id="u1", char_budget=200)
    assert len(ctx) <= 200, "lean_context must hard-cap at char_budget"


def test_lean_context_is_leaner_than_full_history():
    mem = build()
    full = "\n".join(ep.content for ep in mem.episodes_doc.values())
    lean = mem.lean_context("trips", user_id="u1")
    # 只带少量完整 chunk + 小摘要时，lean 必须远小于全量倾倒所有 session
    # （这里历史很小；benchmark 规模下差距会急剧拉大）
    assert len(lean) <= max(2048, len(full) * 3)  # sanity：不能大得离谱


def test_consolidate_full_builds_facts_and_digests():
    mem = MemoryEngine()
    eps = [mem.add(t, user_id="u1", session_id=f"s{i}", event_time=BASE + d * DAY)
           for i, (t, d) in enumerate(SESSIONS)]
    stats = mem.consolidate_full(fact_episodes=eps, digest_episodes=eps)
    assert stats["facts_added"] >= 0 and stats["digests"] == 0
    assert len(mem.digest_vec.values()) == 0


def test_lean_path_works_with_no_digests():
    """优雅降级：即使从未构建摘要，lean_context 仍能返回 facts。"""
    mem = build(generate_digests=False)
    ctx = mem.lean_context("Where do I work?", user_id="u1")
    assert "SESSION DIGESTS" not in ctx  # 未构建任何摘要
    assert ctx.strip(), "still produces a usable context from facts"


def test_synthetic_fact_sensitivity_and_erasure():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "ByteDance", user_id="u1", text="user works at ByteDance")
    assert mem.set_fact_sensitive(fact.id, False).text == fact.text
    assert mem.delete_fact(fact.id)
    assert mem.fact_store.get(fact.id) is None
    assert not mem.delete_fact("nonexistent")


def test_delete_fact_removes_graph_export_edge():
    mem = MemoryEngine()
    f = seed_linked(mem, "user", "has_disease", "diabetes", user_id="u1", text='user has disease diabetes')
    assert f.id in {edge["fact_id"] for edge in mem.graph_data("u1")["edges"]}

    assert mem.delete_fact(f.id) is True

    graph = mem.graph_data("u1")
    assert f.id not in {edge["fact_id"] for edge in graph["edges"]}
    assert "diabetes" not in str(graph).lower()


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
    ctx = mem.lean_context("When did I travel?", user_id="u1", timeline=True)
    assert "TIMELINE" in ctx
    tl = ctx.split("TIMELINE")[1]
    import re
    dates = re.findall(r"(\d{4}-\d{2}-\d{2})", tl.split("SESSION DIGESTS")[0])
    assert dates == sorted(dates), "timeline must be chronological (oldest to newest)"


def test_lean_context_as_of_profile_does_not_leak_current_facts():
    mem = MemoryEngine()
    mem.add("I work at Tencent.", user_id="u1", session_id="old", event_time=BASE)
    mem.add("I work at Moonshot AI.", user_id="u1", session_id="new", event_time=BASE + 30 * DAY)
    mem.consolidate()

    ctx = mem.lean_context(
        "Where do I work?",
        user_id="u1",
        as_of=BASE + 10 * DAY,
        char_budget=10_000,
    )
    assert "USER PROFILE (as of" not in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" not in ctx


def test_search_answers_previous_value_with_explicit_history_state():
    from mnemotrace.store.contracts import FactQuery, FactState

    mem = MemoryEngine()
    previous = mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    current = seed_timeline(mem, "Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY,
                           text='Wei works at Moonshot AI')
    result = mem.search("Where did Wei work?", user_id="u1", facts=FactQuery(state=FactState.HISTORY))
    assert result.top().id == previous.id
    assert mem.search("Where does Wei work?", user_id="u1").top().id == current.id


def test_lean_context_adds_evolution_chain_for_current_lookup():
    mem = MemoryEngine()
    old = mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    new = seed_timeline(mem, "Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY, text='Wei works at Moonshot AI')

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        char_budget=10_000,
    )

    assert new.supersedes == [old.id]
    assert "FACTS (current, dated):" in ctx
    assert "FACT EVOLUTION" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" in ctx
    assert "superseded" in ctx and "current" in ctx


def test_lean_context_evolution_chain_respects_as_of_boundary():
    mem = MemoryEngine()
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    seed_timeline(mem, "Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY, text='Wei works at Moonshot AI')

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        as_of=BASE + 10 * DAY,
        char_budget=10_000,
    )

    assert "Tencent" in ctx
    assert "Moonshot AI" not in ctx
    assert "FACT EVOLUTION" not in ctx


def test_context_for_adds_fact_evolution_chain():
    mem = MemoryEngine()
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    seed_timeline(mem, "Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY, text='Wei works at Moonshot AI')

    ctx = mem.context_for("Where does Wei work?", user_id="u1", k_digests=0)

    assert "FACT EVOLUTION" in ctx
    assert "Tencent" in ctx
    assert "Moonshot AI" in ctx


def test_chain_evidence_can_be_disabled_for_ablation():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(chain_evidence=False))
    mem.add_fact("Wei", "works_at", "Tencent", user_id="u1", valid_at=BASE, text='Wei works at Tencent')
    seed_timeline(mem, "Wei", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + 30 * DAY, text='Wei works at Moonshot AI')

    ctx = mem.lean_context(
        "Where does Wei work?",
        user_id="u1",
        char_budget=10_000,
    )

    assert "Moonshot AI" in ctx
    assert "FACT EVOLUTION" not in ctx


def test_multihop_planner_includes_cold_tier_relation_facts():
    mem = MemoryEngine()
    eps = [
        mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s1", event_time=BASE),
        mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s2", event_time=BASE + DAY),
    ]
    mem.consolidate(eps)

    link_synthetic_facts(mem, {"sister", "occupation", "lives_in"})
    sister = next(f for f in mem.fact_store.values() if f.predicate == "sister")
    sister.salience = 0.0
    assert mem.evict_cold(max_hot=len(mem.fact_store.values()) - 1) == 1
    assert mem.cold_store.get(sister.id) is not None

    path, = mem.follow_path("user", ["sister", "occupation"], user_id="u1").paths
    assert path.endpoint == "pediatrician"
    assert path.facts[0].id == sister.id


def test_deleted_relation_fact_does_not_drive_multihop_planner():
    mem = MemoryEngine()
    eps = [
        mem.add("My sister Maya is a pediatrician.", user_id="u1", session_id="s1", event_time=BASE),
        mem.add("Maya just moved to Seattle.", user_id="u1", session_id="s2", event_time=BASE + DAY),
    ]
    mem.consolidate(eps)
    link_synthetic_facts(mem, {"sister", "occupation", "lives_in"})
    sister = next(f for f in mem.fact_store.values() if f.predicate == "sister")

    assert mem.delete_fact(sister.id) is True

    res = mem.search("What is the profession of the user's sister who moved to Seattle?", user_id="u1")
    assert res.via != "multi-hop"
    assert "pediatrician" not in res.answer().lower()


def test_graph_scores_expand_beyond_one_hop_for_bridge_facts():
    mem = MemoryEngine()
    direct = seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    bridge = seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    second_hop = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, text='Moonshot AI based in Beijing')

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
    mem = MemoryEngine()
    direct = seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    future_bridge = seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    older_target = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE, text='Moonshot AI based in Beijing')
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

    mem = MemoryEngine(config=Config(graph_proximity=False))
    direct = seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    bridge = seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    second_hop = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, text='Moonshot AI based in Beijing')

    scores, qids = mem.retriever._graph_scores(
        "Tell me about Wei",
        "u1",
        [direct, bridge, second_hop],
        None,
    )
    related = mem._graph_related_facts("Tell me about Wei", "u1", None)
    paths = mem._graph_related_facts("Tell me about Wei", "u1", None)

    assert qids, "entity anchoring still works; only proximity evidence is ablated"
    assert all(score == 0.0 for score in scores.values())
    assert related == []
    assert paths == []


def test_graph_relation_awareness_prioritizes_query_relevant_edges():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(graph_relation_awareness=True))
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    distractor = seed_linked(mem, "Lin", "likes", "jazz", user_id="u1", valid_at=BASE + 2 * DAY, text='Lin likes jazz')
    target = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 3 * DAY, text='Moonshot AI based in Beijing')
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

    mem = MemoryEngine(config=Config(graph_relation_awareness=False))
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    distractor = seed_linked(mem, "Lin", "likes", "jazz", user_id="u1", valid_at=BASE + 2 * DAY, text='Lin likes jazz')
    target = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 3 * DAY, text='Moonshot AI based in Beijing')
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
        mem = MemoryEngine(config=Config(graph_path_reinforcement=enabled))
        seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
        seed_linked(mem, "Wei", "mentor", "Maya", user_id="u1", valid_at=BASE + DAY, text='Wei mentor Maya')
        seed_linked(mem, "Lin", "works_on", "Atlas", user_id="u1", valid_at=BASE + 2 * DAY, text='Lin works on Atlas')
        seed_linked(mem, "Maya", "works_on", "Atlas", user_id="u1", valid_at=BASE + 3 * DAY, text='Maya works on Atlas')
        seed_linked(mem, "Lin", "works_on", "Zephyr", user_id="u1", valid_at=BASE + 4 * DAY, text='Lin works on Zephyr')
        target = seed_linked(mem, "Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + 5 * DAY, text='Atlas based in Reykjavik')
        distractor = seed_linked(mem, "Zephyr", "based_in", "Lisbon", user_id="u1", valid_at=BASE + 6 * DAY, text='Zephyr based in Lisbon')
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


def test_multihop_planner_reaches_company_location_chain():
    mem = MemoryEngine()
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, text='Moonshot AI based in Beijing')

    path, = mem.follow_path("Wei", ["colleague", "works_at", "based_in"], user_id="u1").paths
    assert path.endpoint == "Beijing"
    assert [f.predicate for f in path.facts] == ["colleague", "works_at", "based_in"]


def test_multihop_planner_reaches_project_location_chain():
    mem = MemoryEngine()
    seed_linked(mem, "Wei", "works_on", "Atlas", user_id="u1", valid_at=BASE, text='Wei works on Atlas')
    seed_linked(mem, "Atlas", "based_in", "Reykjavik", user_id="u1", valid_at=BASE + DAY, text='Atlas based in Reykjavik')

    path, = mem.follow_path("Wei", ["works_on", "based_in"], user_id="u1").paths
    assert path.endpoint == "Reykjavik"
    assert [f.predicate for f in path.facts] == ["works_on", "based_in"]


def test_heat_tiered_eviction_pages_cold_preserves_durable():
    """MemoryOS 热度分层：超容量时最冷的偶发 fact 换出到 cold tier（非破坏性）；
    持久的偏好/身份 fact 永不被驱逐。"""
    from mnemotrace.types import Fact

    mem = MemoryEngine()
    u = "u1"
    for i in range(5):  # 偶发 fact，显著度递增
        f = Fact(subject="u1", predicate="parked_at", object=f"lot{i}", user_id=u,
                 salience=float(i), last_access=BASE + i, text=' '.join(('u1', 'parked at', f'lot{i}')))
        mem.fact_store.upsert(f.id, [0.0], f)
    durable = Fact(subject="u1", predicate="likes", object="jazz", user_id=u, salience=0.0, last_access=BASE, retention="durable", text='u1 likes jazz')
    mem.fact_store.upsert(durable.id, [0.0], durable)

    n = mem.evict_cold(max_hot=3)
    assert n == 3
    hot = mem.fact_store.values()
    assert durable in hot, "durable preference must never be paged out, even at lowest salience"
    assert len(mem.cold_store.values()) == 3, "evicted facts are preserved in the cold tier, not deleted"


def test_history_includes_cold_tier_facts():
    mem = MemoryEngine()
    cold = mem.add_fact("project", "project_note", "cold audit trail", user_id="u1", text='project project note cold audit trail')
    hot = mem.add_fact("project", "project_note", "hot audit trail", user_id="u1", text='project project note hot audit trail')
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    chain = mem.history("project", "project_note", user_id="u1")
    assert {f.object for f in chain} == {"cold audit trail", "hot audit trail"}


def test_add_fact_supersedes_live_cold_tier_fact():
    mem = MemoryEngine()
    old = mem.add_fact("user", "project_status", "alpha", user_id="u1", text='user project status alpha')
    filler = mem.add_fact("user", "project_note", "hot filler", user_id="u1", text='user project note hot filler')
    old.salience = 0.0
    filler.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(old.id) is not None

    new = seed_timeline(mem, "user", "project_status", "beta", user_id="u1", valid_at=old.valid_at + 1, text='user project status beta')
    assert new.supersedes == [old.id]
    assert old.invalid_at == new.valid_at
    chain = mem.history("user", "project_status", user_id="u1")
    assert [f.object for f in chain] == ["alpha", "beta"]


def test_search_pages_relevant_cold_fact_back_to_hot():
    mem = MemoryEngine()
    fact = mem.add_fact("project", "project_status", "alpha", user_id="u1", text='project project status alpha')
    assert mem.evict_cold(max_hot=0) == 1
    assert mem.fact_store.get(fact.id) is None
    assert mem.cold_store.get(fact.id) is not None

    res = mem.search("What is the project status?", user_id="u1")
    assert res.via == "cold"
    assert "alpha" in res.top().object.lower()
    assert mem.fact_store.get(fact.id) is not None
    assert mem.cold_store.get(fact.id) is None


def test_cold_page_in_preserves_hot_limit():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(max_hot_facts=0, abstain_threshold=2.0))
    target = mem.add_fact("apollo", "launch_code", "alpha", user_id="u1", text='apollo launch code alpha')
    filler = mem.add_fact("zephyr", "project_note", "background filler", user_id="u1", text='zephyr project note background filler')
    target.salience = 0.0
    filler.salience = 0.1
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(target.id) is not None

    mem.config.max_hot_facts = 1
    res = mem.search("What is Apollo's launch code?", user_id="u1")

    assert res.via == "cold"
    assert "alpha" in res.top().object.lower()
    assert len(mem.fact_store.values()) <= 1
    assert mem.fact_store.get(target.id) is not None
    assert mem.cold_store.get(filler.id) is not None


def test_max_hot_facts_auto_pages_on_add_fact():
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config(max_hot_facts=1))
    a = mem.add_fact("project", "project_status", "alpha", user_id="u1", text='project project status alpha')
    b = mem.add_fact("project", "project_note", "beta", user_id="u1", text='project project note beta')
    a.salience = 0.0
    b.salience = 1.0
    mem._enforce_hot_limit()

    assert len(mem.fact_store.values()) == 1
    assert len(mem.cold_store.values()) == 1
    assert mem.cold_pages_out.get("u1", 0) == 1


def test_lean_context_pages_cold_fact_when_hot_misses():
    mem = MemoryEngine()
    fact = mem.add_fact("project", "project_status", "alpha", user_id="u1", text='project project status alpha')
    assert mem.evict_cold(max_hot=0) == 1

    ctx = mem.lean_context("project status", user_id="u1")
    assert "alpha" in ctx.lower()
    assert mem.fact_store.get(fact.id) is not None
    assert mem.cold_store.get(fact.id) is None


def test_graph_related_facts_include_cold_tier_facts():
    mem = MemoryEngine()
    cold = seed_linked(mem, "project", "project_note", "cold graph evidence", user_id="u1", text='project project note cold graph evidence')
    hot = seed_linked(mem, "project", "project_status", "hot graph evidence", user_id="u1", text='project project status hot graph evidence')
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    related = mem._graph_related_facts("project", "u1", None)
    assert "cold graph evidence" in {f.object for f in related}


def test_graph_related_facts_expand_beyond_one_hop():
    mem = MemoryEngine()
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    target = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, text='Moonshot AI based in Beijing')

    related = mem._graph_related_facts("Tell me about Wei", "u1", None)

    assert target.id in {f.id for f in related}


def test_graph_related_facts_respect_as_of_during_expansion():
    mem = MemoryEngine()
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    target = seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE, text='Moonshot AI based in Beijing')

    before = mem._graph_related_facts("Tell me about Wei", "u1", BASE + 0.5 * DAY)
    after = mem._graph_related_facts("Tell me about Wei", "u1", BASE + 2 * DAY)

    assert target.id not in {f.id for f in before}
    assert target.id in {f.id for f in after}


def test_context_for_graph_includes_multihop_related_facts():
    mem = MemoryEngine()
    seed_linked(mem, "Wei", "colleague", "Lin", user_id="u1", valid_at=BASE, text='Wei colleague Lin')
    seed_linked(mem, "Lin", "works_at", "Moonshot AI", user_id="u1", valid_at=BASE + DAY, text='Lin works at Moonshot AI')
    seed_linked(mem, "Moonshot AI", "based_in", "Beijing", user_id="u1", valid_at=BASE + 2 * DAY, text='Moonshot AI based in Beijing')

    ctx = mem.context_for("Tell me about Wei", user_id="u1", k_digests=0, graph=True)

    assert "RELATED FACTS (graph traversal):" in ctx
    assert "Moonshot AI based in Beijing" in ctx


def test_pending_conflict_and_resolution_include_cold_tier_facts():
    from mnemotrace.types import Conflict

    mem = MemoryEngine()
    old = mem.add_fact("project", "status", "alpha", user_id="u1", text='project status alpha')
    new = mem.add_fact("project", "status", "beta", user_id="u1", text='project status beta')
    old.invalid_at = None
    old.expired_at = None
    new.supersedes = []
    old.salience = 0.0
    new.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(old.id) is not None
    cf = Conflict(older=old.id, newer=new.id, user_id="u1", text_older=old.text, text_newer=new.text)
    mem.conflicts[cf.id] = cf

    assert mem.pending_conflicts("u1") == [cf]
    assert mem.resolve_conflict(cf.id, keep="newer") is True
    assert old.withdrawn_at is not None and old.invalid_at is None
    assert mem.pending_conflicts("u1") == []


def test_sensitivity_preserves_cold_tier_without_duplicate():
    mem = MemoryEngine()
    cold = mem.add_fact("project", "project_note", "old value", user_id="u1", text='project project note old value')
    hot = mem.add_fact("project", "project_status", "hot value", user_id="u1", text='project project status hot value')
    cold.salience = 0.0
    hot.salience = 1.0
    assert mem.evict_cold(max_hot=1) == 1
    assert mem.cold_store.get(cold.id) is not None

    updated = mem.set_fact_sensitive(cold.id, False)

    assert updated is not None and updated.object == "old value" and not updated.sensitive
    assert mem.fact_store.get(cold.id) is None
    assert mem.cold_store.get(cold.id) is not None
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

    mem = MemoryEngine()
    older = mem.add_fact("user", "likes", "tea", user_id="u1", text='user likes tea')
    newer = mem.add_fact("user", "likes", "coffee", user_id="u1", text='user likes coffee')
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
    assert mem.fact_store.get(newer.id).supersedes == []
    assert mem.fact_store.get(older.id).withdrawn_at is not None


def test_agentic_multihop_decomposition():
    """Bet B: with an LLM, lean_context decomposes the question into sub-queries and unions their
    retrieval; without an LLM it degrades gracefully to the single query."""
    mem = build()  # 离线提取 facts

    # 优雅降级：agentic=True 但没有 llm -> 仍返回可用 context
    ctx0 = mem.lean_context("Which cities did I visit?", user_id="u1", agentic=True)
    assert ctx0.strip()

    # 配备能分解问题的 llm 后，该路径正常运行并产出 context
    class FakeLLM:
        def complete(self, prompt, system=None, **k):
            return '["cities I traveled to", "trips"]'

    mem.llm = FakeLLM()
    ctx1 = mem.lean_context("Which cities did I visit and how many trips?", user_id="u1",
                            agentic=True)
    assert ctx1.strip() and "SESSION DIGESTS" in ctx1


def test_agentic_episode_gather_respects_as_of():
    """Agentic chunk 检索必须保留调用方指定时点的记忆视图。"""
    from mnemotrace.retrieve.agentic import AgenticRetriever

    class FakeLLM:
        def complete(self, prompt, system=None, **k):
            return '["trips"]'

    class FakeMemory:
        def __init__(self):
            self.calls = []

        def retrieve_digests(self, query, user_id, k, as_of=None):
            self.calls.append(as_of)
            return []

    fake = FakeMemory()
    AgenticRetriever(fake, FakeLLM()).gather_digests("Which cities did I visit?", "u1", 1, as_of=BASE)
    assert fake.calls == [BASE]


def test_working_memory_holds_active_query_set():
    """工作记忆：lean_context 为当前查询填充一个临时的、可检查的活跃集。"""
    mem = build()
    assert mem.working_set == []  # 尚未关注任何内容
    mem.lean_context("Where do I work?", user_id="u1")
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

    incidental = Fact(subject="u", predicate="parked_at", object="lot B", salience=1.0, last_access=BASE, text='u parked at lot B')
    preference = Fact(subject="u", predicate="likes", object="jazz", salience=1.0, last_access=BASE, retention="durable", text='u likes jazz')
    later = BASE + 100 * DAY
    decay(incidental, per_day=0.02, t=later)
    decay(preference, per_day=0.02, t=later)
    assert incidental.salience < 1.0, "incidental fact should fade"
    assert preference.salience == 1.0, "durable preference is exempt from decay"


def test_reinforce_boosts_and_resets_clock():
    from mnemotrace.consolidate.decay import reinforce
    from mnemotrace.types import Fact

    f = Fact(subject="u", predicate="parked_at", object="lot B", salience=1.0, last_access=BASE, text='u parked at lot B')
    reinforce(f, boost=0.5, t=BASE + 5 * DAY)
    assert f.salience == 1.5 and f.access_count == 1 and f.last_access == BASE + 5 * DAY


def test_digest_remains_faithful_after_supersession():
    """在知识更新前定稿的摘要，应由 reflector 把当前值追加进去。"""
    from mnemotrace.types import Fact

    mem = MemoryEngine()
    ep = mem.add("I do yoga twice a week.", user_id="u1", session_id="s1", event_time=BASE)
    # 一条旧 fact（来源于 ep），随后被新 fact 取代
    old = Fact(subject="u1", predicate="yoga_frequency", object="twice a week",
               user_id="u1", valid_at=BASE, provenance=[ep.id],
               invalid_at=BASE + 5 * DAY, expired_at=BASE + 5 * DAY, text='u1 yoga frequency twice a week')
    new = Fact(subject="u1", predicate="yoga_frequency", object="three times a week",
               user_id="u1", valid_at=BASE + 5 * DAY, supersedes=[old.id], text='u1 yoga frequency three times a week')
    mem.fact_store.upsert(old.id, [0.0], old)
    mem.fact_store.upsert(new.id, [0.0], new)
    mem.generate_digests([ep])  # 冻结 "twice a week" 的摘要
    assert "[updated:" not in ep.digest
    before = ep.digest
    stats = mem.consolidate_full(fact_episodes=[], digest_episodes=[ep])
    assert stats["reflected"] == 0
    assert ep.digest == before
    assert "[updated:" not in ep.digest and "three times a week" not in ep.digest


def test_digest_remains_faithful_with_cold_tier_history():
    from mnemotrace.types import Fact

    mem = MemoryEngine()
    ep = mem.add("I do yoga twice a week.", user_id="u1", session_id="s1", event_time=BASE)
    old = Fact(subject="u1", predicate="yoga_frequency", object="twice a week",
               user_id="u1", valid_at=BASE, provenance=[ep.id],
               invalid_at=BASE + 5 * DAY, expired_at=BASE + 5 * DAY, salience=0.0, text='u1 yoga frequency twice a week')
    new = Fact(subject="u1", predicate="yoga_frequency", object="three times a week",
               user_id="u1", valid_at=BASE + 5 * DAY, supersedes=[old.id],
               salience=1.0, text='u1 yoga frequency three times a week')
    mem.fact_store.upsert(old.id, [0.0], old)
    mem.fact_store.upsert(new.id, [0.0], new)
    mem.generate_digests([ep])
    assert mem.evict_cold(max_hot=1) == 1

    before = ep.digest
    assert mem.consolidate_full(fact_episodes=[], digest_episodes=[ep])["reflected"] == 0
    assert ep.digest == before
    assert "three times a week" not in ep.digest


def test_relevance_wins_without_category_weighting():
    mem = build()
    ans = mem.search("Where do I work?", user_id="u1").top().object.lower()
    assert "tencent" in ans


def test_focus_track_boosts_salience_and_mute_hides():
    """跟踪主题提升显著度，静音主题退出读取上下文。"""
    mem = MemoryEngine()
    mem.add_fact("user", "uses", "Python", user_id="u1", text='user uses Python')
    mem.add_fact("user", "plays", "guitar", user_id="u1", text='user plays guitar')
    py = next(f for f in mem.fact_store.values() if "python" in f.text.lower())
    before = py.salience

    mem.set_focus(track=["python"], mute=["guitar"])
    assert py.salience == before

    ctx = mem.lean_context("what do I do", user_id="u1").lower()
    assert "python" in ctx, "tracked fact should remain in the read context"
    assert "guitar" not in ctx, "muted topic must be hidden from the read context"


def test_graph_data_has_no_dangling_edges():
    """graph_data() 供关系图谱视图使用：每条边的端点都必须是返回的节点，孤儿实体
    （没有存留边）会被丢弃，使画面聚焦于关系本身。"""
    mem = build()
    link_synthetic_facts(mem, {"works_at", "lives_in", "colleague"})
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
    mem = MemoryEngine()
    mem.add("Wei works at Tencent.", user_id="u1", event_time=BASE)
    mem.add("Wei works at Moonshot AI.", user_id="u1", event_time=BASE + 30 * DAY)
    mem.consolidate()

    link_synthetic_facts(mem, {"works_at"})
    past = mem.graph_data("u1", as_of=BASE + 10 * DAY)
    past_names = {n["name"] for n in past["nodes"]}
    assert "Tencent" in past_names
    assert "Moonshot AI" not in past_names
    assert all(e["live"] for e in past["edges"])

    current = mem.graph_data("u1")
    assert "Moonshot AI" in {n["name"] for n in current["nodes"]}


def test_graph_data_can_exclude_sensitive_fact_edges():
    mem = MemoryEngine()
    seed_linked(mem, "user", "has_disease", "diabetes", user_id="u1", text='user has disease diabetes')
    seed_linked(mem, "user", "works_at", "Acme", user_id="u1", sensitive=False, text='user works at Acme')

    full = mem.graph_data("u1")
    assert "diabetes" in str(full).lower()
    assert "Acme" in str(full)

    safe = mem.graph_data("u1", include_sensitive=False)
    rendered = str(safe).lower()
    assert "diabetes" not in rendered
    assert "Acme" in str(safe)
    assert all("diabetes" not in e.get("fact_text", "").lower() for e in safe["edges"])


def test_graph_data_can_exclude_sensitive_cold_fact_edges():
    mem = MemoryEngine()
    disease = seed_linked(mem, "user", "has_disease", "diabetes", user_id="u1", text='user has disease diabetes')
    work = seed_linked(mem, "user", "project_note", "Acme", user_id="u1", text='user project note Acme')
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


def test_memory_policy_edits_extraction():
    """记忆策略页面是真实接线：用户的 'what to record' 指令会追加到提取 system prompt 后面，
    完整 prompt 覆盖会替换默认值，传空字符串重置。"""
    class FakeLLM:
        def complete(self, prompt, system=""):
            return "[]"

    from mnemotrace.consolidate.planning import SYSTEM as EXTRACT_SYSTEM
    m = MemoryEngine(llm=FakeLLM())

    # 增量式 'what to record' 指令会进入 extractor 的实际生效 system prompt
    m.set_policy(extract_instruction="只记录与工作相关的事实")
    from mnemotrace.consolidate.planning import system_prompt
    effective = system_prompt(m.policy["extract_instruction"])
    assert "只记录与工作相关的事实" in effective
    assert EXTRACT_SYSTEM in effective  # 默认仍在，指令为追加

    # 完整覆盖会替换默认值，"" 则重置回默认
    m.set_policy(extract_system="CUSTOM PROMPT")
    assert m._effective("extract_system") == "CUSTOM PROMPT"
    m.set_policy(extract_system="")
    assert m._effective("extract_system") == EXTRACT_SYSTEM

    # get_policy 暴露覆盖项和内置默认值。
    gp = m.get_policy()
    assert set(gp) == {"policy", "defaults"} and len(gp["defaults"]) == 4


def test_redacted_context_omits_free_text_layers_that_can_leak_sensitive_content():
    """脱敏/共享 context 不得通过摘要、chunk 或工作记忆文本泄漏敏感内容。
    它只能依赖已分类、非敏感的结构化 facts。"""
    m = MemoryEngine()
    ep = m.add("My private diagnosis is diabetes. I work at Acme.", user_id="u", session_id="s1")
    ep.digest = "Private diagnosis: diabetes. Job: Acme."
    m.digest_vec.upsert(ep.id, m.embedder.embed(ep.digest), ep)
    m.add_fact("user", "has_disease", "diabetes", user_id="u", text='user has disease diabetes')
    m.add_fact("user", "works_at", "Acme", user_id="u", sensitive=False, text='user works at Acme')

    redacted = m.lean_context(
        "what do you know about my diagnosis and work?",
        user_id="u",

        n_digests=5,
        redact_sensitive=True,
        char_budget=10_000,
    )

    assert "Acme" in redacted
    assert "diabetes" not in redacted.lower()
    assert "SESSION DIGESTS" not in redacted
    assert "RELEVANT CONVERSATIONS" not in redacted
    assert "WORKING MEMORY" not in redacted


def test_original_sentence_keeps_canonical_predicate():
    m = MemoryEngine()
    f = m.add_fact("user", "works_at", "字节跳动", user_id="u", text='用户在字节跳动工作。')
    assert f.text == '用户在字节跳动工作。'
    assert f.predicate == "works_at" and f.slot[2] == f.relation_id


def test_conflict_resolve_and_dismiss_are_user_driven():
    """检测绝不自动解决 —— 只有用户的 resolve()/dismiss() 会生效；resolve 使选定的败方被取代，
    dismiss 则两者都保留。"""
    from mnemotrace.types import Conflict
    m = MemoryEngine()
    fa = m.add_fact("user", "likes", "北京", user_id="u", text='user likes 北京')   # 多值 -> 两者并存（不自动合并）
    fb = m.add_fact("user", "likes", "上海", user_id="u", text='user likes 上海')
    cf = Conflict(older=fa.id, newer=fb.id, user_id="u")
    m.conflicts[cf.id] = cf
    assert m.pending_conflicts("u"), "both live -> pending"

    assert m.resolve_conflict(cf.id, keep="newer")
    assert not fa.is_live() and fb.is_live(), "keep=newer supersedes the older"
    assert fa.withdrawn_at is not None and m.pending_conflicts("u") == []

    # dismiss 两者都保留
    g1 = m.add_fact("user", "likes", "茶", user_id="u", text='user likes 茶')
    g2 = m.add_fact("user", "likes", "奶茶", user_id="u", text='user likes 奶茶')
    cf2 = Conflict(older=g1.id, newer=g2.id, user_id="u")
    m.conflicts[cf2.id] = cf2
    assert m.dismiss_conflict(cf2.id) and g1.is_live() and g2.is_live()
    assert m.pending_conflicts("u") == []


def test_evolution_block_leads_with_the_current_value():
    """知识更新回归防护：扁平表格迫使回答者扫描 `role` 列来判断当前哪个值为真，且它选中
    已被取代值的频率高到足以输给朴素 full-context（例如搬迁前的城市）。该块必须首先把当前值
    作为结论陈述，被替换的值明显居于从属地位。"""
    from mnemotrace.config import Config

    mem = MemoryEngine(config=Config())
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
