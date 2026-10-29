"""针对模糊长尾的 LLM 冲突检测（conflict.py 里的廉价结构规则处理高频、高置信的情况；这里捕捉
它们无法枚举的部分）。在 System-2（异步整合，不在写路径上）运行，只处理候选对（先用廉价的嵌入
预筛，而不是 O(n²) 次 LLM 调用），且绝不自动解决——疑似冲突变成待确认的 `Conflict`，由用户裁定。
默认是 COEXIST，因此绝不会悄悄污染记忆。需要显式开启（Config.conflict_detection）；
离线 / 测试保持纯规则、完全确定。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from ..embed import Embedder
from ..llm import LLM
from ..types import Conflict, Fact
from ..util import cosine
from .verdict import parse_verdict

_JUDGE_SYSTEM = (
    "你判断关于同一个人的两条记忆是否冲突——即其中一条是否更新/否定/取代了另一条(同一属性的新值、"
    "偏好反转、地址/职业/称呼变更等),还是两者可以同时成立(不同的事、互补的偏好)。"
    "只回复一个词:CONFLICT 或 COEXIST。拿不准就回 COEXIST。"
)


def _candidate_pairs(facts: list[Fact], seen: set[tuple], sim_lo: float, sim_hi: float):
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


def detect_conflicts(facts: list[Fact], llm: LLM, user_id: str, seen: set[tuple],
                     embedder: Optional[Embedder] = None, max_calls: int = 16,
                     sim_lo: float = 0.62, sim_hi: float = 0.94) -> list[Conflict]:
    """返回 `facts`（用户存活集）中新的待确认冲突。每个候选对一次 LLM 调用，上限 `max_calls`；
    默认 COEXIST，因此只标记真正疑似冲突的情况。"""
    candidates = _candidate_pairs(facts, seen, sim_lo, sim_hi)
    conflicts: list[Conflict] = []
    for a, b in candidates[:max_calls]:
        try:
            verdict = llm.complete(f"A: {a.text}\nB: {b.text}\n\n它们冲突吗?", system=_JUDGE_SYSTEM)
        except Exception:  # noqa: BLE001 -- 检测绝不能中断整合
            parse_verdict(None, {"CONFLICT", "COEXIST"})
            continue
        if parse_verdict(verdict, {"CONFLICT", "COEXIST"}) != "CONFLICT":
            continue  # COEXIST（或无法解析）-> 不标记，两者都保留
        older, newer = (a, b) if a.valid_at <= b.valid_at else (b, a)
        conflicts.append(Conflict(
            older=older.id, newer=newer.id, text_older=older.text, text_newer=newer.text,
            user_id=user_id, reason=verdict.strip()[:80],
        ))
    return conflicts
