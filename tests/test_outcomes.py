"""Read and edit historical conclusion facts, and audit scoped fact slots."""
from __future__ import annotations
from tests.model_fixtures import saved_outcome


from mnemotrace.consolidate.outcomes import (
    OUTCOME_CATEGORY,
    split_outcome_text,
)
from tests.model_fixtures import MemoryEngine
from mnemotrace.types import Fact


def test_split_outcome_text_is_the_exact_inverse_of_the_format():
    f = saved_outcome(MemoryEngine(), user_id="u", session_id="s1", kind="finding",
                      statement="client.ts declares its own copy of the API types",
                      why="a contract change failed at runtime, not compile time")
    assert split_outcome_text(f.text) == (
        "client.ts declares its own copy of the API types",
        "a contract change failed at runtime, not compile time",
    )
    # 只记录陈述、没有依据的条目，往返解析后 why 应为空串，而不是多出一个孤立的括号
    assert split_outcome_text("just a statement") == ("just a statement", "")


# --- service 层：console 和 MCP 客户端实际看到的 outcome 层 ---

def _stored_outcome(mem, predicate="finding", statement="分页游标在第二页丢失排序键", why="实测第二页重复"):
    f = Fact(user_id="u", subject="s-1", predicate=predicate, object=statement,
             text=f"{statement} （依据：{why}）", display=statement, category=OUTCOME_CATEGORY)
    f.embedding = mem.embedder.embed(f.text)
    mem.fact_store.upsert(f.id, f.embedding, f)
    return f


def test_renaming_a_predicate_refreshes_the_stale_display():
    """只改 predicate 的编辑会重建 `text`；若把 `display` 落下，所有展示面在用户被告知保存成功之后，
    仍一直渲染编辑前的措辞。"""
    mem = MemoryEngine()
    f = _stored_outcome(mem)
    mem.update_fact(f.id, predicate="lesson")

    got = mem.fact_store.get(f.id)
    assert got.predicate == "lesson"
    assert got.display == got.object       # 已刷新，而不是编辑前的措辞
    assert got.category == OUTCOME_CATEGORY


def test_editing_a_conclusion_keeps_it_in_the_journal():
    """classify() 只认识 attribute 的桶，所以没有显式守卫的话，一次编辑会悄悄把结论降级出 Journal
    和 audit 跳过规则都依赖的那个 category。"""
    mem = MemoryEngine()
    f = _stored_outcome(mem, predicate="decision", statement="读路径用混合检索：事实加原始片段")
    mem.update_fact(f.id, object="读路径用混合检索：consolidated facts 加 raw chunks")

    assert mem.fact_store.get(f.id).category == OUTCOME_CATEGORY
