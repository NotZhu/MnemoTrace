"""Read and edit historical conclusion facts, and audit scoped fact slots."""
from __future__ import annotations
from tests.model_fixtures import saved_outcome

import json

from mnemotrace.consolidate.outcomes import (
    OUTCOME_CATEGORY,
    OUTCOME_PREDICATES,
    split_outcome_text,
)
from tests.model_fixtures import Memory
from tests.model_fixtures import MemoryService
from mnemotrace.types import Fact


def test_split_outcome_text_is_the_exact_inverse_of_the_format():
    f = saved_outcome(Memory(), user_id="u", session_id="s1", kind="finding",
                      statement="client.ts declares its own copy of the API types",
                      why="a contract change failed at runtime, not compile time")
    assert split_outcome_text(f.text) == (
        "client.ts declares its own copy of the API types",
        "a contract change failed at runtime, not compile time",
    )
    # 只记录陈述、没有依据的条目，往返解析后 why 应为空串，而不是多出一个孤立的括号
    assert split_outcome_text("just a statement") == ("just a statement", "")


# --- service 层：console 和 MCP 客户端实际看到的 outcome 层 ---

def _svc(tmp_path):
    return MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")


def _seed_conclusion(svc, response):
    with svc.write_lock("u"):
        mem = svc.get("u")
        saved_outcome(mem, user_id="u", session_id="s1", **response)
        svc._save("u", mem)


def test_kind_partitions_the_fact_set_exactly(tmp_path):
    response = json.loads('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件",'
                  '"why":"四个 agent 掉线"}]')[0]
    svc = _svc(tmp_path)
    svc.add_fact("u", "user", "occupation", "engineer")
    _seed_conclusion(svc, response)

    everything = svc.memories("u")
    outcomes = svc.memories("u", kind="outcomes")
    attributes = svc.memories("u", kind="attributes")
    ids = lambda page: {f["id"] for f in page["facts"]}  # noqa: E731
    assert ids(outcomes) and ids(attributes)
    assert ids(outcomes) | ids(attributes) == ids(everything)
    assert not (ids(outcomes) & ids(attributes))
    assert all(f["predicate"] in OUTCOME_PREDICATES for f in outcomes["facts"])
    # counts 是在未过滤的全集上算的，所以三个视图之间保持一致
    assert everything["counts"]["facts_outcomes"] == 1
    assert attributes["counts"]["facts_outcomes"] == 1


def test_unknown_kind_is_ignored_not_rejected(tmp_path):
    """与 `status` 同一套契约：过期的客户端传来我们不认识的值，仍能拿到它的记忆。"""
    svc = _svc(tmp_path)
    svc.add_fact("u", "user", "occupation", "engineer")
    assert len(svc.memories("u", kind="banana")["facts"]) == 1
    assert len(svc.memories("u", kind="")["facts"]) == 1


def test_outcome_facts_carry_their_evidence_split_out(tmp_path):
    response = json.loads('[{"kind":"finding","statement":"共享机上不要执行 docker build","why":"会重建别人的容器"}]')[0]
    svc = _svc(tmp_path)
    svc.add_fact("u", "user", "occupation", "engineer")
    _seed_conclusion(svc, response)
    row = svc.memories("u", kind="outcomes")["facts"][0]
    assert row["why"] == "会重建别人的容器"
    assert svc.memories("u", kind="attributes")["facts"][0]["why"] == ""


def test_outcomes_never_reach_the_graph(tmp_path):
    """结论的 object 是一个句子、subject 是会话 id，建边的话每条陈述会铸造一个垃圾实体、每个会话
    再铸一个——然后 audit 还会把它们报成孤儿节点。"""
    response = json.loads('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件","why":"四个 agent 掉线"}]')[0]
    svc = _svc(tmp_path)
    _seed_conclusion(svc, response)
    names = {n["name"] for n in svc.graph("u", include_sensitive=True)["nodes"]}
    assert "s1" not in names
    assert not any("重新配对会覆盖" in n for n in names)


