"""内置的小型合成评测集，覆盖真实 memory benchmark 考察的类别：
单跳（single-hop）、知识更新（knowledge-update）、多跳（multi-hop）、时序（as-of）、拒答（假前提）。

每个 item 自包含（有自己的多会话历史），互不干扰。`answer=None` 表示正确行为是拒答（ABSTAIN）。
`as_of_day` 让查询在过去某个时间点执行。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EvalItem:
    id: str
    category: str
    sessions: list[tuple[str, int]]  # (text, day_offset)
    question: str
    answer: Optional[str]  # None => 期望拒答
    as_of_day: Optional[int] = None
    path: tuple[str, ...] = ()  # Explicit caller input: anchor followed by outbound predicates.
    entity_types: dict[str, str] = field(default_factory=dict)


ITEMS: list[EvalItem] = [
    EvalItem(
        "sh1", "single_hop",
        [("My name is Ana and I work at Spotify.", 0), ("I really enjoy hiking on weekends.", 1)],
        "Where do I work?", "Spotify",
    ),
    EvalItem(
        "sh2", "single_hop",
        [("My name is Cara and my favorite color is blue.", 0)],
        "What is my favorite color?", "blue",
    ),
    EvalItem(
        "ku1", "knowledge_update",
        [("My name is Ben and I live in Boston.", 0), ("I moved — I now live in Denver.", 60)],
        "Where do I live?", "Denver",
    ),
    EvalItem(
        "ku2", "knowledge_update",
        [("My name is Cara and my favorite color is blue.", 0), ("My favorite color is green now.", 40)],
        "What is my favorite color?", "green",
    ),
    EvalItem(
        "mh1", "multi_hop",
        [("My name is Dora.", 0), ("My colleague Evan works at Netflix.", 1)],
        "Where does my colleague work?", "Netflix", path=("我", "colleague", "works_at"),
        entity_types={"我": "person", "Evan": "person", "Netflix": "organization"},
    ),
    EvalItem(
        "mh2", "multi_hop",
        [
            ("My sister Maya is a pediatrician at a children's hospital.", 0),
            ("Maya just moved to Seattle for a fellowship.", 1),
            ("I bought a new espresso machine and it's wonderful.", 2),
        ],
        "Where does my sister live?", "Seattle", path=("我", "sister", "lives_in"),
        entity_types={"我": "person", "Maya": "person", "Seattle": "location"},
    ),
    EvalItem(
        "tmp1", "temporal",
        [("My name is Finn and I work at IBM.", 0), ("I now work at Oracle.", 50)],
        "Where do I work?", "IBM", as_of_day=20,
    ),
    EvalItem(
        "tmp2", "temporal",
        [("My name is Finn and I work at IBM.", 0), ("I now work at Oracle.", 50)],
        "Where do I work?", "Oracle",  # 当前状态（无 as_of）
    ),
    EvalItem(
        "abs1", "abstention",
        [("My name is Gina and I work at Adobe.", 0)],
        "What is my favorite food?", None,  # 属性从未提及 -> 拒答
    ),
    EvalItem(
        "abs2", "abstention",
        [("My name is Hugo and I live in Lisbon.", 0)],
        "What is the capital of France?", None,  # 与记忆无关 -> 拒答
    ),
]
