"""从召回起点按层扩展可见的实体关系。"""

from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from ..domain.ids import EntityId, FactId
from ..domain.values import Predicate
from ..ports.queries import FactFilter
from ..ports.storage import GraphEdge, SpaceReader

DEFAULT_GRAPH_HOPS = 2
MAX_GRAPH_HOPS = 4
MAX_EXPLICIT_ANCHORS = 32
MAX_GRAPH_PREDICATES = 32
_MAX_EDGES = 200


@dataclass(frozen=True)
class GraphRecallOptions:
    """开启图召回并限定起点、谓词及最大跳数。

    Attributes:
        origin_entity_ids: 按指定顺序使用空间中存在的遍历起点，最多 32 个。
            None 表示自动选取，空序列表示无起点。
        predicates: 每跳允许的规范谓词，最多 32 个。None 表示不限。
        max_hops: 最大跳数，范围为 1 至 4，默认为 2。

    Raises:
        ValueError: 参数越界或包含无效的领域值。
    """

    origin_entity_ids: Sequence[EntityId] | None = None
    predicates: Sequence[Predicate] | None = None
    max_hops: int = DEFAULT_GRAPH_HOPS

    def __post_init__(self) -> None:
        if type(self.max_hops) is not int or not 1 <= self.max_hops <= MAX_GRAPH_HOPS:
            raise ValueError(f"max_hops 须为 1 至 {MAX_GRAPH_HOPS} 的整数")
        if self.origin_entity_ids is not None:
            object.__setattr__(
                self,
                "origin_entity_ids",
                _validated_values(
                    self.origin_entity_ids,
                    EntityId,
                    MAX_EXPLICIT_ANCHORS,
                    "origin_entity_ids",
                ),
            )
        if self.predicates is not None:
            predicates = _validated_values(
                self.predicates, Predicate, MAX_GRAPH_PREDICATES, "predicates"
            )
            if not predicates:
                raise ValueError("predicates 须为非空序列，省略时不限谓词")
            object.__setattr__(self, "predicates", predicates)


def _validated_values[T: EntityId | Predicate](
    values: Sequence[T], item_type: type[T], limit: int, field: str
) -> tuple[T, ...]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes, bytearray))
        or len(values) > limit
        or any(not isinstance(item, item_type) for item in values)
    ):
        raise ValueError(f"{field} 须为最多 {limit} 个 {item_type.__name__} 的序列")
    try:
        if any(b"\x00" in item.value.encode("utf-8") for item in values):
            raise ValueError(f"{field} 不能含空字符")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} 须能编码为 UTF-8") from exc
    return tuple(dict.fromkeys(values))


async def graph_ranking(
    reader: SpaceReader,
    options: GraphRecallOptions,
    lexical: Sequence[FactId],
    semantic: Sequence[FactId],
    *,
    filters: FactFilter,
    reference_time: float,
    top_k: int,
) -> tuple[FactId, ...]:
    """在普通召回的读快照中生成图候选排名。

    Args:
        reader: 已授权且正在用于读取候选的会话。
        options: 已校验的图召回参数。
        lexical: 词法候选事实标识，按该路排名排列。
        semantic: 向量候选事实标识，按该路排名排列。
        filters: 本次召回统一使用的时间和可见性条件。
        reference_time: 本次请求共用的 UTC epoch 秒。
        top_k: 最终证据上限，用于确定全局边预算。

    Returns:
        按跳数、主体发现顺序和事实标识排序的候选事实标识。
    """
    origins = await _graph_origins(
        reader, options, lexical, semantic, exclude_sensitive=filters.exclude_sensitive
    )
    ranking = [
        edge.fact_id
        async for edges in _graph_layers(
            reader,
            options,
            tuple(origins),
            filters=filters,
            reference_time=reference_time,
            budget=_edge_budget(top_k),
        )
        for edge in edges
    ]
    return tuple(ranking)


def _edge_budget(top_k: int) -> int:
    return min(8 * top_k, _MAX_EDGES)


async def _graph_layers(
    reader: SpaceReader,
    options: GraphRecallOptions,
    frontier: tuple[EntityId, ...],
    *,
    filters: FactFilter,
    reference_time: float,
    budget: int,
) -> AsyncIterator[Sequence[GraphEdge]]:

    visited = set(frontier)
    for _ in range(options.max_hops):
        if not frontier or budget == 0:
            break
        edges = await reader.outgoing_edges(
            frontier,
            filters=filters,
            reference_time=reference_time,
            limit=budget,
            predicates=options.predicates or (),
        )
        budget -= len(edges)
        yield edges
        next_frontier = []
        for edge in edges:
            if edge.object_entity_id not in visited:
                visited.add(edge.object_entity_id)
                next_frontier.append(edge.object_entity_id)
        frontier = tuple(next_frontier)


async def _graph_origins(
    reader: SpaceReader,
    options: GraphRecallOptions,
    lexical: Sequence[FactId],
    semantic: Sequence[FactId],
    *,
    exclude_sensitive: bool,
) -> dict[EntityId, FactId | None]:
    if options.origin_entity_ids is not None:
        if not options.origin_entity_ids:
            return {}
        entities = await reader.get_entities(options.origin_entity_ids)
        return {
            entity_id: None
            for entity_id in options.origin_entity_ids
            if entity_id in entities
        }
    ids = tuple(dict.fromkeys((*lexical, *semantic)))
    if not ids:
        return {}
    edges = await reader.get_relation_edges(ids, exclude_sensitive=exclude_sensitive)
    routes = deque(
        (edges[fact_id] for fact_id in ranked_ids if fact_id in edges)
        for ranked_ids in (lexical, semantic)
    )
    ordered: dict[EntityId, FactId | None] = {}
    while routes:
        route = routes.popleft()
        for edge in route:
            if edge.subject_id not in ordered:
                ordered[edge.subject_id] = edge.fact_id
                routes.append(route)
                break
    return ordered
