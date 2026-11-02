"""Pure candidate selection; model judgements are pipeline operations."""
from collections import defaultdict
from ..types import Fact
from ..util import cosine

def candidate_pairs(facts: list[Fact], seen: set[tuple], sim_lo: float, sim_hi: float):
    """廉价预筛：同 subject 且嵌入相近但不完全相同（可能是换说法/更新过的属性）的对，或
    同 object 但 predicate 不同（可能是规则漏掉的反转）的对。已判定过的对会被跳过，
    所以重复运行只会浮现新的候选。"""
    by_subj: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_subj[f.subject.lower()].append(f)
    out: list[tuple[Fact, Fact]] = []
    for fs in by_subj.values():
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                a, b = fs[i], fs[j]
                if a.assertion_kind != b.assertion_kind:
                    continue
                if tuple(sorted((a.id, b.id))) in seen:
                    continue
                same_obj = a.object.strip().lower() == b.object.strip().lower()
                if same_obj and a.predicate == b.predicate:
                    continue  # 完全相同的断言——是重复，不是冲突
                same_obj_diff_pred = same_obj and a.predicate != b.predicate
                near = (a.embedding and b.embedding and sim_lo <= cosine(a.embedding, b.embedding) <= sim_hi)
                if same_obj_diff_pred or near:
                    out.append((a, b))
    return out
