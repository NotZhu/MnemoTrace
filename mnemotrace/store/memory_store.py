"""内存版参考实现。暴力扫描、无索引 —— 正确且零依赖，但不追求速度。
约 1 万条以内表现良好，对 demo、测试和小规模评测切片足够了。"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from ..types import Entity, Relation
from ..util import cosine
from .base import DocStore, GraphStore, Predicate, VectorStore


class InMemoryVectorStore(VectorStore):
    """内存版向量存储。"""
    def __init__(self) -> None:
        """初始化向量存储。"""
        # 以 key 为索引，存储 (vector, payload) 元组
        self._d: dict[str, tuple[list[float] | None, Any]] = {}

    def upsert(self, key: str, vector: list[float] | None, payload: Any) -> None:
        """插入或更新向量存储中的条目。"""
        self._d[key] = (vector, payload)

    def search(
        self, vector: list[float], top_k: int, where: Optional[Predicate] = None
    ) -> list[tuple[float, Any]]:
        scored = [
            (cosine(vector, vec), payload)
            for vec, payload in self._d.values()
            if vec is not None and (where is None or where(payload))
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def get(self, key: str) -> Any | None:
        hit = self._d.get(key)
        return hit[1] if hit else None

    def delete(self, key: str) -> None:
        self._d.pop(key, None)

    def values(self) -> list[Any]:
        return [payload for _, payload in self._d.values()]


class InMemoryDocStore(DocStore):
    """内存版文档存储。"""
    def __init__(self) -> None:
        """初始化文档存储。"""
        self._d: dict[str, Any] = {}

    def put(self, key: str, obj: Any) -> None:
        self._d[key] = obj

    def get(self, key: str) -> Any | None:
        return self._d.get(key)

    def values(self) -> list[Any]:
        return list(self._d.values())

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class InMemoryGraphStore(GraphStore):
    """内存版图存储。"""
    def __init__(self) -> None:
        """初始化图存储。"""
        # 节点主表，key = entity.id，value = Entity 实例
        self.entities: dict[str, Entity] = {}
        # 名字到 id 的反查索引，key = (user_id, name.lower())，value = entity.id
        self._by_name: dict[tuple[str, str], str] = {}
        # 边表，key = relation.id，value = Relation 实例
        self.rels: dict[str, Relation] = {}
        # 出边邻接表，key = subject_id，value = list[relation.id]
        self._out: dict[str, list[str]] = defaultdict(list)
        # 入边邻接表，key = object_id，value = list[relation.id]
        self._in: dict[str, list[str]] = defaultdict(list)

    def upsert_entity(self, entity: Entity) -> Entity:
        key = (entity.user_id, entity.name.lower())
        existing_id = self._by_name.get(key)
        if existing_id is not None:
            return self.entities[existing_id]
        self.entities[entity.id] = entity
        self._by_name[key] = entity.id
        return entity

    def get_entity(self, user_id: str, name: str) -> Entity | None:
        eid = self._by_name.get((user_id, name.lower()))
        return self.entities.get(eid) if eid else None

    def add_relation(self, relation: Relation) -> None:
        self.rels[relation.id] = relation
        self._out[relation.subject_id].append(relation.id)
        self._in[relation.object_id].append(relation.id)

    def neighbors(
        self, entity_id: str, as_of: Optional[float] = None, direction: str = "out"
    ) -> list[Relation]:
        rel_ids = self._out[entity_id] if direction == "out" else self._in[entity_id]
        out: list[Relation] = []
        for rid in rel_ids:
            r = self.rels[rid]
            live = as_of is None or (
                (r.valid_at is not None and r.valid_at <= as_of) and (r.invalid_at is None or r.invalid_at > as_of)
            )
            if live:
                out.append(r)
        return out

    def invalidate_relations_for_fact(self, fact_id: str, t: float) -> None:
        for r in self.rels.values():
            if r.fact_id == fact_id and r.invalid_at is None:
                r.invalid_at = t

    def delete_relations_for_fact(self, fact_id: str) -> None:
        for rid, rel in list(self.rels.items()):
            if rel.fact_id != fact_id:
                continue
            self.rels.pop(rid, None)
            if rid in self._out.get(rel.subject_id, []):
                self._out[rel.subject_id].remove(rid)
            if rid in self._in.get(rel.object_id, []):
                self._in[rel.object_id].remove(rid)

    def prune_orphan_entities(self) -> int:
        referenced = {r.subject_id for r in self.rels.values()} | {r.object_id for r in self.rels.values()}
        removed = 0
        for eid, ent in list(self.entities.items()):
            if eid in referenced:
                continue
            self.entities.pop(eid, None)
            self._by_name.pop((ent.user_id, ent.name.lower()), None)
            self._out.pop(eid, None)
            self._in.pop(eid, None)
            removed += 1
        return removed

    def relations(self) -> list[Relation]:
        return list(self.rels.values())
