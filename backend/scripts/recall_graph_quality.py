"""对照图路召回、种子边计分和出边查询成本。"""

from collections import defaultdict
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from typing import cast

import psycopg
from psycopg import sql
from recall_graph_cases import DATASETS, GraphDataset, GraphQueryCase, stress_dataset

from mnemotrace_next import GraphRecallOptions, Memory
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.reader import _outgoing_edge_query
from mnemotrace_next.application.graph_recall import (
    _MAX_EDGES,
    _edge_budget,
    _graph_layers,
    _graph_origins,
)
from mnemotrace_next.application.recall import (
    _RRF_K,
    _VECTOR_MIN_COSINE,
    _candidate_limit,
    _fuse_rankings,
)
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import EntityId, FactId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, Predicate
from mnemotrace_next.ports.inference import Embedder
from mnemotrace_next.ports.queries import FactFilter
from mnemotrace_next.ports.storage import GraphEdge, SpaceReader

_TOP_KS = (1, 5)
_PLAN_SAMPLES = 12
_NOW = 100.0


class _FixedClock:
    def now(self) -> float:
        return _NOW


@dataclass(frozen=True)
class _QueryEmbedder:
    embedding_space: str
    dim: int
    vector: Sequence[float]

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return (self.vector,)


@dataclass(frozen=True)
class _Expansion:
    layers: tuple[Sequence[GraphEdge], ...]
    elapsed_ms: float
    budget: int

    @property
    def ids(self) -> tuple[FactId, ...]:
        return tuple(edge.fact_id for layer in self.layers for edge in layer)

    def report(self, target: FactId | None) -> dict[str, object]:
        return {
            "edge_budget": self.budget,
            "budget_consumed": len(self.ids) == self.budget,
            "attempted_hops": len(self.layers),
            "depth_reached": sum(bool(layer) for layer in self.layers),
            "edges_per_hop": [len(layer) for layer in self.layers],
            "first_hop_subjects_with_edges": len(
                {edge.subject_id for edge in self.layers[0]}
            )
            if self.layers
            else 0,
            "hops": [[edge.fact_id.value for edge in layer] for layer in self.layers],
            "target_hop": next(
                (
                    hop
                    for hop, layer in enumerate(self.layers, 1)
                    if any(edge.fact_id == target for edge in layer)
                ),
                None,
            ),
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


@dataclass(frozen=True)
class _Trace:
    case: GraphQueryCase
    lexical: tuple[FactId, ...]
    semantic: tuple[FactId, ...]
    origins: dict[EntityId, FactId | None]
    expansions: dict[str, _Expansion]
    rankings: dict[str, list[FactId]]
    public_ms: dict[str, float]

    def report(self, top_k: int) -> dict[str, object]:
        target = FactId(self.case.expected) if self.case.expected is not None else None
        return {
            "id": self.case.id,
            "kind": self.case.kind,
            "query": self.case.text,
            "expected": self.case.expected,
            "as_of": self.case.as_of,
            "explicit_origins": self.case.entity_names,
            "predicates": self.case.predicates,
            "lexical": [hit.value for hit in self.lexical],
            "semantic": [hit.value for hit in self.semantic],
            "origins": [
                {
                    "entity_id": entity.value,
                    "seed_fact_id": seed.value if seed else None,
                }
                for entity, seed in self.origins.items()
            ],
            "expansions": {
                name: expansion.report(target)
                for name, expansion in self.expansions.items()
            },
            "policies": {
                name: {
                    "returned": [hit.value for hit in ranking[:top_k]],
                    "expected_rank": ranking.index(target) + 1
                    if target is not None and target in ranking
                    else None,
                }
                for name, ranking in self.rankings.items()
            },
            "public_elapsed_ms_without_embedding": self.public_ms,
        }


async def _seed_dataset(
    storage: PostgresStorage, embedder: Embedder, dataset: GraphDataset
) -> SpaceId:
    space_id = SpaceId(dataset.name)
    entities = {
        name: Entity(space_id, name)
        for name in dict.fromkeys(
            name for fact in dataset.facts for name in (fact.subject, fact.target)
        )
    }
    facts = tuple(
        Fact(
            id=FactId(case.id),
            space_id=space_id,
            subject=EntityRef(entities[case.subject].id),
            predicate=Predicate(case.predicate),
            object=EntityRef(entities[case.target].id),
            text=case.text,
            valid_at=case.valid_at,
            invalid_at=case.invalid_at,
            recorded_at=10.0,
            expired_at=_NOW if case.invalid_at is not None else None,
            provenance=(),
            labels=(),
            sensitive=False,
            source=Source.MANUAL,
        )
        for case in dataset.facts
    )
    indexed = tuple(
        fact for case, fact in zip(dataset.facts, facts, strict=True) if case.indexed
    )
    vectors = (
        await embedder.embed(tuple(fact.text for fact in indexed)) if indexed else ()
    )
    await storage.create_space(Space(space_id, "recall-quality", dataset.name, 0.0))
    async with storage.session(space_id, write=True) as writer:
        for entity in entities.values():
            await writer.upsert_entity(entity)
        await writer.save_facts(facts, changed_at=_NOW)
        if indexed:
            await writer.upsert_vectors(
                tuple(
                    (fact.id, vector)
                    for fact, vector in zip(indexed, vectors, strict=True)
                ),
                embedding_space=embedder.embedding_space,
            )
    return space_id


async def _expand(
    reader: SpaceReader,
    options: GraphRecallOptions,
    origins: tuple[EntityId, ...],
    filters: FactFilter,
    budget: int,
) -> _Expansion:
    started = perf_counter()
    layers = [
        layer
        async for layer in _graph_layers(
            reader,
            options,
            origins,
            filters=filters,
            reference_time=_NOW,
            budget=budget,
        )
    ]
    return _Expansion(tuple(layers), (perf_counter() - started) * 1000, budget)


def _rank_without_seed_votes(
    lexical: Sequence[FactId],
    semantic: Sequence[FactId],
    graph: Sequence[FactId],
    seeds: AbstractSet[FactId],
) -> list[FactId]:
    # 先确定原名次，跳过种子边图路分时保留后续边的相对得分。
    votes = (
        enumerate(lexical, start=1),
        enumerate(semantic, start=1),
        ((rank, hit) for rank, hit in enumerate(graph, start=1) if hit not in seeds),
    )
    scores: dict[FactId, float] = defaultdict(float)
    for lane in votes:
        for rank, hit in lane:
            scores[hit] += 1 / (_RRF_K + rank)
    return sorted(scores, key=lambda hit: (-scores[hit], hit.value))


async def _trace_case(
    storage: PostgresStorage,
    space: SpaceId,
    case: GraphQueryCase,
    embedder: _QueryEmbedder,
    top_k: int,
) -> _Trace:
    options = GraphRecallOptions(
        origin_entity_ids=tuple(Entity(space, name).id for name in case.entity_names)
        if case.entity_names is not None
        else None,
        predicates=tuple(Predicate(name) for name in case.predicates) or None,
    )
    filters = FactFilter(valid_at=case.as_of)
    limit = _candidate_limit(top_k)
    async with storage.session(space) as reader:
        lexical = tuple(
            hit
            for hit, _ in await reader.search_lexical(
                case.text,
                limit,
                filters=filters,
                reference_time=_NOW,
            )
        )
        semantic = tuple(
            hit
            for hit, _ in await reader.search_vectors(
                embedder.vector,
                limit,
                filters=filters,
                reference_time=_NOW,
                embedding_space=embedder.embedding_space,
                min_score=_VECTOR_MIN_COSINE,
            )
        )
        origins = await _graph_origins(
            reader,
            options,
            lexical,
            semantic,
            exclude_sensitive=filters.exclude_sensitive,
        )
        expansions = {
            name: await _expand(reader, options, tuple(origins), filters, budget)
            for name, budget in (
                ("default", _edge_budget(top_k)),
                ("budget_4x_top_k", min(4 * top_k, _MAX_EDGES)),
                ("budget_16x_top_k", min(16 * top_k, _MAX_EDGES)),
            )
        }

    seeds = frozenset(seed for seed in origins.values() if seed is not None)
    graph_ids = expansions["default"].ids
    rankings = {
        "graph_off": _fuse_rankings(lexical, semantic),
        "graph_on": _fuse_rankings(lexical, semantic, graph_ids),
        "no_seed_vote": _rank_without_seed_votes(lexical, semantic, graph_ids, seeds),
        **{
            name: _fuse_rankings(lexical, semantic, expansion.ids)
            for name, expansion in expansions.items()
            if name != "default"
        },
    }
    recall = Memory.for_recall(
        space_id=space, storage=storage, embedder=embedder, clock=_FixedClock()
    )
    elapsed = {}
    for name, graph_options in (("graph_off", None), ("graph_on", options)):
        started = perf_counter()
        result = await recall.recall(
            case.text, top_k=top_k, as_of=case.as_of, graph=graph_options
        )
        elapsed[name] = round((perf_counter() - started) * 1000, 3)
        if (
            result.degraded_reason
            or [item.fact_id for item in result.evidence] != rankings[name][:top_k]
        ):
            raise RuntimeError(f"{space.value}/{case.id}/{name} 与公开召回结果不一致")
    return _Trace(case, lexical, semantic, origins, expansions, rankings, elapsed)


def _metrics(traces: Sequence[_Trace], top_k: int) -> dict[str, object]:
    groups = {
        kind: [trace for trace in traces if trace.case.kind == kind]
        for kind in ("one_hop", "two_hop", "no_answer")
    }
    return {
        name: {
            kind: {
                "queries": len(group),
                "target_in_candidates": sum(
                    trace.case.expected in [hit.value for hit in trace.rankings[name]]
                    for trace in group
                ),
                "hit_at_1": sum(
                    trace.case.expected
                    in [hit.value for hit in trace.rankings[name][:1]]
                    for trace in group
                ),
                "hit_at_k": sum(
                    trace.case.expected
                    in [hit.value for hit in trace.rankings[name][:top_k]]
                    for trace in group
                ),
                "graph_only_hit_at_k": sum(
                    trace.case.expected
                    in [hit.value for hit in trace.rankings[name][:top_k]]
                    and trace.case.expected
                    not in [hit.value for hit in trace.rankings["graph_off"]]
                    for trace in group
                ),
                "no_answer_nonempty": sum(
                    trace.case.expected is None and bool(trace.rankings[name])
                    for trace in group
                ),
            }
            for kind, group in groups.items()
        }
        for name in traces[0].rankings
    }


def _latency(samples: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        name: round(ordered[ceil(fraction * len(ordered)) - 1], 3)
        for name, fraction in (("p50", 0.5), ("p95", 0.95))
    }


async def _plan_samples(
    conn: psycopg.AsyncConnection[psycopg.rows.TupleRow],
    space: SpaceId,
    origins: tuple[EntityId, ...],
    top_k: int,
) -> dict[str, object]:
    query, params = _outgoing_edge_query(
        space,
        origins,
        filters=FactFilter(),
        reference_time=_NOW,
        limit=_edge_budget(top_k),
        predicates=(),
    )
    explain = sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ") + query
    times = []
    plan: dict[str, object] = {}
    for sample in range(_PLAN_SAMPLES + 1):
        cursor = await conn.execute(explain, params)
        row = await cursor.fetchone()
        assert row is not None
        (plan,) = cast(list[dict[str, object]], row[0])
        if sample:
            times.append(cast(float, plan["Execution Time"]))
    return {
        "origins": len(origins),
        "execution_ms": _latency(times),
        "execution_ms_samples": times,
        "plan": plan,
    }


async def run_graph_suite(
    storage: PostgresStorage, embedder: Embedder, dsn: str
) -> dict[str, object]:
    """对照自动图扩展的质量和出边查询计划。

    Args:
        storage: 绑定合成评测账号的 PostgreSQL 存储。
        embedder: 用于生成事实和查询向量的实际嵌入端口。
        dsn: 本次独立临时数据库连接串。

    Returns:
        含完整逐题结果、预算对照和高出度计划的报告。
    """
    reports: dict[str, object] = {}
    stress_reports: dict[str, object] = {}
    stress = ((12, 12), (2048, 31))
    stress_data = tuple(stress_dataset(fanout, peers) for fanout, peers in stress)
    datasets = (*DATASETS, *stress_data)
    for dataset in datasets:
        await _seed_dataset(storage, embedder, dataset)

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            "ANALYZE mnemotrace_next.facts, mnemotrace_next.fact_vectors"
        )
        for dataset in datasets:
            space = SpaceId(dataset.name)
            vectors = await embedder.embed(tuple(case.text for case in dataset.queries))
            top_reports: dict[str, object] = {}
            for top_k in _TOP_KS:
                traces = [
                    await _trace_case(
                        storage,
                        space,
                        case,
                        _QueryEmbedder(embedder.embedding_space, embedder.dim, vector),
                        top_k,
                    )
                    for case, vector in zip(dataset.queries, vectors, strict=True)
                ]
                if dataset in stress_data:
                    (trace,) = traces
                    origins = tuple(trace.origins)
                    top_reports[str(top_k)] = {
                        "query": trace.report(top_k),
                        "first_hop_plans": {
                            "all_origins": await _plan_samples(
                                conn, space, origins, top_k
                            ),
                            "first_two_origins": await _plan_samples(
                                conn, space, origins[:2], top_k
                            ),
                        },
                    }
                else:
                    top_reports[str(top_k)] = {
                        "metrics": _metrics(traces, top_k),
                        "public_latency_ms_without_embedding": {
                            name: _latency([trace.public_ms[name] for trace in traces])
                            for name in ("graph_off", "graph_on")
                        },
                        "queries": [trace.report(top_k) for trace in traces],
                    }
            destination = stress_reports if dataset in stress_data else reports
            destination[dataset.name] = {
                "facts": len(dataset.facts),
                "indexed_facts": sum(case.indexed for case in dataset.facts),
                "top_k": top_reports,
            }
    return {
        "datasets": reports,
        "stress": stress_reports,
        "plan_sample_count": _PLAN_SAMPLES,
    }