def test_audit_reports_one_slot_overflow_and_leaves_outcomes_alone(tmp_path):
    """owner 的真实存储里躺着 84 条存活的 `occupation` fact。每一行都能通过逐条规则；缺陷只在
    槽位层面可见。报告一次，而不是 84 次。"""
    response = json.loads('[{"kind":"lesson","statement":"配置文件路径 ~/.cumora/computer.json 没有覆盖机制",'
                  '"why":"跑 --pair 覆盖了线上那份"}]')[0]
    svc = _svc(tmp_path)
    _seed_conclusion(svc, response)
    # 直接写进存储，而不走 add_fact：冲突消解会在这个单值槽位上逐条 supersede 前值，而 field 现场
    # 恰恰没有发生这种收敛——逐轮抽取器让 84 条全部存活，这才是该检查必须捕获的状态。
    mem = svc.get("u")
    for i in range(10):
        junk = Fact(subject="user", predicate="occupation", object=f"borrow from big tech {i}",
                    user_id="u")
        junk.embedding = mem.embedder.embed(junk.text)
        mem.fact_store.upsert(junk.id, junk.embedding, junk)

    audit = svc.audit("u", limit=200)
    overflow = [r for r in audit["findings"] if r["kind"] == "slot_overflow"]
    assert len(overflow) == 1 and audit["by_kind"]["slot_overflow"] == 1
    assert overflow[0]["count"] == 10 and overflow[0]["predicate"] == "occupation"
    assert len(overflow[0]["samples"]) == 5
    assert "fact_id" not in overflow[0]  # 分组发现没有单独一行可供编辑或删除
    # 这 10 行被折进分组结果，而会话结论不算待修复的垃圾
    outcome_ids = {f["id"] for f in svc.memories("u", kind="outcomes")["facts"]}
    assert not any(r.get("fact_id") in outcome_ids for r in audit["findings"])
    assert not [r for r in audit["findings"] if r.get("predicate") == "occupation"
                and r["kind"] != "slot_overflow"]


def test_editing_an_outcome_keeps_its_shape_and_refreshes_display(tmp_path):
    """通用的重建逻辑会把 "s1 lesson ..." 写进 `text`——把会话 id 嵌进嵌入向量，丢掉 recall 匹配
    所依赖的依据从句——还会让 `display` 永远停留在编辑前的措辞。"""
    response = json.loads('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件","why":"四个 agent 掉线"}]')[0]
    svc = _svc(tmp_path)
    _seed_conclusion(svc, response)
    fact_id = svc.memories("u", kind="outcomes")["facts"][0]["id"]

    svc.update_fact("u", fact_id, object="重新配对会覆盖 ~/.cumora/computer.json")
    row = [f for f in svc.memories("u", kind="outcomes")["facts"] if f["id"] == fact_id][0]
    assert row["text"] == "重新配对会覆盖 ~/.cumora/computer.json （依据：四个 agent 掉线）"
    assert "s1" not in row["text"]
    assert row["why"] == "四个 agent 掉线"
    assert row["display"] == "重新配对会覆盖 ~/.cumora/computer.json"


def test_editing_an_attribute_does_not_collapse_its_display_to_the_bare_object(tmp_path):
    """上面那个测试的姊妹篇，也是它容易引入的回归：`display` 之所以对 outcome 可重新赋值，只是因为
    outcomes.py 写入了 `display=statement`。LLM 抽取器在那里写的是完整句子，所以对 attribute 复用
    `display = object` 会把编辑过的 fact 渲染成 "Moonshot AI" 而不是 "Wei works at Moonshot AI"——
    在 `kind` 的另一侧又碎成片段了。"""
    svc = _svc(tmp_path)
    mem = svc.get("u")
    f = Fact(user_id="u", subject="Wei", predicate="works_at", object="Tencent",
             text="Wei works at Tencent", display="Wei works at Tencent")
    f.embedding = mem.embedder.embed(f.text)
    mem.fact_store.upsert(f.id, f.embedding, f)

    svc.update_fact("u", f.id, object="Moonshot AI")
    row = [r for r in svc.memories("u", kind="attributes")["facts"] if r["id"] == f.id][0]
    assert row["text"] == "Wei works at Moonshot AI"
    assert row["display"] == "Wei works at Moonshot AI"  # 而不是光秃秃的 "Moonshot AI"
    assert row["why"] == ""


def _stored_outcome(mem, predicate="finding", statement="分页游标在第二页丢失排序键", why="实测第二页重复"):
    f = Fact(user_id="u", subject="s-1", predicate=predicate, object=statement,
             text=f"{statement} （依据：{why}）", display=statement, category=OUTCOME_CATEGORY)
    f.embedding = mem.embedder.embed(f.text)
    mem.fact_store.upsert(f.id, f.embedding, f)
    return f


