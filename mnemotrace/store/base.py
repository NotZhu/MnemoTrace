"""存储接口。参考实现是内存版（零配置）；生产环境在同一套 API 后面换用 LanceDB/Qdrant/pgvector（向量）、
Kuzu/Neo4j（图）以及真正的文档存储。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from ..types import Entity, Relation

Predicate = Callable[[Any], bool]


class VectorStore(ABC):
    """向量存储接口。支持 key 映射到 (vector, payload)，并支持带可选过滤条件的最近邻检索。"""

    @abstractmethod
    def upsert(self, key: str, vector: list[float], payload: Any) -> None: ...

    @abstractmethod
    def search(
        self, vector: list[float], top_k: int, where: Optional[Predicate] = None
    ) -> list[tuple[float, Any]]: ...

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def values(self) -> list[Any]: ...


class DocStore(ABC):
    """纯 key->object 存储（用于只追加的 episode 日志）。"""

    @abstractmethod
    def put(self, key: str, obj: Any) -> None: ...

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def values(self) -> list[Any]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...


class GraphStore(ABC):
    """图存储接口：管理实体节点和关系边，支持按有效时间查询关系。"""

    @abstractmethod
    def upsert_entity(self, entity: Entity) -> Entity:
        """按 (user_id, name) 解析；已存在则返回现有节点，否则插入。"""

    @abstractmethod
    def get_entity(self, user_id: str, name: str) -> Entity | None: ...

    @abstractmethod
    def add_relation(self, relation: Relation) -> None: ...

    @abstractmethod
    def neighbors(
        self, entity_id: str, as_of: Optional[float] = None, direction: str = "out"
    ) -> list[Relation]:
        """与 `entity_id` 相连的存活 relation（过滤出在 `as_of` 时刻仍有效的那些）。"""

    @abstractmethod
    def invalidate_relations_for_fact(self, fact_id: str, t: float) -> None: ...

    @abstractmethod
    def delete_relations_for_fact(self, fact_id: str) -> None:
        """硬删除某个 fact 投影出的所有图边。仅用于用户编辑/擦除场景。"""

    @abstractmethod
    def prune_orphan_entities(self) -> int:
        """删除不再被任何 relation 引用的 entity。返回删除数量。"""

    @abstractmethod
    def relations(self) -> list[Relation]: ...
