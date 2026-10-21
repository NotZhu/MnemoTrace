"""多跳规划器测试（AGENTS.md Bet B）。在一张手工搭建的小图上驱动新的 LLM 分解路径及其各层回退，
因此是确定性的、无需真实模型。

被测链条：Wei -colleague-> Lei -works_at-> Moonshot。两跳问题（"where does Wei's colleague work?"）
必须解析出 Moonshot。覆盖：LLM 计划沿图行走、捏造的谓词被丢弃、格式错误的输出回退到关键词分解器、
fact-store 桥接找回缺失边、以及离线路径保持逐字节不变（无桥接、无 LLM）。"""
from __future__ import annotations

from mnemotrace.config import Config
from mnemotrace.llm.fake import FakeLLM
from mnemotrace.retrieve.planner import MultiHopPlanner
from mnemotrace.store.memory_store import InMemoryGraphStore, InMemoryVectorStore
from mnemotrace.types import Entity, Fact, Relation

_Q = "Where does Wei's colleague work?"  # 词干化为 wei/colleague/work -> 得到锚点并做关键词分解


def _build(bridge_last: bool = False):
    """Wei -colleague-> Lei -works_at-> Moonshot。当 bridge_last=True 时，works_at 这一跳只以
    Fact 形式存在（没有图边），规划器必须回退到 fact-store 桥接才能补全链条。"""
    g = InMemoryGraphStore()
    fs = InMemoryVectorStore()
    ents = {name: g.upsert_entity(Entity(name=name, user_id="u")) for name in ("Wei", "Lei", "Moonshot")}

    def add(subj, pred, obj, edge=True):
        f = Fact(subject=subj, predicate=pred, object=obj, user_id="u")
        fs.upsert(f.id, [0.0], f)  # 规划器从不调用嵌入；哑向量即可
        if edge:
            g.add_relation(Relation(subject_id=ents[subj].id, predicate=pred,
                                    object_id=ents[obj].id, fact_id=f.id))
        return f

    add("Wei", "colleague", "Lei")
    add("Lei", "works_at", "Moonshot", edge=not bridge_last)
    return g, fs


def _planner(g, fs, llm=None):
    return MultiHopPlanner(g, fs, Config(), llm=llm)


def test_llm_plan_walks_the_graph():
    g, fs = _build()
    llm = FakeLLM(responses=['{"anchor":"Wei","predicates":["colleague","works_at"]}'])
    res = _planner(g, fs, llm).plan(_Q, "u")
    assert res is not None
    assert res.answer == "Moonshot"
    assert res.chain == ["colleague", "works_at"]
    assert len(res.facts) >= 2  # 两跳的证据都收集到了


def test_invented_predicate_is_dropped_then_keyword_fallback_recovers():
    g, fs = _build()
    # 'bogus_pred' 不在用户的谓词表里 -> 被过滤 -> 只剩 1 个有效谓词 -> LLM 计划被拒绝
    # -> 确定性的关键词分解器接管，仍能解析出整条链。
    llm = FakeLLM(responses=['{"anchor":"Wei","predicates":["colleague","bogus_pred"]}'])
    res = _planner(g, fs, llm).plan(_Q, "u")
    assert res is not None and res.answer == "Moonshot"


def test_malformed_llm_output_falls_back_to_keywords():
    g, fs = _build()
    res = _planner(g, fs, FakeLLM(responses=["I cannot help with that."])).plan(_Q, "u")
    assert res is not None and res.answer == "Moonshot"


def test_bridge_recovers_a_missing_graph_edge():
    g, fs = _build(bridge_last=True)  # works_at 只以 Fact 形式存在，没有图边
    llm = FakeLLM(responses=['{"anchor":"Wei","predicates":["colleague","works_at"]}'])
    res = _planner(g, fs, llm).plan(_Q, "u")
    assert res is not None and res.answer == "Moonshot"
    assert res.chain == ["colleague", "works_at"]


def test_offline_path_is_unchanged_no_bridge():
    # llm=None：关键词分解器，桥接默认关闭。缺失边意味着给不出有把握的多跳答案，
    # 于是 plan() 返回 None，search() 落回 hybrid（既有行为）。
    g, fs = _build(bridge_last=True)
    assert _planner(g, fs, None).plan(_Q, "u") is None


def test_offline_path_resolves_when_edges_present():
    g, fs = _build()
    res = _planner(g, fs, None).plan(_Q, "u")
    assert res is not None and res.answer == "Moonshot"


def test_single_hop_question_is_not_planned():
    g, fs = _build()
    # 一个谓词 -> 不是多跳 -> None（交给 hybrid 处理）
    assert _planner(g, fs, None).plan("Where does Wei live?", "u") is None


def test_unknown_anchor_returns_none():
    g, fs = _build()
    # LLM 给出的锚点不在图里，查询里也没有实体词 -> 没有锚点 -> None
    llm = FakeLLM(responses=['{"anchor":"Nobody","predicates":["colleague","works_at"]}'])
    assert _planner(g, fs, llm).plan("Where does the colleague work and live?", "u") is None
