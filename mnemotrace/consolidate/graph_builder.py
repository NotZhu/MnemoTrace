"""根据事实创建图中的实体和关系，并维护关系的有效时间。"""
from __future__ import annotations

import re

from ..store import GraphStore
from ..types import Entity, Fact, Relation
from ..util import name_key

# Graph identifiers have the same capacity in both stores as the Postgres identifier domain.
_MAX_GRAPH_FIELD_LENGTH = 512
_NON_SINGLE_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def projects_fact(fact: Fact, definition=None) -> bool:
    """判断事实能否投影为图中的关系。

    Args:
        fact: 待检查的事实。
        definition: 可选的关系类型及端点约束。

    Returns:
        两端实体及字段满足图投影约束时返回 True。
    """
    if not (fact.subject_id and fact.object_id):
        return False
    if definition and definition.endpoint_types:
        endpoints = [fact.subject_type, fact.object_type]
        if endpoints not in definition.endpoint_types and (
            definition.direction != 'symmetric' or endpoints[::-1] not in definition.endpoint_types
        ):
            return False
    fields = (fact.subject, fact.predicate, fact.object, name_key(fact.subject), name_key(fact.object))
    return all(
        value.strip() and len(value) <= _MAX_GRAPH_FIELD_LENGTH and not _NON_SINGLE_LINE.search(value)
        for value in fields
    )


class GraphBuilder:
    def __init__(self, graph: GraphStore) -> None:
        """绑定要维护的图存储。

        Args:
            graph: 接收事实投影的图存储。
        """
        self.graph = graph

    def add_fact(self, fact: Fact) -> None:
        """按事实当前内容替换其关系边。

        Args:
            fact: 提供关系端点及有效时间的事实。
        """
        self.graph.delete_relations_for_fact(fact.id)
        if not projects_fact(fact):
            return
        subj = self.graph.upsert_entity(Entity(id=fact.subject_id, name=fact.subject, user_id=fact.user_id, type=fact.subject_type))
        obj = self.graph.upsert_entity(Entity(id=fact.object_id, name=fact.object, user_id=fact.user_id, type=fact.object_type))
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
