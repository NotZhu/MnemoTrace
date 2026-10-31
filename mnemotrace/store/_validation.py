"""Wire contracts for restoration, independent of new-object defaults and storage I/O."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Callable

from ..topics import TOPICS, TOPIC_SOURCES, validate_topic

COLLECTIONS = ("episodes", "facts", "entities", "relations", "working", "conflicts")
Validator = Callable[[Any, str], None]


def _fail(path: str, reason: str) -> None:
    raise ValueError(f"{path}: {reason}")


def _string(value: Any, path: str) -> None:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(path, "must be valid UTF-8 text")


def _identifier(value: Any, path: str) -> None:
    _string(value, path)
    if not value.strip():
        _fail(path, "must be a non-empty string")


def _number(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or isinstance(value, float) and not math.isfinite(value):
        _fail(path, "must be a finite number")


def _integer(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "must be an integer")


def _count(value: Any, path: str) -> None:
    _integer(value, path)
    if value < 0:
        _fail(path, "must be non-negative")


def _dimension(value: Any, path: str) -> None:
    _integer(value, path)
    if value <= 0:
        _fail(path, "must be positive")


def _boolean(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "must be a boolean")


def _object(value: Any, path: str) -> None:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        _fail(path, "must be an object with string keys")
    for key in value:
        _string(key, f"{path}.keys")


def _metadata(value: Any, path: str) -> None:
    _object(value, path)
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        _fail(path, "must contain finite JSON values")


def _nullable(check: Validator) -> Validator:
    def validate(value: Any, path: str) -> None:
        if value is not None:
            check(value, path)
    return validate


def _list(check: Validator) -> Validator:
    def validate(value: Any, path: str) -> None:
        if not isinstance(value, list):
            _fail(path, "must be a list")
        for index, item in enumerate(value):
            check(item, f"{path}[{index}]")
    return validate


def _mapping(check: Validator) -> Validator:
    def validate(value: Any, path: str) -> None:
        _object(value, path)
        for item in value.values():
            check(item, f"{path}.values")
    return validate


def _choice(*choices: str) -> Validator:
    def validate(value: Any, path: str) -> None:
        _string(value, path)
        if value not in choices:
            _fail(path, "must be one of " + ", ".join(choices))
    return validate


_STRINGS = _list(_string)
_VECTOR = _nullable(_list(_number))
_TIME_OR_NULL = _nullable(_number)
_ID_OR_NULL = _nullable(_identifier)
_SOURCE = _choice("user", "extracted")

# Explicit wire fields: adding a dataclass default must not silently change restoration.
_EPISODE = {
    "id": _identifier, "content": _string, "user_id": _identifier,
    "session_id": _identifier, "speaker": _identifier, "event_time": _number,
    "ingested_at": _number, "embedding": _VECTOR, "consolidated": _boolean,
    "summary": _string, "summary_embedding": _VECTOR, "metadata": _metadata,
}
_FACT = {
    "id": _identifier, "subject": _identifier, "predicate": _identifier, "object": _identifier,
    "text": _string, "display": _string, "user_id": _identifier, "embedding": _VECTOR,
    "salience": _number, "confidence": _number, "source": _SOURCE,
    "category": _string, "sensitive": _boolean, "valid_at": _number,
    "topic": _choice(*TOPICS), "topic_source": _choice(*TOPIC_SOURCES),
    "invalid_at": _TIME_OR_NULL, "created_at": _number, "expired_at": _TIME_OR_NULL,
    "supersedes": _ID_OR_NULL, "provenance": _STRINGS, "last_access": _number,
    "access_count": _count,
}
_RECORDS = {
    "Episode": _EPISODE,
    "Fact": _FACT,
    "Entity": {"id": _identifier, "name": _identifier, "type": _identifier,
               "user_id": _identifier, "embedding": _VECTOR, "aliases": _STRINGS},
    "Relation": {"id": _identifier, "subject_id": _identifier, "predicate": _identifier,
                 "object_id": _identifier, "fact_id": _identifier,
                 "valid_at": _number, "invalid_at": _TIME_OR_NULL},
    "WorkingMemory": {"id": _identifier, "content": _string, "user_id": _identifier,
                      "session_id": _identifier, "kind": _identifier, "event_time": _number,
                      "created_at": _number, "expires_at": _TIME_OR_NULL, "consumed": _boolean,
                      "embedding": _VECTOR, "metadata": _metadata},
    "Conflict": {"id": _identifier, "older": _identifier, "newer": _identifier,
                 "text_older": _string, "text_newer": _string, "user_id": _identifier,
                 "reason": _string, "status": _choice("pending", "resolved", "dismissed"),
                 "detected_at": _number},
}
_POLICY = {key: _string for key in (
    "extract_instruction", "extract_system", "summary_system", "persona_system",
)}
_FOCUS = {"track": _STRINGS, "mute": _STRINGS}


def _fields(value: Any, spec: dict[str, Validator], path: str,
            optional: tuple[str, ...] = ()) -> dict[str, Any]:
    _object(value, path)
    for key, check in spec.items():
        if key not in value:
            if key in optional:
                continue
            _fail(f"{path}.{key}", "missing required field")
        check(value[key], f"{path}.{key}")
    return {key: deepcopy(value[key]) for key in spec if key in value}


def validate_embedding(vector: Any, path: str, dimension: int) -> None:
    _dimension(dimension, f"{path}.dimension")
    _list(_number)(vector, path)
    if len(vector) != dimension:
        _fail(path, "length must match embedding_dim")


def validate_record(kind: str, record: Any, path: str, *, jsonl_fact: bool = False,
                    embedding_dim: int | None = None) -> dict:
    spec = _RECORDS[kind]
    if jsonl_fact:
        spec = {**spec, "_store": _choice("hot", "cold")}
    result = _fields(record, spec, path)
    if kind == "Episode":
        if result["summary"] and result["summary_embedding"] is None:
            _fail(path, "non-empty summary requires summary_embedding")
        from ..consolidate.processing import validate_progress
        progress = validate_progress(result["content"], result["metadata"])
        if progress and result["consolidated"] != (progress["completed_through"] == len(result["content"])):
            _fail(path, "completion flag must match processing range")
    if kind == "Fact":
        _validate_topic_pair(result, path)
    if embedding_dim is not None:
        for key in ("embedding", "summary_embedding"):
            if result.get(key) is not None:
                validate_embedding(result[key], f"{path}.{key}", embedding_dim)
    return result


def _validate_topic_pair(record: dict, path: str) -> None:
    try:
        validate_topic(record["topic"], record["topic_source"])
    except ValueError as exc:
        _fail(path, str(exc))


def validate_state(state: Any, path: str = "manifest.state") -> dict:
    result = _fields(state, {
        "focus": lambda v, p: _fields(v, _FOCUS, p),
        "policy": lambda v, p: _fields(v, _POLICY, p),
        "identity": _mapping(_string), "aliases": _mapping(_STRINGS), "import_seen": _STRINGS,
        "persona_cache": _mapping(_string),
        "cold_pages_out": _mapping(_count), "cold_pages_in": _mapping(_count),
    }, path)
    if 'summarization' in state:
        from ..consolidate.summaries import validate_state as validate_summaries
        result['summarization'] = validate_summaries(state['summarization'])
    if 'processing' in state:
        from ..consolidate.tasks import validate_tasks
        result['processing'] = validate_tasks(state['processing'])
    return result


def validate_manifest(manifest: Any) -> dict:
    _object(manifest, "manifest")
    result = _fields(manifest, {
        "snapshot_id": _identifier, "embedder_id": _identifier,
        "embedding_dim": _dimension, "backend": _choice("durable", "lancedb", "qdrant"),
        "mnemotrace_version": _identifier, "created_at": _number, "updated_at": _number,
        "counts": lambda v, p: _fields(v, {key: _count for key in COLLECTIONS}, p),
        "state": _object,
    }, "manifest")
    result["state"] = validate_state(result["state"])
    return result


# A migration carries source identities/history, not the target user's runtime state.
_IMPORT_FACT = {key: _FACT[key] for key in (
    "id", "subject", "predicate", "object", "text", "source", "category", "sensitive",
    "topic", "topic_source",
    "salience", "confidence", "valid_at", "invalid_at", "created_at", "expired_at",
    "supersedes", "provenance",
)}
_IMPORT_FACT.update(status=_choice("live", "superseded"), valid_at_h=_string,
                    invalid_at_h=_nullable(_string))
_IMPORT_EPISODE = {key: _EPISODE[key] for key in (
    "id", "content", "session_id", "speaker", "event_time", "summary",
)}


def is_snapshot_candidate(value: Any) -> bool:
    """Route snapshot-shaped objects to strict validation, including incomplete envelopes."""
    return isinstance(value, dict) and ("facts" in value or "episodes" in value)


def validate_snapshot(snapshot: Any) -> dict:
    _object(snapshot, "snapshot")
    for key in ("facts", "episodes"):
        if key not in snapshot:
            _fail(f"snapshot.{key}", "missing required field")
    result = {}
    for name, spec, optional in (("facts", _IMPORT_FACT, ("text", "status", "valid_at_h", "invalid_at_h")),
                                 ("episodes", _IMPORT_EPISODE, ("summary",))):
        rows = snapshot[name]
        if not isinstance(rows, list):
            _fail(f"snapshot.{name}", "must be a list")
        result[name] = [_fields(row, spec, f"snapshot.{name}[{i}]", optional)
                        for i, row in enumerate(rows)]
        if name == "facts":
            for i, row in enumerate(result[name]):
                _validate_topic_pair(row, f"snapshot.facts[{i}]")
        seen = set()
        for i, row in enumerate(result[name]):
            if row["id"] in seen:
                _fail(f"snapshot.{name}[{i}].id", "duplicate source identifier")
            seen.add(row["id"])
    return result
