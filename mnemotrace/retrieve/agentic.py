"""Agentic 迭代检索(AGENTS.md Bet B)—— 针对全领域最薄弱处:multi-session / multi-hop。
不是一次嵌入查找,而是让 LLM 把问题分解成子查询,逐个子查询检索,然后基于已找到的内容再做一轮
后续检索(让第二跳的实体浮出来)。这是一个可开关、可消融的层:如果在 harness 上无法提升
multi-session,就删掉它。"""
from __future__ import annotations

import json
import re

DECOMP_SYSTEM = (
    "You plan retrieval for a question answered from a user's long chat history. Break the question into "
    "1-4 short, standalone search queries whose results together contain the evidence to answer it. For "
    "multi-hop questions, include the intermediate lookups. Output ONLY a JSON array of query strings."
)
DECOMP_TEMPLATE = "Question: {query}\n{hint}\nJSON queries:"


def _parse_query_list(raw: str, fallback: str) -> list[str]:
    if not raw:
        return [fallback]
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            qs = [str(x).strip() for x in data if str(x).strip()]
            if qs:
                return qs
        except json.JSONDecodeError:
            pass
    return [fallback]


class AgenticRetriever:
    def __init__(self, memory, llm, max_subqueries: int = 4, per_query_k: int = 3, rounds: int = 2):
        self.mem = memory
        self.llm = llm
        self.max_subqueries = max_subqueries
        self.per_query_k = per_query_k
        self.rounds = rounds

    def _subqueries(self, query: str, hint: str = "") -> list[str]:
        hint_block = f"What we've found so far (use it to plan follow-up lookups):\n{hint}\n" if hint else ""
        raw = self.llm.complete(DECOMP_TEMPLATE.format(query=query, hint=hint_block), system=DECOMP_SYSTEM)
        return _parse_query_list(raw, query)[: self.max_subqueries]

    def gather_episodes(self, query: str, user_id: str, k: int, as_of: float | None = None):
        """跨分解出的子查询 + 一轮后续检索,返回最多 k 条 episode。"""
        seen: dict = {}

        def pull(qs: list[str]):
            for sq in qs:
                for ep in self.mem.retrieve_episodes(sq, user_id, self.per_query_k, as_of=as_of):
                    seen.setdefault(ep.id, ep)

        pull(self._subqueries(query))
        if self.rounds > 1 and seen:
            hint = "\n".join(ep.text[:200] for ep in list(seen.values())[:5])
            pull(self._subqueries(query, hint=hint))
        return list(seen.values())[:k]
