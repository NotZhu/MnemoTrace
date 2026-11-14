"""Wire contracts for restoration, independent of new-object defaults and storage I/O."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from graphlib import CycleError, TopologicalSorter
from typing import Any, Callable

from ..types import RETENTIONS
from ..categories import CATEGORIES, validate_categories

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




def validate_coverage(value: Any, path: str) -> None:
    _object(value, path)
    if set(value) != {'unit', 'ranges', 'partial', 'policy', 'source', 'original_length', 'input_hash', 'strategy_completed'}:
        _fail(path, 'invalid coverage fields')
    _choice('unicode_code_points')(value['unit'], path + '.unit')
    _boolean(value['partial'], path + '.partial')
    _count(value['original_length'], path + '.original_length')
    _boolean(value['strategy_completed'], path + '.strategy_completed')
    message_policy = value['policy'] in {'head_tail_v1', 'full_message_v1'}
    if value['policy'] not in {'head_tail_v1', 'full_message_v1', 'full_source_v1', 'digest_source_v1'}:
        _fail(path, 'invalid coverage policy')
    if message_policy and (not value['strategy_completed'] or value['partial'] != (value['policy'] == 'head_tail_v1')):
        _fail(path, 'message coverage does not match strategy')
    for field in ('source', 'input_hash'):
        text = value[field]
        if not isinstance(text, str) or len(text) != 64 or any(c not in '0123456789abcdef' for c in text):
            _fail(path + '.' + field, 'must be a SHA256 fingerprint')
    ranges = value['ranges']
    if not isinstance(ranges, list) or len(ranges) != (2 if value['policy'] == 'head_tail_v1' else 1):
        _fail(path, 'invalid coverage ranges')
    end = 0
    for span in ranges:
        if not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span):
            _fail(path, 'invalid codepoint interval')
        if not end <= span[0] < span[1] <= value['original_length']:
            _fail(path, 'invalid codepoint bounds')
        end = span[1]
    if (ranges[0][0] != 0 or value['strategy_completed'] and end != value['original_length']
            or value['policy'] == 'head_tail_v1' and ranges[0][1] >= ranges[1][0]
            or not message_policy and value['partial'] != (end < value['original_length'])):
        _fail(path, 'ranges do not match the declared strategy')


# Explicit wire fields: adding a dataclass default must not silently change restoration.
_EPISODE = {
    "id": _identifier, "content": _string, "user_id": _identifier,
    "session_id": _identifier, "role": _nullable(_identifier), "event_time": _TIME_OR_NULL,
    "ingested_at": _number, "consolidated": _boolean,
    "digest": _string, "digest_embedding": _VECTOR, "metadata": _metadata,
    "source_kind": _choice("dialogue", "note"), "extract": _boolean,
    "message_id": _ID_OR_NULL, "sequence": _nullable(_count),
    "coverage": _nullable(validate_coverage),
    "digest_coverage": _nullable(validate_coverage),
    "digest_written_at": _TIME_OR_NULL,
}
_FACT = {
    "id": _identifier, "subject": _identifier, "predicate": _identifier, "object": _identifier,
    "text": _identifier, "user_id": _identifier, "embedding": _VECTOR,
    "salience": _number, "source": _SOURCE,
    "sensitive": _boolean, "valid_at": _TIME_OR_NULL,
    "categories": _list(_choice(*CATEGORIES)),
    "subject_id": _ID_OR_NULL, "object_id": _ID_OR_NULL,
    "subject_type": _ID_OR_NULL, "object_type": _ID_OR_NULL,
    "relation_id": _identifier,
    "retention": _choice(*RETENTIONS),
    "invalid_at": _TIME_OR_NULL, "created_at": _number, "expired_at": _TIME_OR_NULL,
    "supersedes": _list(_identifier), "provenance": _STRINGS, "last_access": _number,
    "access_count": _count,
}
def _endpoint_pair(value, path):
    _list(_ID_OR_NULL)(value, path)
    if len(value) != 2 or value[0] is None:
        _fail(path, "expected two endpoint types")


_RECORDS = {
    "Episode": _EPISODE,
    "Fact": _FACT,
    "Entity": {"id": _identifier, "name": _identifier, "type": _ID_OR_NULL,
               "user_id": _identifier, "embedding": _VECTOR},
    "Relation": {"id": _identifier, "subject_id": _identifier, "predicate": _identifier,
                 "object_id": _identifier, "fact_id": _identifier,
                 "valid_at": _TIME_OR_NULL, "invalid_at": _TIME_OR_NULL},
    "EntityType": {"id": _identifier, "meaning": _identifier, "user_id": _identifier},
    "RelationType": {"id": _identifier, "user_id": _identifier, "predicate": _identifier,
                     "meaning": _identifier, "direction": _choice("forward", "symmetric"),
                     "endpoint_types": _list(_endpoint_pair)},
}
def _fields(value: Any, spec: dict[str, Validator], path: str,
            optional: tuple[str, ...] = (), *, ignored=frozenset()) -> dict[str, Any]:
    _object(value, path)
    if value.keys() - spec.keys() - ignored:
        _fail(path, "unsupported field")
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


def validate_record(kind: str, record: Any, path: str, *,
                    embedding_dim: int | None = None, detached: bool = False) -> dict:
    spec = _RECORDS[kind]
    result = _fields(record, spec, path)
    if kind == "Episode":
        for name in ('coverage', 'digest_coverage'):
            if result[name] is not None and result[name]['original_length'] != len(result['content']):
                _fail(path, 'coverage length does not match original content')
        if embedding_dim is not None and result["digest"] and result["digest_embedding"] is None and not detached:
            _fail(path, "non-empty digest requires digest_embedding")
    if kind == "Fact":
        _validate_fact_categories(result, path)
    if embedding_dim is not None:
        for key in ("embedding", "digest_embedding"):
            if result.get(key) is not None:
                validate_embedding(result[key], f"{path}.{key}", embedding_dim)
    return result


def _validate_fact_categories(record: dict, path: str) -> None:
    try:
        validate_categories(record["categories"])
    except ValueError as exc:
        _fail(path, str(exc))


# A migration carries source identities/history, not the target user's runtime state.
_IMPORT_FACT = {key: _FACT[key] for key in (
    "id", "subject", "predicate", "object", "text", "source", "sensitive",
    "categories", "retention", "relation_id", "subject_id", "object_id",
    "subject_type", "object_type",
    "salience", "valid_at", "invalid_at", "created_at", "expired_at",
    "supersedes", "provenance",
)}
_IMPORT_FACT.update(status=_choice("live", "superseded", "scheduled"), valid_at_h=_string,
                    invalid_at_h=_nullable(_string))
_IMPORT_EPISODE = {key: _EPISODE[key] for key in (
    "id", "content", "session_id", "role", "event_time", "ingested_at", "digest", "coverage", "digest_coverage",
    "digest_written_at",
)}


# The two serializers attach presentation fields and local runtime state.
# Import restores only the portable fields described above.
_SNAPSHOT_METADATA = {"space_id", "include_sensitive", "redacted_sensitive", "focus", "graph"}
_FACT_METADATA = _FACT.keys() - _IMPORT_FACT.keys()
_EPISODE_METADATA = (_EPISODE.keys() - _IMPORT_EPISODE.keys()) | {
    "chars", "date", "session", "agent_id", "space_id",
}
SNAPSHOT_FORMAT_VERSION = 6


def _validate_snapshot_timeline(facts: list[dict]) -> None:
    positions = {row["id"]: index for index, row in enumerate(facts)}
    timeline = TopologicalSorter()
    for index, row in enumerate(facts):
        path = f"snapshot.facts[{index}]"
        start, end = row["valid_at"], row["invalid_at"]
        if start is not None and end is not None and start >= end:
            _fail(f"{path}.invalid_at", "must be later than valid_at")
        predecessors = row["supersedes"]
        if row["id"] in predecessors or len(predecessors) != len(set(predecessors)):
            _fail(f"{path}.supersedes", "self-references and duplicate predecessors are unsupported")
        timeline.add(row["id"], *(key for key in predecessors if key in positions))
    try:
        timeline.prepare()
    except CycleError as exc:
        index = positions[exc.args[1][0]]
        raise ValueError(f"snapshot.facts[{index}].supersedes: replacement cycle") from None


def is_snapshot_candidate(value: Any) -> bool:
    """Route snapshot-shaped objects to strict validation, including incomplete envelopes."""
    return isinstance(value, dict) and ("facts" in value or "episodes" in value)


def validate_snapshot(snapshot: Any) -> dict:
    _object(snapshot, "snapshot")
    if type(snapshot.get("format_version")) is not int or snapshot["format_version"] != SNAPSHOT_FORMAT_VERSION:
        _fail("snapshot.format_version", "unsupported snapshot version")
    if snapshot.keys() - _SNAPSHOT_METADATA - {"format_version", "facts", "episodes"}:
        _fail("snapshot", "unsupported field")
    for key in ("facts", "episodes"):
        if key not in snapshot:
            _fail(f"snapshot.{key}", "missing required field")
    result = {}
    for name, spec, optional, ignored in (
        ("facts", _IMPORT_FACT, ("status", "valid_at_h", "invalid_at_h"), _FACT_METADATA),
        ("episodes", _IMPORT_EPISODE, ("digest", "coverage", "digest_coverage", "ingested_at",
                                      "digest_written_at"), _EPISODE_METADATA),
    ):
        rows = snapshot.get(name, [])
        if not isinstance(rows, list):
            _fail(f"snapshot.{name}", "must be a list")
        result[name] = [_fields(row, spec, f"snapshot.{name}[{i}]", optional, ignored=ignored)
                        for i, row in enumerate(rows)]
        if name == "facts":
            for i, row in enumerate(result[name]):
                _validate_fact_categories(row, f"snapshot.facts[{i}]")
        seen = set()
        for i, row in enumerate(result[name]):
            if row["id"] in seen:
                _fail(f"snapshot.{name}[{i}].id", "duplicate source identifier")
            seen.add(row["id"])
    _validate_snapshot_timeline(result["facts"])
    return result
