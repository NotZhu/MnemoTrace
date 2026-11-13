"""Read historical conclusion facts, and audit scoped fact slots."""
from __future__ import annotations
from tests.model_fixtures import saved_outcome


from mnemotrace.consolidate.outcomes import split_outcome_text
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
             text=f"{statement} （依据：{why}）")
    f.embedding = mem.embedder.embed(f.text)
    mem.fact_store.upsert(f.id, f.embedding, f)
    return f


def test_fact_reads_preserve_the_explicit_sentence():
    mem = MemoryEngine()
    f = _stored_outcome(mem)

    got = mem.fact_store.get(f.id)
    assert got.predicate == "finding"
    assert got.text == f.text and not hasattr(got, 'display')
