"""内存版参考存储的安全 JSONL 持久化。

这个格式刻意只存纯记录（plain record），不存 Python 对象 —— 加载过程因此不可执行任意代码，同时
保留了双时间轴字段；正是这些字段让 忆痕 成为一个记忆系统，而不是一个覆盖式缓存。
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import fields
from typing import Any, Iterable

from .. import __version__, _flock
from ..types import Conflict, Entity, Episode, Fact, Relation, WorkingMemory
from ..util import now
from ._validation import (
    COLLECTIONS, validate_manifest, validate_record, validate_state,
)
from .memory_store import InMemoryDocStore, InMemoryGraphStore


class PersistenceError(Exception):
    """存储持久化错误的基类。"""


class DimensionMismatchError(PersistenceError):
    """存储的嵌入维度与当前配置的嵌入器不一致。"""


class EmbedderMismatchError(PersistenceError):
    """存储记录的嵌入模型标识与当前配置的嵌入器不一致。"""


class StoreFormatError(PersistenceError):
    """该路径或 manifest 不是合法的 忆痕 JSONL 存储。"""


def _open_manifest(path: str):
    if os.name != "nt":
        return open(path, "rb")
    import _winapi
    import msvcrt

    # CRT 的 open() 不允许共享删除；版本检查须允许原子提交，
    # 即使另一进程仍持有旧 manifest 的读取句柄也不能阻止替换。
    share_read_write_delete = 0x1 | 0x2 | 0x4
    handle = _winapi.CreateFile(path, _winapi.GENERIC_READ, share_read_write_delete,
                               0, _winapi.OPEN_EXISTING, 0x80, 0)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:  # noqa: BLE001 -- 句柄所有权尚未转交给 CRT，失败时须自行关闭
        _winapi.CloseHandle(handle)
        raise
    try:
        return os.fdopen(fd, "rb")
    except BaseException:  # noqa: BLE001 -- 中断时也须关闭已经转交的文件描述符
        os.close(fd)
        raise


def _replace_manifest(source: str, destination: str) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    import ctypes
    from ctypes import wintypes

    # 即使设置 FILE_SHARE_DELETE，MoveFileEx（os.replace）仍拒绝替换在用目标；
    # ReplaceFile 保持原子替换，并允许共享删除的已有读者继续读取旧文件。
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    replace = kernel.ReplaceFileW
    replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
    replace.restype = wintypes.BOOL
    if not replace(destination, source, None, 0, None, None):
        error = ctypes.get_last_error()
        if error in (2, 3):  # 首次提交时尚无可替换的目标文件。
            os.replace(source, destination)
        else:
            raise ctypes.WinError(error)


def _read_manifest(path: str) -> tuple[dict[str, Any], str] | None:
    try:
        with _open_manifest(os.path.join(path, "manifest.json")) as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    try:
        manifest = _validated_manifest(json.loads(raw.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StoreFormatError("manifest: malformed JSON or encoding") from exc
    return manifest, f"snapshot:{manifest['snapshot_id']}"


def _validated_manifest(manifest: Any) -> dict:
    try:
        return validate_manifest(manifest)
    except ValueError as exc:
        raise StoreFormatError(str(exc)) from exc


def snapshot_version(path: str) -> str | None:
    """读取原子提交的 manifest 标识，无需读取集合文件。"""
    result = _read_manifest(path)
    return None if result is None else result[1]


def _embedder_id(embedder: Any) -> str:
    name = getattr(embedder, "model_name", None) or getattr(embedder, "model", None)
    return str(name or embedder.__class__.__name__)


def _embedder_dim(embedder: Any) -> int:
    dim = getattr(embedder, "dim", None)
    if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
        raise StoreFormatError("embedder.embedding_dim: must be a positive integer")
    return dim


def _record(obj: Any) -> dict[str, Any]:
    return {f.name: getattr(obj, f.name) for f in fields(obj)}


def _from_record(cls: type, record: Any, location: str | None = None):
    try:
        values = validate_record(cls.__name__, record, location or cls.__name__)
    except ValueError as exc:
        raise StoreFormatError(str(exc)) from exc
    return _restore_record(cls, values)


def _restore_record(cls: type, values: dict):
    obj = cls(**values)
    if cls is Fact:
        # 已存空正文是合法值，不能被新建 Fact 的三元组默认正文覆盖。
        obj.text = values["text"]
    return obj


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
    # Decode only committed lines; even invalid UTF-8 in an uncommitted tail is irrelevant.
    with open(file_path, "rb") as fh:
        for line_no, line in enumerate(fh, start=1):
            if limit is not None and len(out) >= limit:
                break
            try:
                out.append(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise StoreFormatError(
                    f"{name}.jsonl has malformed JSON inside the committed prefix at line {line_no}"
                ) from exc
    if limit is not None and len(out) < limit:
        raise StoreFormatError(
            f"{name}.jsonl has {len(out)} committed record(s), manifest requires {limit}"
        )
    return out


def _memory_state(mem: Any) -> dict[str, Any]:
    for key, attribute in (("focus", "focus"), ("policy", "policy"), ("identity", "_identity"),
                           ("aliases", "_aliases"), ("import_seen", "_import_seen")):
        if not hasattr(mem, attribute):
            raise StoreFormatError(f"manifest.state.{key}: missing required runtime attribute")
    try:
        if not isinstance(mem._import_seen, set) or any(not isinstance(v, str) for v in mem._import_seen):
            raise StoreFormatError("manifest.state.import_seen: must be a set of strings in memory")
        if not isinstance(mem._aliases, dict) or any(
            not isinstance(v, set) or any(not isinstance(name, str) for name in v)
            for v in mem._aliases.values()
        ):
            raise StoreFormatError("manifest.state.aliases: must map strings to sets of strings in memory")
        state = {
            "persona_cache": getattr(mem, "_persona_cache", {}), "focus": mem.focus, "policy": mem.policy,
            "identity": mem._identity, "aliases": {k: sorted(v) for k, v in mem._aliases.items()},
            "cold_pages_out": getattr(mem, "cold_pages_out", {}),
            "cold_pages_in": getattr(mem, "cold_pages_in", {}),
            "import_seen": sorted(mem._import_seen),
        }
        if mem._processing_state is not None:
            from ..consolidate.tasks import validate_task_sources
            validate_task_sources(mem._processing_state, mem.episodes_doc.values())
            state['processing'] = mem._processing_state
        return validate_state(state)
    except ValueError as exc:
        raise StoreFormatError(str(exc)) from exc


def save_memory(mem: Any, path: str, backend: str = "durable") -> None:
    """把一个 Memory 实例持久化到当前字段契约的 JSONL 目录。"""
    if os.path.exists(path) and not os.path.isdir(path):
        raise StoreFormatError(f"store path exists and is not a directory: {path}")

    with _lock(path):
        previous = _read_manifest(path)
        episodes = mem.episodes_doc.values()
        facts = []
        for fact in mem.fact_store.values():
            row = _record(fact)
            row["_store"] = "hot"
            facts.append(row)
        for fact in mem.cold_store.values():
            row = _record(fact)
            row["_store"] = "cold"
            facts.append(row)
        collections = {
            "episodes": [_record(e) for e in episodes], "facts": facts,
            "entities": [_record(e) for e in mem.graph.entities.values()],
            "relations": [_record(r) for r in mem.graph.relations()],
            "working": [_record(w) for w in mem.working_mem.values()],
            "conflicts": [_record(c) for c in mem.conflicts.values()],
        }
        kinds = (Episode, Fact, Entity, Relation, WorkingMemory, Conflict)
        dimension = _embedder_dim(mem.embedder)
        try:
            for (name, rows), cls in zip(collections.items(), kinds):
                for i, row in enumerate(rows, 1):
                    validate_record(cls.__name__, row, f"{name}.jsonl:{i}",
                                    jsonl_fact=name == "facts", embedding_dim=dimension)
        except ValueError as exc:
            raise StoreFormatError(str(exc)) from exc
        hot_ids = {row["id"] for row in facts if row["_store"] == "hot"}
        collections["facts"] = [row for row in facts if row["_store"] == "hot" or row["id"] not in hot_ids]
        for rows in collections.values():
            rows.sort(key=lambda row: row["id"])
        manifest_path = os.path.join(path, "manifest.json")
        manifest = _validated_manifest({
            "snapshot_id": uuid.uuid4().hex,
            "mnemotrace_version": __version__,
            "embedder_id": _embedder_id(mem.embedder),
            "embedding_dim": dimension,
            "backend": backend,
            "created_at": previous[0]["created_at"] if previous else now(),
            "updated_at": now(),
            "counts": {name: len(rows) for name, rows in collections.items()},
            "state": _memory_state(mem),
        })
        # All record/state validation must finish before the first collection is replaced.
        for name, rows in collections.items():
            _write_jsonl(path, name, rows)
        tmp = os.path.join(path, "manifest.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_manifest(tmp, manifest_path)
        # 记录实际提交的标识，不能根据随后取得的文件状态或内容反推版本。
        mem._snapshot_version = f"snapshot:{manifest['snapshot_id']}"


def load_memory(mem: Any, path: str, allow_mismatch: bool = False) -> bool:
    """把 JSONL 存储的内容加载进一个已有的 Memory 实例。

    若 manifest 不存在则返回 False，表示"从空开始"。`allow_mismatch=True` 会跳过嵌入器
    身份/维度校验 —— 仅用于显式的重新嵌入迁移（`Memory.reembed()`），它会在加载后立即替换
    所有已存储的向量。
    """
    if os.path.exists(path) and not os.path.isdir(path):
        raise StoreFormatError(
            f"{path} is not an MnemoTrace JSONL store directory"
        )
    loaded = _read_manifest(path)
    if loaded is None:
        mem._snapshot_version = None
        return False
    manifest, version = loaded
    stored_dim = manifest["embedding_dim"]
    current_dim = _embedder_dim(mem.embedder)
    if not allow_mismatch and stored_dim != current_dim:
        raise DimensionMismatchError(
            f"store embedding_dim={stored_dim} does not match current embedder dim={current_dim}; "
            "re-attach the original embedder, or migrate with Memory.open(path, allow_mismatch=True, ...)"
            ".reembed() (service: MNEMOTRACE_REEMBED_ON_MISMATCH=1)"
        )
    stored_embedder = manifest["embedder_id"]
    current_embedder = _embedder_id(mem.embedder)
    if not allow_mismatch and stored_embedder != current_embedder:
        raise EmbedderMismatchError(
            f"store embedder_id={stored_embedder!r} does not match current embedder_id={current_embedder!r}; "
            "re-attach the original embedder, or migrate with Memory.open(path, allow_mismatch=True, ...)"
            ".reembed() (service: MNEMOTRACE_REEMBED_ON_MISMATCH=1)"
        )

    state = manifest["state"]
    aliases = {k: set(v) for k, v in state["aliases"].items()}
    import_seen = set(state["import_seen"])
    prepared = {}
    kinds = (Episode, Fact, Entity, Relation, WorkingMemory, Conflict)
    for name, cls in zip(COLLECTIONS, kinds):
        prepared[name] = []
        for i, row in enumerate(_read_jsonl(path, name, manifest["counts"][name]), 1):
            location = f"{name}.jsonl:{i}"
            try:
                values = validate_record(cls.__name__, row, location, jsonl_fact=name == "facts",
                                         embedding_dim=stored_dim)
            except ValueError as exc:
                raise StoreFormatError(str(exc)) from exc
            tier = values.pop("_store", None)
            obj = _restore_record(cls, values)
            prepared[name].append((tier, obj) if name == "facts" else obj)

    from ..consolidate.tasks import validate_task_sources
    try:
        validate_task_sources(state.get('processing'), prepared['episodes'])
    except ValueError as exc:
        raise StoreFormatError(str(exc)) from exc
    finish_vector_restore = mem._prepare_vector_restore(stored_dim, allow_mismatch)

    # Decoding existing persistent payloads can itself fail. Inspect every index before
    # deleting anything, so a corrupt later index cannot leave earlier ones emptied.
    clear_plan = [(store, [payload.id for payload in store.values() if hasattr(payload, "id")])
                  for store in (mem.episodes_vec, mem.fact_store, mem.cold_store, mem.summary_vec)]
    if finish_vector_restore is not None:
        finish_vector_restore()
    mem.episodes_doc = InMemoryDocStore()
    for store, keys in clear_plan:
        for key in keys:
            store.delete(key)
    mem.graph = InMemoryGraphStore()

    for ep in prepared["episodes"]:
        mem.episodes_doc.put(ep.id, ep)
        mem.episodes_vec.upsert(ep.id, ep.embedding or [], ep)
        if ep.summary_embedding is not None:
            mem.summary_vec.upsert(ep.id, ep.summary_embedding, ep)

    for tier, fact in prepared["facts"]:
        target = mem.cold_store if tier == "cold" else mem.fact_store
        target.upsert(fact.id, fact.embedding or [], fact)

    for entity in prepared["entities"]:
        mem.graph.upsert_entity(entity)
    for relation in prepared["relations"]:
        mem.graph.add_relation(relation)

    mem.working_mem = {}
    for item in prepared["working"]:
        mem.working_mem[item.id] = item
    mem.conflicts = {}
    for item in prepared["conflicts"]:
        mem.conflicts[item.id] = item

    mem._persona_cache = state["persona_cache"]
    mem.focus = state["focus"]
    mem.policy = state["policy"]
    mem._identity = state["identity"]
    mem._aliases = aliases
    mem.cold_pages_out = state["cold_pages_out"]
    mem.cold_pages_in = state["cold_pages_in"]
    mem._import_seen = import_seen
    mem._processing_state = state.get('processing')
    mem._snapshot_version = version
    return True
