"""Session outcomes——与 owner 实际工作方式匹配的记忆单元。

逐 episode 的抽取器找的是个人履历类三元组，这对 LongMemEval 合适，对技术工作会话却不行：在一份
真实的 115 轮 transcript 上它产出了 `user requires_no_commit_push_publish = true`（把一次性的任务
约束提升成了持久属性），而十个会话共 3353 轮只换来五条 fact。改成让它输出决策和经验教训后，同一个
模型返回的内容与 owner 手写进 markdown 的东西相当。"""
from __future__ import annotations

import json
import pytest
from mnemotrace.extraction import ExtractionError, ExtractionResponseError, LLMNotConfiguredError

from mnemotrace.consolidate.outcomes import (
    OUTCOME_CATEGORY,
    OUTCOME_PREDICATES,
    OUTCOME_SYSTEM,
    _windowed,
    extract_outcomes,
    split_outcome_text,
)
from tests.model_fixtures import Memory
from tests.model_fixtures import MemoryService
from mnemotrace.types import Episode, Fact


class StubLLM:
    """记录 prompt，供测试断言 transcript 是以何种形式呈现的。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompt = ""
        self.system = ""

    def complete(self, prompt: str, system: str = "", **kwargs) -> str:
        self.prompt, self.system = prompt, system
        return self.reply


def _episodes(n: int = 3) -> list[Episode]:
    return [Episode(content=f"turn {i} with enough words to survive filtering", user_id="u",
                    session_id="s1", speaker="user" if i % 2 else "assistant",
                    event_time=1_700_000_000.0 + i)
            for i in range(n)]


def test_outcomes_become_ordinary_facts_with_kind_predicates():
    """不新增类型也不新增存储：outcomes 搭 Fact 表的便车，从而免费继承双时间轴有效期、取代链
    （supersession）与 provenance。"""
    llm = StubLLM('[{"kind":"lesson","statement":"Running --pair overwrites the single global config",'
                  '"why":"took four agents offline"}]')
    facts = extract_outcomes(llm, _episodes(), "u", session_id="s1")
    assert len(facts) == 1
    f = facts[0]
    assert f.predicate == "lesson" and f.predicate in OUTCOME_PREDICATES
    assert f.object.startswith("Running --pair")
    assert "took four agents offline" in f.text  # 理由内嵌在文本里，所以原因可被检索
    assert f.display == f.object
    # 归属到整个会话：一个结论靠的是全过程，而不是某一轮
    assert len(f.provenance) == 3
    assert f.valid_at == _episodes()[-1].event_time


def test_unknown_kinds_and_degenerate_statements_are_rejected():
    llm = StubLLM('['
                  '{"kind":"gossip","statement":"something long enough to pass length checks"},'
                  '{"kind":"lesson","statement":"too short"},'
                  '{"kind":"finding","statement":"' + "x" * 500 + '"},'
                  '{"kind":"finding","statement":"A real finding with adequate length here"}]')
    with pytest.raises(ExtractionResponseError):
        extract_outcomes(llm, _episodes(), "u")


def test_duplicate_statements_collapse():
    llm = StubLLM('[{"kind":"decision","statement":"We chose rsync over docker compose"},'
                  '{"kind":"decision","statement":"We chose rsync over docker compose"}]')
    assert len(extract_outcomes(llm, _episodes(), "u")) == 1


@pytest.mark.parametrize('kind', ['FINDING', ' finding '])
def test_kind_is_normalized_before_validation(kind):
    llm = StubLLM(json.dumps([{'kind': kind, 'statement': 'A synthetic finding with adequate length.'}]))
    item, = extract_outcomes(llm, _episodes(), 'u')
    assert item.predicate == 'finding'


@pytest.mark.parametrize('statement', ['too short', 'x' * 500])
def test_valid_kind_with_degenerate_length_is_filtered(statement):
    llm = StubLLM(json.dumps([{'kind': 'finding', 'statement': statement}]))
    assert extract_outcomes(llm, _episodes(), 'u') == []


def test_transcript_is_fenced_as_data_not_conversation():
    """以 "[assistant] ..." 开头的请求体会被当成"要续写的对话"：在一份真实的 1164 轮会话上，模型
    写起了下一轮而不是做抽取，结果一无所获。"""
    llm = StubLLM("[]")
    extract_outcomes(llm, _episodes(), "u")
    assert "<session>" in llm.prompt and "</session>" in llm.prompt
    assert not llm.prompt.lstrip().startswith("[")


def test_long_sessions_keep_both_ends():
    """普通截断只让模型看到 335k 字符会话的前 7%——那还是铺垫，什么都没决定。结论都落在末尾。"""
    turns = [f"turn {i} " + "x" * 200 for i in range(300)]
    out = _windowed(turns, 4000)
    assert len(out) <= 4100
    assert "turn 0 " in out and "turn 299 " in out and "省略" in out


def test_a_session_with_nothing_durable_yields_nothing():
    assert extract_outcomes(StubLLM("[]"), _episodes(), "u") == []


def test_model_outage_never_loses_the_session():
    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("provider down")

    with pytest.raises(ExtractionError):
        extract_outcomes(Broken(), _episodes(), "u")


def test_truncated_model_output_is_rejected():
    """被 max_tokens 截断的回复过去会丢掉所有已抽取的条目——而这恰恰是长会话的常态，因为模型会
    用满预算。"""
    from mnemotrace.consolidate.llm_extractor import parse_json_facts

    truncated = ('[{"kind":"finding","statement":"first complete one","why":"a"},'
                 '{"kind":"lesson","statement":"second complete one","why":"b"},'
                 '{"kind":"decis')
    with pytest.raises(ExtractionResponseError):
        parse_json_facts(truncated)


def test_split_outcome_text_is_the_exact_inverse_of_the_format():
    """格式化器与这个解析器刻意相隔十行：分隔符一变，这里必须先挂。"""
    llm = StubLLM('[{"kind":"finding","statement":"client.ts declares its own copy of the API types",'
                  '"why":"a contract change failed at runtime, not compile time"}]')
    f = extract_outcomes(llm, _episodes(), "u", session_id="s1")[0]
    assert split_outcome_text(f.text) == (
        "client.ts declares its own copy of the API types",
        "a contract change failed at runtime, not compile time",
    )
    # 只记录陈述、没有依据的条目，往返解析后 why 应为空串，而不是多出一个孤立的括号
    assert split_outcome_text("just a statement") == ("just a statement", "")


# --- service 层：console 和 MCP 客户端实际看到的 outcome 层 ---

def _svc(tmp_path, llm=None):
    """离线 service（llm_name="" => self.llm 为 None），之后可选地注入一个 stub LLM。"""
    svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    if llm is not None:
        svc.llm = llm
    return svc


def _session(svc, user="u", session_id="s1"):
    svc.remember(user, "我们把部署方式从 docker compose 换成了 rsync", session_id=session_id, scope="working")
    svc.remember(user, "重新配对会覆盖唯一的全局配置文件", session_id=session_id, scope="working")


def test_close_session_requires_llm_when_outcomes_requested(tmp_path):
    from mnemotrace.service import MemoryService as NativeService
    svc = NativeService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    _session(svc)
    with pytest.raises(LLMNotConfiguredError):
        svc.close_session("u", "s1")
    assert len(svc.get("u").working_memory("u", "s1")) == 2


def test_env_kill_switch_turns_outcomes_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SESSION_OUTCOMES", "0")
    assert _svc(tmp_path).config.session_outcomes is False


def test_env_kill_switch_outranks_a_client_opt_in(tmp_path, monkeypatch):
    """这个开关是运营方的成本上限，所以不能只是个默认值：/v1/sessions/close 接受 `outcomes` 字段，
    而 connectors/watch.py 对它 import 的每个会话都会 post `outcomes: True`——那是唯一无人值守
    运行的调用方。只靠默认值的开关会让这条路径照样烧钱。"""
    monkeypatch.setenv("MNEMOTRACE_SESSION_OUTCOMES", "0")
    llm = StubLLM('[{"kind":"decision","statement":"部署方式从 docker compose 换成 rsync",'
                  '"why":"compose 在共享机上会重建别人的容器"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    assert svc.close_session("u", "s1", outcomes=True)["outcomes"] == 0
    # 不只是"没有写入 fact"：蒸馏调用本身就不该发生。outcome 调用是 close 流程里最后一次 LLM 调用，
    # 所以一个从未见过 OUTCOME_SYSTEM 的 stub 就是从未被要求蒸馏。
    assert llm.system != OUTCOME_SYSTEM
    assert svc.memories("u", kind="outcomes")["facts"] == []


def test_reclosing_an_unchanged_session_adds_no_duplicates(tmp_path):
    """connectors/watch.py 会重发变长了的 transcript 并再次 close；extract_outcomes 只在单次调用内
    去重，所以没有守卫的话每次 re-close 都会让结论翻倍。"""
    llm = StubLLM('[{"kind":"decision","statement":"部署方式从 docker compose 换成 rsync",'
                  '"why":"compose 在共享机上会重建别人的容器"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    assert svc.close_session("u", "s1")["outcomes"] == 1
    assert svc.close_session("u", "s1")["outcomes"] == 0
    outcomes = svc.memories("u", kind="outcomes")["facts"]
    assert len(outcomes) == 1


def test_kind_partitions_the_fact_set_exactly(tmp_path):
    llm = StubLLM('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件",'
                  '"why":"四个 agent 掉线"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.add_fact("u", "user", "occupation", "engineer")
    svc.close_session("u", "s1")

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
    llm = StubLLM('[{"kind":"finding","statement":"共享机上不要执行 docker build","why":"会重建别人的容器"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.add_fact("u", "user", "occupation", "engineer")
    svc.close_session("u", "s1")
    row = svc.memories("u", kind="outcomes")["facts"][0]
    assert row["why"] == "会重建别人的容器"
    assert svc.memories("u", kind="attributes")["facts"][0]["why"] == ""


def test_outcomes_never_reach_the_graph(tmp_path):
    """结论的 object 是一个句子、subject 是会话 id，建边的话每条陈述会铸造一个垃圾实体、每个会话
    再铸一个——然后 audit 还会把它们报成孤儿节点。"""
    llm = StubLLM('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件","why":"四个 agent 掉线"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.close_session("u", "s1")
    names = {n["name"] for n in svc.graph("u", include_sensitive=True)["nodes"]}
    assert "s1" not in names
    assert not any("重新配对会覆盖" in n for n in names)


def test_audit_reports_one_slot_overflow_and_leaves_outcomes_alone(tmp_path):
    """owner 的真实存储里躺着 84 条存活的 `occupation` fact。每一行都能通过逐条规则；缺陷只在
    槽位层面可见。报告一次，而不是 84 次。"""
    llm = StubLLM('[{"kind":"lesson","statement":"配置文件路径 ~/.cumora/computer.json 没有覆盖机制",'
                  '"why":"跑 --pair 覆盖了线上那份"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.close_session("u", "s1")
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
    llm = StubLLM('[{"kind":"lesson","statement":"重新配对会覆盖唯一的全局配置文件","why":"四个 agent 掉线"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.close_session("u", "s1")
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
    llm = StubLLM('[{"kind":"lesson","statement":"%s","why":"泄露过一次"}]' % secret)
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.close_session("u", "s1")

    owner = svc.memories("u", kind="outcomes", include_sensitive=True)["facts"]
    assert len(owner) == 1 and owner[0]["sensitive"] is True
    assert owner[0]["category"] == OUTCOME_CATEGORY  # classify 自己的桶不能胜出
    assert svc.memories("u")["facts"] == []  # 可分享视图会隐藏它


def test_an_ordinary_conclusion_is_not_swept_up_by_the_sensitivity_check(tmp_path):
    """上面的守卫不能把每条结论都变成私密——那会清空可分享视图。"""
    llm = StubLLM('[{"kind":"decision","statement":"部署方式从 docker compose 换成 rsync",'
                  '"why":"compose 在共享机上会重建别人的容器"}]')
    svc = _svc(tmp_path, llm)
    _session(svc)
    svc.close_session("u", "s1")

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