def test_renaming_a_predicate_refreshes_the_stale_display():
    """只改 predicate 的编辑会重建 `text`；若把 `display` 落下，所有展示面在用户被告知保存成功之后，
    仍一直渲染编辑前的措辞。"""
    mem = Memory()
    f = _stored_outcome(mem)
    mem.update_fact(f.id, predicate="lesson")

    got = mem.fact_store.get(f.id)
    assert got.predicate == "lesson"
    assert got.display == got.object       # 已刷新，而不是编辑前的措辞
    assert got.category == OUTCOME_CATEGORY


def test_editing_a_conclusion_keeps_it_in_the_journal():
    """classify() 只认识 attribute 的桶，所以没有显式守卫的话，一次编辑会悄悄把结论降级出 Journal
    和 audit 跳过规则都依赖的那个 category。"""
    mem = Memory()
    f = _stored_outcome(mem, predicate="decision", statement="读路径用混合检索：事实加原始片段")
    mem.update_fact(f.id, object="读路径用混合检索：consolidated facts 加 raw chunks")

    assert mem.fact_store.get(f.id).category == OUTCOME_CATEGORY


def _svc_with_slot(tmp_path, n=6, subject="user", predicate="occupation"):
    svc = MemoryService(data_dir=str(tmp_path))
    mem = svc.get("u")
    for i in range(n):
        junk = Fact(subject=subject, predicate=predicate, object=f"borrow from big tech {i}", user_id="u")
        junk.embedding = mem.embedder.embed(junk.text)
        mem.fact_store.upsert(junk.id, junk.embedding, junk)
    keep = Fact(subject=subject, predicate="lives_in", object="Shenzhen", user_id="u")
    keep.embedding = mem.embedder.embed(keep.text)
    mem.fact_store.upsert(keep.id, keep.embedding, keep)
    return svc, keep


def test_clearing_a_slot_erases_only_that_slot(tmp_path):
    """audit 建议清空溢出的槽位；真正执行时不能碰任何其他东西。"""
    svc, keep = _svc_with_slot(tmp_path, n=6)

    result = svc.clear_slot("u", "user", "occupation", expect_count=6)
    assert result["ok"] and result["deleted"] == 6

    left = svc.memories("u", kind="attributes")["facts"]
    assert [f["id"] for f in left] == [keep.id]
    assert not [r for r in svc.audit("u")["findings"] if r["kind"] == "slot_overflow"]


def test_clearing_a_slot_refuses_when_the_count_moved(tmp_path):
    """owner 批准的是删除他们看到的那 N 行。如果 audit 之后存储又变大了，把新行也删掉就超出了
    被批准的范围。"""
    svc, _ = _svc_with_slot(tmp_path, n=6)

    result = svc.clear_slot("u", "user", "occupation", expect_count=5)
    assert result["ok"] is False and result["deleted"] == 0
    assert result["found"] == 6 and "changed" in result["reason"]
    # 什么都没被碰
    assert len(svc.memories("u", kind="attributes")["facts"]) == 7


def test_clearing_a_slot_is_case_insensitive_on_the_predicate(tmp_path):
    """audit 按存储原样报告 predicate；客户端用不同大小写回传时也必须照样生效。"""
    svc, _ = _svc_with_slot(tmp_path, n=4)
    assert svc.clear_slot("u", "user", "Occupation", expect_count=4)["deleted"] == 4


def test_clearing_a_slot_leaves_superseded_history_alone(tmp_path):
    """audit 统计的是存活（LIVE）fact，所以 `expect_count` 是存活数。如果把被清除集合也算上被
    supersede 的行，守卫就会在比较两个不同的总体：对任何带 supersedes 链的槽位它会永远拒绝；而
    若省略守卫，则会删掉 owner 从没见过的双时间轴历史（AGENTS.md 「对象与时间语义」：对被取代的 fact 要失效化，
    绝不硬删）。"""
    import time

    svc, _ = _svc_with_slot(tmp_path, n=3)
    mem = svc.get("u")
    history = []
    for title in ("intern", "junior dev"):
        old = Fact(subject="user", predicate="occupation", object=title, user_id="u")
        old.embedding = mem.embedder.embed(old.text)
        old.invalid_at = time.time() - 60  # 已被后来的晋升非破坏性地失效化
        mem.fact_store.upsert(old.id, old.embedding, old)
        history.append(old)

    finding = [r for r in svc.audit("u")["findings"] if r["kind"] == "slot_overflow"][0]
    assert finding["count"] == 3  # 只统计存活的垃圾

    result = svc.clear_slot("u", finding["subject"], finding["predicate"],
                            expect_count=finding["count"])
    assert result["ok"] and result["deleted"] == 3
    assert all(mem.fact_store.get(h.id) is not None for h in history)


