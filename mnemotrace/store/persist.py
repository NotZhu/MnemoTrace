"""内存版参考存储的安全 JSONL 持久化。

这个格式刻意只存纯记录（plain record），不存 Python 对象 —— 加载过程因此不可执行任意代码，同时
保留了双时间轴字段；正是这些字段让 忆痕 成为一个记忆系统，而不是一个覆盖式缓存。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import fields
from typing import Any, Iterable

from .. import _flock, __version__
from ..ingest import IdentityResolver
from ..types import Conflict, Entity, Episode, Fact, Relation, WorkingMemory
from ..util import now
from .memory_store import InMemoryDocStore, InMemoryGraphStore

SCHEMA_VERSION = 1
COLLECTIONS = ("episodes", "facts", "entities", "relations", "working", "conflicts")


class PersistenceError(Exception):
    """存储持久化错误的基类。"""


class IncompatibleStoreError(PersistenceError):
    """该存储由当前版本无法读取的 schema 写成。"""


class DimensionMismatchError(PersistenceError):
    """存储的嵌入维度与当前配置的嵌入器不一致。"""


class EmbedderMismatchError(PersistenceError):
    """存储记录的嵌入模型标识与当前配置的嵌入器不一致。"""


class StoreFormatError(PersistenceError):
    """该路径或 manifest 不是合法的 忆痕 JSONL 存储。"""


def _embedder_id(embedder: Any) -> str:
    name = getattr(embedder, "model_name", None) or getattr(embedder, "model", None)
    return str(name or embedder.__class__.__name__)


def _embedder_dim(embedder: Any) -> int | None:
    dim = getattr(embedder, "dim", None)
    return int(dim) if dim is not None else None


def _record(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def _from_record(cls: type, record: dict[str, Any]):
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in record.items() if k in allowed})


def _json_dump(obj: Any, fh) -> None:
    fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    fh.write("\n")


@contextmanager
def _lock(path: str):
    os.makedirs(path, exist_ok=True)
    lock_path = os.path.join(path, ".lock")
    with open(lock_path, "a", encoding="utf-8") as fh:
        _flock.acquire(fh)  # 阻塞式：多进程间的保存不得交错执行
        try:
            yield
        finally:
            _flock.release(fh)


def _write_jsonl(path: str, name: str, rows: Iterable[dict[str, Any]]) -> int:
    tmp = os.path.join(path, f"{name}.jsonl.tmp")
    final = os.path.join(path, f"{name}.jsonl")
    count = 0
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            _json_dump(row, fh)
            count += 1
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return count


def _read_jsonl(path: str, name: str, limit: int | None = None) -> list[dict[str, Any]]:
    file_path = os.path.join(path, f"{name}.jsonl")
    if not os.path.exists(file_path):
        if limit:
            raise StoreFormatError(f"{name}.jsonl is missing {limit} committed record(s)")
        return []
    out: list[dict[str, Any]] = []
    with open(file_path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if limit is not None and len(out) >= limit:
                break
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise StoreFormatError(
                    f"{name}.jsonl has malformed JSON inside the committed prefix at line {line_no}"
                ) from exc
    if limit is not None and len(out) < limit:
        raise StoreFormatError(
            f"{name}.jsonl has {len(out)} committed record(s), manifest requires {limit}"
        )
    return out


def _manifest_count(counts: dict[str, Any], name: str) -> int:
    try:
        count = int(counts[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreFormatError(f"manifest missing valid count for {name}") from exc
    if count < 0:
        raise StoreFormatError(f"manifest count for {name} must be non-negative")
    return count


def _memory_state(mem: Any) -> dict[str, Any]:
    return {
        "resolver_parent": dict(getattr(mem.resolver, "_parent", {})),
        "persona_cache": dict(getattr(mem, "_persona_cache", {})),
        "focus": getattr(mem, "focus", {"track": [], "mute": []}),
        "policy": getattr(mem, "policy", {}),
        "identity": dict(getattr(mem, "_identity", {})),
        "aliases": {k: sorted(v) for k, v in getattr(mem, "_aliases", {}).items()},
        "cold_pages_out": dict(getattr(mem, "cold_pages_out", {})),
        "cold_pages_in": dict(getattr(mem, "cold_pages_in", {})),
        # 幂等导入的指纹（仅内容哈希，不含记忆内容本身），保证重启后重复导入仍能去重
        "import_seen": sorted(getattr(mem, "_import_seen", ())),
    }


def save_memory(mem: Any, path: str, backend: str = "durable") -> None:
    """把一个 Memory 实例持久化到带 schema 版本的 JSONL 目录。"""
    if os.path.exists(path) and not os.path.isdir(path):
        raise StoreFormatError(f"store path exists and is not a directory: {path}")

    with _lock(path):
        episodes = sorted(mem.episodes_doc.values(), key=lambda e: e.id)
        hot_ids = {f.id for f in mem.fact_store.values()}
        facts = []
        for fact in sorted(mem.fact_store.values(), key=lambda f: f.id):
            row = _record(fact)
            row["_store"] = "hot"
            facts.append(row)
        for fact in sorted(mem.cold_store.values(), key=lambda f: f.id):
            if fact.id in hot_ids:
                continue
            row = _record(fact)
            row["_store"] = "cold"
            facts.append(row)
        counts = {
            "episodes": _write_jsonl(path, "episodes", (_record(e) for e in episodes)),
            "facts": _write_jsonl(path, "facts", facts),
            "entities": _write_jsonl(
                path, "entities", (_record(e) for e in sorted(mem.graph.entities.values(), key=lambda e: e.id))
            ),
            "relations": _write_jsonl(
                path, "relations", (_record(r) for r in sorted(mem.graph.relations(), key=lambda r: r.id))
            ),
            "working": _write_jsonl(
                path, "working", (_record(w) for w in sorted(mem.working_mem.values(), key=lambda w: w.id))
            ),
            "conflicts": _write_jsonl(
                path, "conflicts", (_record(c) for c in sorted(mem.conflicts.values(), key=lambda c: c.id))
            ),
        }
        for name in COLLECTIONS:
            counts.setdefault(name, 0)

        created_at = now()
        manifest_path = os.path.join(path, "manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as fh:
                    created_at = float(json.load(fh).get("created_at", created_at))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "mnemotrace_version": __version__,
            "embedder_id": _embedder_id(mem.embedder),
            "embedding_dim": _embedder_dim(mem.embedder),
            "backend": backend,
            "created_at": created_at,
            "updated_at": now(),
            "counts": counts,
            "state": _memory_state(mem),
        }
        tmp = os.path.join(path, "manifest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, manifest_path)


def load_memory(mem: Any, path: str, allow_mismatch: bool = False) -> bool:
    """把 JSONL 存储的内容加载进一个已有的 Memory 实例。

    若 manifest 不存在则返回 False，表示"从空开始"。`allow_mismatch=True` 会跳过嵌入器
    身份/维度校验 —— 仅用于显式的重新嵌入迁移（`Memory.reembed()`），它会在加载后立即替换
    所有已存储的向量。
    """
    if os.path.exists(path) and not os.path.isdir(path):
        raise StoreFormatError(
            f"{path} is not an MnemoTrace JSONL store directory; run the explicit pickle migration first"
        )
    manifest_path = os.path.join(path, "manifest.json")
    if not os.path.exists(manifest_path):
        return False
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    schema = int(manifest.get("schema_version", 0))
    if schema > SCHEMA_VERSION:
        raise IncompatibleStoreError(
            f"store schema_version={schema} is newer than supported schema_version={SCHEMA_VERSION}"
        )
    stored_dim = manifest.get("embedding_dim")
    current_dim = _embedder_dim(mem.embedder)
    if not allow_mismatch and stored_dim is not None and current_dim is not None \
            and int(stored_dim) != int(current_dim):
        raise DimensionMismatchError(
            f"store embedding_dim={stored_dim} does not match current embedder dim={current_dim}; "
            "re-attach the original embedder, or migrate with Memory.open(path, allow_mismatch=True, ...)"
            ".reembed() (service: MNEMOTRACE_REEMBED_ON_MISMATCH=1)"
        )
    stored_embedder = manifest.get("embedder_id")
    current_embedder = _embedder_id(mem.embedder)
    if not allow_mismatch and stored_embedder is not None and str(stored_embedder) != current_embedder:
        raise EmbedderMismatchError(
            f"store embedder_id={stored_embedder!r} does not match current embedder_id={current_embedder!r}; "
            "re-attach the original embedder, or migrate with Memory.open(path, allow_mismatch=True, ...)"
            ".reembed() (service: MNEMOTRACE_REEMBED_ON_MISMATCH=1)"
        )

    counts = manifest.get("counts") or {}
    expected_counts = {name: _manifest_count(counts, name) for name in COLLECTIONS}

    episodes = [_from_record(Episode, row) for row in _read_jsonl(path, "episodes", expected_counts["episodes"])]
    facts = [
        (row.get("_store", "hot"), _from_record(Fact, row))
        for row in _read_jsonl(path, "facts", expected_counts["facts"])
    ]
    entities = [_from_record(Entity, row) for row in _read_jsonl(path, "entities", expected_counts["entities"])]
    relations = [
        _from_record(Relation, row)
        for row in _read_jsonl(path, "relations", expected_counts["relations"])
    ]
    working_items = [
        _from_record(WorkingMemory, row)
        for row in _read_jsonl(path, "working", expected_counts["working"])
    ]
    conflict_items = [
        _from_record(Conflict, row)
        for row in _read_jsonl(path, "conflicts", expected_counts["conflicts"])
    ]

    def clear_vector(store):
        for payload in list(store.values()):
            key = getattr(payload, "id", None)
            if key is not None:
                store.delete(key)

    mem.episodes_doc = InMemoryDocStore()
    clear_vector(mem.episodes_vec)
    clear_vector(mem.fact_store)
    clear_vector(mem.cold_store)
    clear_vector(mem.summary_vec)
    mem.graph = InMemoryGraphStore()

    for ep in episodes:
        mem.episodes_doc.put(ep.id, ep)
        mem.episodes_vec.upsert(ep.id, ep.embedding or [], ep)
        if ep.summary_embedding is not None:
            mem.summary_vec.upsert(ep.id, ep.summary_embedding, ep)

    for tier, fact in facts:
        target = mem.cold_store if tier == "cold" else mem.fact_store
        target.upsert(fact.id, fact.embedding or [], fact)

    for entity in entities:
        mem.graph.upsert_entity(entity)
    for relation in relations:
        mem.graph.add_relation(relation)

    mem.working_mem = {}
    for item in working_items:
        mem.working_mem[item.id] = item
    mem.conflicts = {}
    for item in conflict_items:
        mem.conflicts[item.id] = item

    state = manifest.get("state") or {}
    mem.resolver = IdentityResolver()
    mem.resolver._parent.update(state.get("resolver_parent") or {})
    mem._persona_cache = state.get("persona_cache") or {}
    mem.focus = state.get("focus") or {"track": [], "mute": []}
    mem.policy = state.get("policy") or {
        "extract_instruction": "",
        "extract_system": "",
        "summary_system": "",
        "persona_system": "",
    }
    mem._identity = state.get("identity") or {}
    mem._aliases = {k: set(v) for k, v in (state.get("aliases") or {}).items()}
    mem.cold_pages_out = {k: int(v) for k, v in (state.get("cold_pages_out") or {}).items()}
    mem.cold_pages_in = {k: int(v) for k, v in (state.get("cold_pages_in") or {}).items()}
    mem._import_seen = set(state.get("import_seen") or ())
    return True
