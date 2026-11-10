"""根据事实创建图中的实体和关系，并维护关系的有效时间。"""
from __future__ import annotations

import re

from ..embed import Embedder
from ..store import GraphStore
from ..types import Entity, Fact, Relation

# Graph identifiers have the same capacity in both stores as the Postgres identifier domain.
_MAX_GRAPH_FIELD_LENGTH = 512
_NON_SINGLE_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def projects_fact(fact: Fact) -> bool:
    if fact.review_pending or fact.object_kind != "entity":
        return False
    fields = (fact.subject, fact.predicate, fact.object, fact.subject.lower(), fact.object.lower())
    return all(
        value.strip() and len(value) <= _MAX_GRAPH_FIELD_LENGTH and not _NON_SINGLE_LINE.search(value)
        for value in fields
    )


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
        self.graph.delete_relations_for_fact(fact.id)
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
