"""可选 Qdrant 向量存储适配器；导入本模块不会加载 qdrant-client。"""
from __future__ import annotations

import math
import uuid
from typing import Any, Optional

from ..types import Episode, Fact
from ..util import cosine
from .base import Predicate, VectorStore
from .persist import DimensionMismatchError, PersistenceError, StoreFormatError, _from_record, _record

_TYPES = {"Episode": Episode, "Fact": Fact}
_POINT_NAMESPACE = uuid.UUID("a42da97c-510c-46ed-8fef-37905f1eb5cb")
_PAGE_SIZE = 256


def _point_id(key: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, key))


def _encode(key: str, vector: list[float], payload: Any) -> dict:
    typ = type(payload).__name__ if type(payload) in _TYPES.values() else "json"
    data = _record(payload) if typ != "json" else payload
    # Validate typed records at the same seam as reads, before any database mutation.
    if typ != "json":
        _from_record(_TYPES[typ], data, "Qdrant payload.data")
    import json
    encoded = {"key": key, "type": typ, "data": data, "vector": vector}
    json.dumps(encoded, allow_nan=False)
    return encoded


def _decode(point, dimension: int) -> tuple[str, list[float], Any]:
    raw = point.payload
    if (not isinstance(raw, dict) or not isinstance(raw.get("key"), str)
            or raw.get("type") not in (*_TYPES, "json") or "data" not in raw
            or not isinstance(raw.get("vector"), list)
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in raw["vector"])):
        raise StoreFormatError("Qdrant payload: invalid key, type, data or vector")
    if raw["vector"] and len(raw["vector"]) != dimension:
        raise StoreFormatError("Qdrant payload.vector: dimension does not match collection")
    if str(point.id) != _point_id(raw["key"]):
        raise StoreFormatError("Qdrant payload: key does not match point ID")
    typ = raw["type"]
    data = (_from_record(_TYPES[typ], raw["data"], "Qdrant payload.data")
            if typ in _TYPES else raw["data"])
    return raw["key"], raw["vector"], data


class QdrantVectorStore(VectorStore):
    """单个集合的适配器；集合和注入客户端归调用方所有，此处不关闭客户端。

    Python 谓词需要精确的全量扫描。载荷保留原始向量，因为余弦集合会归一化
    已存向量，而空向量也须保持对 VectorStore 接口可见。
    """

    def __init__(self, client: Any, collection: str, dimension: int) -> None:
        """初始化 Qdrant 向量存储适配器。

        Args:
            client: Qdrant 客户端实例。
            collection: 向量集合名称。
            dimension: 向量维度。
        """
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise ValueError("Qdrant dimension must be a positive integer")
        self.client = client
        self.collection = collection
        self.dimension = dimension

    def _call(self, method: str, *args, **kwargs):
        try:
            return getattr(self.client, method)(*args, **kwargs)
        except Exception as exc:
            # Service degradation handlers must distinguish failed storage from
            # optional model failures, without exposing endpoint or credentials.
            raise PersistenceError("Qdrant storage operation failed") from exc

    def _exists(self) -> bool:
        from qdrant_client import models

        if not self._call("collection_exists", self.collection):
            return False
        params = self._call("get_collection", self.collection).config.params.vectors
        if not isinstance(params, models.VectorParams) or params.distance != models.Distance.COSINE:
            raise StoreFormatError("Qdrant collection must use one unnamed cosine vector")
        if params.size != self.dimension:
            raise DimensionMismatchError(
                f"Qdrant collection dimension={params.size}, expected={self.dimension}; "
                "use explicit reembed() to change dimensions"
            )
        return True

    def _ensure(self) -> None:
        from qdrant_client import models

        if not self._exists():
            self._call("create_collection",
                self.collection,
                vectors_config=models.VectorParams(size=self.dimension, distance=models.Distance.COSINE),
            )

    def _validate_vector(self, vector: list[float]) -> None:
        if (not isinstance(vector, list) or any(isinstance(v, bool)
                or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)):
            raise ValueError("Qdrant vector must contain finite numbers")
        if vector and len(vector) != self.dimension:
            raise DimensionMismatchError(
                f"Qdrant vector dimension={len(vector)}, expected={self.dimension}"
            )

    def upsert(self, key: str, vector: list[float], payload: Any) -> None:
        from qdrant_client import models

        self._validate_vector(vector)
        encoded = _encode(key, vector, payload)
        self._ensure()
        self._call("upsert", self.collection, points=[models.PointStruct(
            id=_point_id(key), vector=vector or {}, payload=encoded,
        )], wait=True)

    def _rows(self, *, empty_only: bool = False):
        from qdrant_client import models

        offset = None
        condition = (models.Filter(must=[models.IsEmptyCondition(
            is_empty=models.PayloadField(key="vector"),
        )]) if empty_only else None)
        while True:
            points, offset = self._call("scroll",
                self.collection, scroll_filter=condition, offset=offset,
                limit=_PAGE_SIZE, with_payload=True, with_vectors=False,
            )
            for point in points:
                yield _decode(point, self.dimension)
            if offset is None:
                return

    def search(
        self, vector: list[float], top_k: int, where: Optional[Predicate] = None,
    ) -> list[tuple[float, Any]]:
        from qdrant_client import models

        self._validate_vector(vector)
        if top_k <= 0 or not self._exists():
            return []
        if where is not None or not any(vector):
            rows = self._rows()
        else:
            hits = self._call("query_points",
                self.collection, query=vector, limit=top_k,
                search_params=models.SearchParams(exact=True),
                with_payload=True, with_vectors=False,
            ).points
            # Empty vectors have score zero: they must beat negative similarities,
            # and also fill a result containing fewer than top_k searchable points.
            rows = [*(_decode(point, self.dimension) for point in hits), *self._rows(empty_only=True)]
        scored = [(cosine(vector, vec), payload) for _, vec, payload in rows
                  if where is None or where(payload)]
        scored.sort(key=lambda hit: hit[0], reverse=True)
        return scored[:top_k]

    def get(self, key: str) -> Any | None:
        if not self._exists():
            return None
        points = self._call("retrieve", self.collection, ids=[_point_id(key)], with_payload=True)
        return _decode(points[0], self.dimension)[2] if points else None

    def delete(self, key: str) -> None:
        from qdrant_client import models

        if self._exists():
            self._call("delete", self.collection,
                               points_selector=models.PointIdsList(points=[_point_id(key)]), wait=True)

    def values(self) -> list[Any]:
        return [payload for _, _, payload in self._rows()] if self._exists() else []

    def drop(self) -> None:
        """Erase only this exact collection, including an incompatible schema."""
        if self._call("collection_exists", self.collection):
            from qdrant_client.local.qdrant_local import QdrantLocal

            local = getattr(self.client, "_client", None)
            if isinstance(local, QdrantLocal):
                # qdrant-client 1.19/1.20 delete_collection drops the local object
                # without closing SQLite first. Windows otherwise retains the file
                # and a recreated collection may reopen stale rows.
                local.collections[self.collection].close()
            self._call("delete_collection", self.collection)

    def reset_dimension(self, dimension: int) -> None:
        """Only the explicit re-embedding path may recreate a collection."""
        self.drop()
        self.dimension = dimension
