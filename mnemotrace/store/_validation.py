"""Wire contracts for restoration, independent of new-object defaults and storage I/O."""
from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any, Callable

from ..topics import TOPICS, TOPIC_SOURCES, validate_topic

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
    if value['policy'] not in {'head_tail_v1', 'full_message_v1', 'full_source_v1', 'summary_source_v1'}:
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
    "ingested_at": _number, "embedding": _VECTOR, "consolidated": _boolean,
    "summary": _string, "summary_embedding": _VECTOR, "metadata": _metadata,
    "source_kind": _choice("dialogue", "raw", "document"), "scope": _choice("auto", "long", "raw", "working"),
    "message_id": _ID_OR_NULL, "sequence": _nullable(_count),
    "mode": _nullable(_choice("live", "historical")), "coverage": _nullable(validate_coverage),
    "summary_coverage": _nullable(validate_coverage),
}
_FACT = {
    "id": _identifier, "subject": _identifier, "predicate": _identifier, "object": _identifier,
    "text": _string, "display": _string, "user_id": _identifier, "embedding": _VECTOR,
    "salience": _number, "confidence": _number, "source": _SOURCE,
    "category": _string, "sensitive": _boolean, "valid_at": _TIME_OR_NULL,
    "sensitive_override": _boolean, "review_pending": _boolean, "approved_at": _TIME_OR_NULL,
    "source_roles": _list(_choice("user", "assistant")),
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
                 "valid_at": _TIME_OR_NULL, "invalid_at": _TIME_OR_NULL},
    "WorkingMemory": {"id": _identifier, "content": _string, "user_id": _identifier,
                      "session_id": _identifier, "kind": _identifier, "event_time": _TIME_OR_NULL,
                      "created_at": _number, "expires_at": _TIME_OR_NULL, "consumed": _boolean,
                      "embedding": _VECTOR, "metadata": _metadata},
    "Conflict": {"id": _identifier, "older": _identifier, "newer": _identifier,
                 "text_older": _string, "text_newer": _string, "user_id": _identifier,
                 "reason": _string, "status": _choice("pending", "resolved", "dismissed"),
                 "detected_at": _number},
}
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


def validate_record(kind: str, record: Any, path: str, *,
                    embedding_dim: int | None = None, detached: bool = False) -> dict:
    spec = _RECORDS[kind]
    result = _fields(record, spec, path)
    if kind == "Episode":
        for name in ('coverage', 'summary_coverage'):
            if result[name] is not None and result[name]['original_length'] != len(result['content']):
                _fail(path, 'coverage length does not match original content')
        if result["summary"] and result["summary_embedding"] is None and not detached:
            _fail(path, "non-empty summary requires summary_embedding")
        forbidden = {'extraction_progress', 'extraction_counts', 'extraction_error', 'memory_result', 'memory_scope', 'outcome_error', 'extraction'}
        if forbidden.intersection(result['metadata']):
            _fail(path, 'legacy processing metadata is unsupported; task migration is not provided')
    if kind == "Fact":
        _validate_topic_pair(result, path)
        if result["review_pending"] and result["approved_at"] is not None:
            _fail(path, "pending fact cannot have an approval time")
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


# A migration carries source identities/history, not the target user's runtime state.
_IMPORT_FACT = {key: _FACT[key] for key in (
    "id", "subject", "predicate", "object", "text", "source", "category", "sensitive",
    "topic", "topic_source",
    "salience", "confidence", "valid_at", "invalid_at", "created_at", "expired_at",
    "supersedes", "provenance", "source_roles", "sensitive_override", "review_pending", "approved_at",
)}
_IMPORT_FACT.update(status=_choice("live", "superseded"), valid_at_h=_string,
                    invalid_at_h=_nullable(_string))
_IMPORT_EPISODE = {key: _EPISODE[key] for key in (
    "id", "content", "session_id", "role", "event_time", "summary", "coverage", "summary_coverage",
)}


_IMPORT_CONFLICT = {key: value for key, value in _RECORDS["Conflict"].items() if key != "user_id"}


def is_snapshot_candidate(value: Any) -> bool:
    """Route snapshot-shaped objects to strict validation, including incomplete envelopes."""
    return isinstance(value, dict) and ("facts" in value or "episodes" in value)


def validate_snapshot(snapshot: Any) -> dict:
    _object(snapshot, "snapshot")
    for key in ("facts", "episodes"):
        if key not in snapshot:
            _fail(f"snapshot.{key}", "missing required field")
    result = {}
    for name, spec, optional in (("facts", _IMPORT_FACT, ("text", "status", "valid_at_h", "invalid_at_h", "source_roles", "sensitive_override", "review_pending", "approved_at")),
                                 ("episodes", _IMPORT_EPISODE, ("summary", "coverage", "summary_coverage")),
                                 ("conflicts", _IMPORT_CONFLICT, ())):
        rows = snapshot.get(name, [])
        if not isinstance(rows, list):
            _fail(f"snapshot.{name}", "must be a list")
        if name == "facts":
            for i, row in enumerate(rows):
                if isinstance(row, dict) and "assertion_kind" in row:
                    _fail(f"snapshot.facts[{i}].assertion_kind", "legacy fact format is unsupported")
        result[name] = [_fields(row, spec, f"snapshot.{name}[{i}]", optional)
                        for i, row in enumerate(rows)]
        if name == "facts":
            for i, row in enumerate(result[name]):
                _validate_topic_pair(row, f"snapshot.facts[{i}]")
                if row.get("review_pending") and row.get("approved_at") is not None:
                    _fail(f"snapshot.facts[{i}]", "pending fact cannot have an approval time")
        seen = set()
        for i, row in enumerate(result[name]):
            if row["id"] in seen:
                _fail(f"snapshot.{name}[{i}].id", "duplicate source identifier")
            seen.add(row["id"])
    fact_ids = {row["id"] for row in result["facts"]}
    paired = set()
    pairs = set()
    for i, row in enumerate(result["conflicts"]):
        pair = frozenset((row["older"], row["newer"]))
        if len(pair) != 2 or not pair <= fact_ids or pair in pairs:
            _fail(f"snapshot.conflicts[{i}]", "invalid or duplicate fact pair")
        pairs.add(pair)
        paired.update(pair)
    for i, row in enumerate(result["facts"]):
        if row.get("review_pending") and row["id"] not in paired:
            _fail(f"snapshot.facts[{i}]", "pending fact has no conflict")
    return result
