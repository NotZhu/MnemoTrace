"""根据事实创建图中的实体和关系，并维护关系的有效时间。"""
from __future__ import annotations

from ..embed import Embedder
from ..store import GraphStore
from ..types import Entity, Fact, Relation
from .outcomes import OUTCOME_PREDICATES

# object 是散文而非可命名事物的 predicate。若为其建边，整句话会被 upsert 成一个实体。会话结论
# 是最坏的情况：object 是完整结论、subject 是 session id，每条提炼出的语句都会制造一个垃圾节点，
# 外加每个 session 一个节点，污染 /v1/graph，并在本用于清理存储的审计里凭空制造 orphan_entity 发现。
_TEXTUAL_OBJECT_PREDICATES = {
    "procedure", "how_to", "routine", "instruction", "agent_instruction",
    *OUTCOME_PREDICATES,
}


def projects_fact(fact: Fact) -> bool:
    return fact.predicate.lower() not in _TEXTUAL_OBJECT_PREDICATES


class GraphBuilder:
    def __init__(self, graph: GraphStore, embedder: Embedder) -> None:
        """初始化图构建器。

        Args:
            graph: 图存储。
            embedder: 文本嵌入器。
        """
        self.graph = graph
        self.embedder = embedder

    def add_fact(self, fact: Fact) -> None:
        if not projects_fact(fact):
            return
        subj = self.graph.upsert_entity(Entity(name=fact.subject, user_id=fact.user_id))
        obj = self.graph.upsert_entity(Entity(name=fact.object, user_id=fact.user_id))
        self.graph.add_relation(
            Relation(
                subject_id=subj.id,
                predicate=fact.predicate,
                object_id=obj.id,
                fact_id=fact.id,
                valid_at=fact.valid_at,
                invalid_at=fact.invalid_at,
            )
        )

    def invalidate(self, fact_id: str, t: float) -> None:
        self.graph.invalidate_relations_for_fact(fact_id, t)