def test_slot_overflow_does_not_fire_on_genuinely_multi_valued_attributes(tmp_path):
    """三个孩子、三门语言或三个学位是一个真实的人，不是抽取垃圾。对他们断言"单值属性不可能有
    3 个值"是错的，而且该发现还附带一键硬删。"""
    svc = MemoryService(data_dir=str(tmp_path))
    mem = svc.get("u")
    for predicate, values in (("children", ("Mia", "Leo", "Ana")),
                              ("speaks", ("Chinese", "English", "Japanese")),
                              ("graduated_from", ("Tsinghua", "MIT", "Stanford")),
                              ("occupation", ("a", "b", "c"))):
        for value in values:
            f = Fact(subject="user", predicate=predicate, object=value, user_id="u")
            f.embedding = mem.embedder.embed(f.text)
            mem.fact_store.upsert(f.id, f.embedding, f)

    flagged = {r["predicate"] for r in svc.audit("u")["findings"] if r["kind"] == "slot_overflow"}
    assert flagged == {"occupation"}


def test_a_conclusion_is_classified_for_sensitivity_like_every_other_write(tmp_path):
    """结论是模型针对工作会话写的文字，可能包含凭证、诊断或薪资。其他所有写入路径都跑 classify()；
    这条若不跑，fact 生来 sensitive=False，会落入可分享的 `/v1/memories` 和 `/v1/export` 视图——
    而且只有在 owner 编辑它之后才会开始被隐藏，因为 update_fact 会做 classify。"""
    secret = "把生产数据库密码从 .env 换成 vault，密码不要再写进仓库"
    response = json.loads('[{"kind":"lesson","statement":"%s","why":"泄露过一次"}]' % secret)[0]
    svc = _svc(tmp_path)
    _seed_conclusion(svc, response)

    owner = svc.memories("u", kind="outcomes", include_sensitive=True)["facts"]
    assert len(owner) == 1 and owner[0]["sensitive"] is True
    assert owner[0]["category"] == OUTCOME_CATEGORY  # classify 自己的桶不能胜出
    assert svc.memories("u")["facts"] == []  # 可分享视图会隐藏它


def test_an_ordinary_conclusion_is_not_swept_up_by_the_sensitivity_check(tmp_path):
    """上面的守卫不能把每条结论都变成私密——那会清空可分享视图。"""
    response = json.loads('[{"kind":"decision","statement":"部署方式从 docker compose 换成 rsync",'
                  '"why":"compose 在共享机上会重建别人的容器"}]')[0]
    svc = _svc(tmp_path)
    _seed_conclusion(svc, response)

    shared = svc.memories("u")["facts"]
    assert len(shared) == 1 and shared[0]["sensitive"] is False


def test_a_slot_clear_never_touches_a_row_the_owner_already_fixed(tmp_path):
    """Health 页面允许在同一个槽位上先后做两件事：就地修复某一行，然后清空其余。就地修复会把该行
    标为 source="user" 而不 supersede 同级行，于是两条流程交汇——没有豁免的话，第二个动作会抹掉
    第一个刚完成的工作。

    audit() 与 clear_slot() 必须以相同方式应用该豁免：不一致就是计数守卫在一个总体上通过、删除落在
    另一个总体上的根源。
    """
    svc = MemoryService(data_dir=str(tmp_path))
    mem = svc.get("u")
    rows = []
    for i in range(6):
        f = Fact(subject="user", predicate="occupation", object=f"borrowed title {i}", user_id="u")
        f.embedding = mem.embedder.embed(f.text)
        mem.fact_store.upsert(f.id, f.embedding, f)
        rows.append(f)

    # ...owner 在 Health 页面上修复了其中一行。
    svc.update_fact("u", rows[0].id, object="founder of MnemoTrace")
    assert mem.fact_store.get(rows[0].id).source == "user"

    finding = [r for r in svc.audit("u")["findings"] if r["kind"] == "slot_overflow"][0]
    assert finding["count"] == 5  # 被修复的行不再被算作垃圾

    assert svc.clear_slot("u", "user", "occupation", expect_count=5)["deleted"] == 5
    survivor = mem.fact_store.get(rows[0].id)
    assert survivor is not None and survivor.object == "founder of MnemoTrace"
