"""Validation and execution shared by explicit memory queries."""
from dataclasses import dataclass

from ..consolidate.fact_rules import object_key
from ..store.contracts import FactQuery, _id, _time
from ..types import Fact
from ..util import name_key
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
    """校验显式检索的字符串值。

    Args:
        value: 要校验的筛选值。

    Raises:
        ValueError: 值格式无效或含空字符。
    """
    _id(value)
    if '\x00' in value:
        raise ValueError('Unexpected NUL in query input')


def validate_aggregate(operation, scope, start, end, query):
    """校验聚合方式、筛选条件和时间范围。

    Args:
        operation: 计数方式。
        scope: 包含谓词的事实筛选条件。
        start: 可选的有效时间下界。
        end: 可选的有效时间上界。
        query: 可选的摘要回退查询。

    Raises:
        ValueError: 参数无效，或时间范围为空或反向。
    """
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
    """统计全部匹配事实，保留限定数量的证据。

    Args:
        rows: 符合条件的事实。
        operation: 统计事实条数或不同对象值的数量。
        limit: 返回证据的数量上限。

    Returns:
        计数和用于说明结果的事实。
    """
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
    """校验显式路径参数并规范化谓词。

    Args:
        anchor: 起始实体名称。
        predicates: 两到四个按顺序排列的谓词。
        scope: 关联事实的筛选条件。

    Returns:
        规范化的谓词序列。

    Raises:
        ValueError: 锚点、谓词序列或筛选条件无效。
    """
    _query_value(anchor)
    if not isinstance(predicates, (tuple, list)) or not 2 <= len(predicates) <= 4:
        raise ValueError('Expected two to four predicates')
    if not isinstance(scope, FactQuery) or scope.after_id is not None:
        raise ValueError('Path traversal requires fact conditions and accepts no cursor')
    for predicate in predicates:
        _query_value(predicate)
    return tuple(name_key(predicate) for predicate in predicates)


def traverse(anchor, predicates, neighbors, limit):
    """逐谓词扩展关系路径，限制每一跳的候选数量。

    Args:
        anchor: 起始实体 ID。
        predicates: 按顺序匹配的谓词。
        neighbors: 返回邻居实体与支撑事实的函数。
        limit: 每一跳保留的候选路径上限。

    Returns:
        匹配的终点与事实路径，以及预算限制。
    """
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
