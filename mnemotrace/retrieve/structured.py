"""Validation and execution shared by explicit memory queries."""
from dataclasses import dataclass
from ..consolidate.fact_rules import object_key

from ..store.contracts import FactQuery, _id, _time
from ..types import Fact
from .events import EventEvidence


@dataclass(frozen=True)
class AggregateResult:
    count: int
    facts: tuple[Fact, ...]
    candidates: tuple[EventEvidence, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class PathMatch:
    endpoint: str
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class PathResult:
    paths: tuple[PathMatch, ...]
    limitations: tuple[str, ...] = ()


def _query_value(value):
    _id(value)
    if '\x00' in value:
        raise ValueError('Unexpected NUL in query input')


def validate_aggregate(operation, scope, start, end, query):
    if operation not in ('count', 'count_distinct'):
        raise ValueError('Expected count or count_distinct')
    if not isinstance(scope, FactQuery) or scope.predicate is None or scope.after_id is not None:
        raise ValueError('Aggregation requires a predicate and accepts no cursor')
    _time(start)
    _time(end)
    if start is not None and end is not None and start >= end:
        raise ValueError('Expected start before end')
    for value in (scope.predicate, scope.subject, query):
        if value is not None:
            _query_value(value)


def summarize_matches(rows, operation, limit):
    count = 0
    values = set()
    evidence = []
    for fact in rows:
        count += 1
        if operation == 'count_distinct':
            values.add(object_key(fact))
        if len(evidence) < limit:
            evidence.append(fact)
    return AggregateResult(count if operation == 'count' else len(values), tuple(evidence))


def validate_path(anchor, predicates, scope):
    _query_value(anchor)
    if not isinstance(predicates, (tuple, list)) or not 2 <= len(predicates) <= 4:
        raise ValueError('Expected two to four predicates')
    if not isinstance(scope, FactQuery) or scope.after_id is not None:
        raise ValueError('Path traversal requires fact conditions and accepts no cursor')
    for predicate in predicates:
        _query_value(predicate)
    return tuple(predicate.lower() for predicate in predicates)


def traverse(anchor, predicates, neighbors, limit):
    frontier = [(anchor, ())]
    truncated = False
    for predicate in predicates:
        expanded = []
        for entity, path in frontier:
            for target, fact in neighbors(entity, predicate):
                expanded.append((target, (*path, fact)))
                if len(expanded) > limit:
                    break
            if len(expanded) > limit:
                break
        truncated |= len(expanded) > limit
        frontier = expanded[:limit]
        if not frontier:
            break
    return frontier, ('path_budget',) if truncated else ()
