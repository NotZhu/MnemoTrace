"""可选的 LanceDB 向量存储后端。

本模块刻意不被默认路径导入。LanceDB 是规模化的后端，不是核心依赖；零配置用户不应为它支付任何
导入或安装成本。
"""
from __future__ import annotations

import json
import os
from dataclasses import is_dataclass
from typing import Any, Optional

from ..types import Episode, Fact
from ..util import cosine
from .base import Predicate, VectorStore
from .persist import StoreFormatError, _from_record, _record

_TYPES = {"Episode": Episode, "Fact": Fact}


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _encode_payload(payload: Any) -> str:
    if is_dataclass(payload):
        return json.dumps(
            {"type": payload.__class__.__name__, "data": _record(payload)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return json.dumps({"type": "json", "data": payload}, ensure_ascii=False, sort_keys=True)


def _decode_payload(raw: str) -> Any:
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise StoreFormatError("LanceDB payload: malformed JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str) or "data" not in obj:
        raise StoreFormatError("LanceDB payload: missing valid type or data")
    typ = obj.get("type")
    if typ in _TYPES:
        return _from_record(_TYPES[typ], obj["data"], "LanceDB payload.data")
    return obj.get("data")


class LanceDBVectorStore(VectorStore):
    """持久化向量存储：每个逻辑 忆痕 索引对应一张 LanceDB 表。"""

    def __init__(self, path: str, table: str = "vectors") -> None:
        import lancedb  # noqa: PLC0415 - 可选依赖，刻意延迟导入。

        self.path = os.path.expanduser(path)
        self.table_name = table
        os.makedirs(self.path, exist_ok=True)
        self._db = lancedb.connect(self.path)
        self._table = None

    def _open(self):
        if self._table is not None:
            return self._table
        tables = self._db.list_tables()
        names = getattr(tables, "tables", tables)
        if self.table_name in names:
            self._table = self._db.open_table(self.table_name)
        return self._table

    def _ensure(self, vector: list[float], key: str, payload: Any):
        table = self._open()
        if table is None:
            self._table = self._db.create_table(
                self.table_name,
                data=[{"key": key, "vector": vector, "payload": _encode_payload(payload)}],
                mode="overwrite",
            )
            return None
        return table

    def upsert(self, key: str, vector: list[float], payload: Any) -> None:
        table = self._ensure(vector, key, payload)
        if table is None:
            return
        table.delete(f"key = {_quote(key)}")
        table.add([{"key": key, "vector": vector, "payload": _encode_payload(payload)}])

    def search(
        self, vector: list[float], top_k: int, where: Optional[Predicate] = None
    ) -> list[tuple[float, Any]]:
        table = self._open()
        if table is None or top_k <= 0:
            return []
        # Python 谓词是 VectorStore 契约的一部分，所以带过滤的检索必须在排序前扫描所有行；
        # 否则租户/用户过滤可能漏掉 LanceDB 未过滤最近邻范围之外的有效命中。
        rows = table.to_arrow().to_pylist() if where is not None else table.search(vector).limit(top_k).to_list()
        scored: list[tuple[float, Any]] = []
        for row in rows:
            payload = _decode_payload(row["payload"])
            if where is not None and not where(payload):
                continue
            scored.append((cosine(vector, row["vector"]), payload))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def get(self, key: str) -> Any | None:
        table = self._open()
        if table is None:
            return None
        rows = table.to_arrow().to_pylist()
        for row in rows:
            if row.get("key") == key:
                return _decode_payload(row["payload"])
        return None

    def delete(self, key: str) -> None:
        table = self._open()
        if table is not None:
            table.delete(f"key = {_quote(key)}")

    def values(self) -> list[Any]:
        table = self._open()
        if table is None:
            return []
        return [_decode_payload(row["payload"]) for row in table.to_arrow().to_pylist()]
