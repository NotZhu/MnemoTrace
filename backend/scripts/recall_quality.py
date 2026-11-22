"""用独立 PostgreSQL 和实际嵌入对照召回质量。"""

import argparse
import asyncio
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import ClassVar, Literal
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool
from recall_graph_quality import run_graph_suite
from recall_quality_cases import (
    DATASETS,
    QUALITY_NOW,
    RECENCY_DATASET,
    Dataset,
    FactCase,
    QueryCase,
)

from mnemotrace_next import Memory
from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.migrate import apply_migrations
from mnemotrace_next.application.recall import (
    _VECTOR_MIN_COSINE,
    _candidate_limit,
    _fuse_rankings,
    _recency_candidates,
    _rrf_scores,
)
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace_next.ports.inference import Embedder
from mnemotrace_next.ports.queries import FactFilter
from mnemotrace_next.ports.storage import Storage

_TOP_K = 5
_MAX_TESTED_DEPTH = _candidate_limit(100)
_NOW = QUALITY_NOW
_DEFAULT_LIMIT = _candidate_limit(_TOP_K)
type Hits = Sequence[tuple[FactId, float]]
type _Suite = Literal["ordinary", "graph", "recency"]


@dataclass(frozen=True)
class _Policy:
    name: str
    channel: Literal["lexical", "vector", "rrf"] = "rrf"
    limit: int = _DEFAULT_LIMIT
    min_cosine: float = _VECTOR_MIN_COSINE
    lexical_min_score: float = 0.0

    def rank(self, lexical: Hits, semantic: Hits) -> list[FactId]:
        lexical = [hit for hit in lexical if hit[1] >= self.lexical_min_score][
            : self.limit
        ]
        semantic = [hit for hit in semantic if hit[1] >= self.min_cosine][: self.limit]
        if self.channel == "lexical":
            return [fact_id for fact_id, _ in lexical]
        if self.channel == "vector":
            return [fact_id for fact_id, _ in semantic]
        return _fuse_rankings(
            (fact_id for fact_id, _ in lexical), (fact_id for fact_id, _ in semantic)
        )


_BASELINE = _Policy("rrf")
_POLICIES = (
    _Policy("lexical", "lexical"),
    _Policy("vector", "vector"),
    _BASELINE,
    *(
        _Policy(f"rrf-depth-{limit}", limit=limit)
        for limit in (16, 64, _MAX_TESTED_DEPTH)
    ),
    *(
        _Policy(f"rrf-cosine-{score}", min_cosine=score)
        for score in (0.3, 0.35, 0.4, 0.45, 0.55, 0.6, 0.65, 0.7, 0.75)
    ),
    *(
        _Policy(f"rrf-lexical-{score}", lexical_min_score=score)
        for score in (0.005, 0.01, 0.02, 0.03)
    ),
)


class _FixedClock:
    def now(self) -> float:
        return _NOW


class _MeasuredEmbedder:
    """记录公开召回入口实际使用的查询向量及嵌入时间。"""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        self.embedding_space = embedder.embedding_space
        self.dim = embedder.dim
        self.vector: Sequence[float] = ()
        self.elapsed_ms = 0.0

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        started = perf_counter()
        result = await self._embedder.embed(texts)
        self.elapsed_ms = (perf_counter() - started) * 1000
        (self.vector,) = result
        return result


class _RecencyEmbedder:
    """固定主题向量，使时间排序的对照可以离线复现。"""

    embedding_space = "recall-quality-recency-v1"
    dim = 3
    _vectors: ClassVar[dict[str, tuple[float, ...]]] = {
        "rollouttag": (1.0, 0.0, 0.0),
        "policytag": (0.0, 1.0, 0.0),
        "historytag": (0.0, 0.0, 1.0),
    }

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return tuple(self._vectors[text.split(maxsplit=1)[0]] for text in texts)


@dataclass(frozen=True)
class _FixedQueryEmbedder:
    """为同一查询的两次公开召回复用预先生成的向量。"""

    embedding_space: str
    dim: int
    vector: tuple[float, ...]

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return tuple(self.vector for _ in texts)


@dataclass(frozen=True)
class _Trace:
    case: QueryCase
    lexical: Hits
    semantic: Hits
    elapsed_ms: dict[str, float]


@contextmanager
def _quality_database(admin_dsn: str) -> Iterator[str]:
    database = f"mnemo_recall_quality_{uuid4().hex}"
    identifier = sql.Identifier(database)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                "LOCALE_PROVIDER libc LOCALE 'C'"
            ).format(identifier)
        )
        try:
            dsn = make_conninfo(admin_dsn, dbname=database)
            apply_migrations(dsn)
            yield dsn
        finally:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier))


def _make_fact(case: FactCase, subject: Entity) -> Fact:
    return Fact(
        id=FactId(case.id),
        space_id=subject.space_id,
        subject=EntityRef(subject.id),
        predicate=Predicate("record"),
        object=LiteralValue(case.text),
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


async def _seed_dataset(
    storage: Storage, embedder: Embedder, dataset: Dataset
) -> SpaceId:
    scope = Space(SpaceId(dataset.name), "recall-quality", dataset.name, 0.0)
    subject = Entity(scope.id, "合成评测")
    facts = tuple(_make_fact(case, subject) for case in dataset.facts)
    indexed = tuple(
        fact for fact, case in zip(facts, dataset.facts, strict=True) if case.indexed
    )
    await storage.create_space(scope)
    vectors = await embedder.embed(tuple(fact.text for fact in indexed))
    async with storage.session(scope.id, write=True) as writer:
        await writer.upsert_entity(subject)
        await writer.save_facts(facts, changed_at=_NOW)
        await writer.upsert_vectors(
            tuple(
                (fact.id, vector) for fact, vector in zip(indexed, vectors, strict=True)
            ),
            embedding_space=embedder.embedding_space,
        )
    return scope.id


async def _run_dataset(
    storage: PostgresStorage, embedder: Embedder, dataset: Dataset
) -> list[_Trace]:
    space_id = await _seed_dataset(storage, embedder, dataset)

    measured = _MeasuredEmbedder(embedder)
    memory = Memory.for_recall(
        space_id=space_id, storage=storage, embedder=measured, clock=_FixedClock()
    )
    traces = []
    for case in dataset.queries:
        started = perf_counter()
        result = await memory.recall(case.text, top_k=_TOP_K, as_of=case.as_of)
        recall_ms = (perf_counter() - started) * 1000
        if result.degraded_reason is not None:
            raise RuntimeError(f"{dataset.name}/{case.id} 发生降级，无法标定双路质量")

        filters = FactFilter(valid_at=case.as_of)
        async with storage.session(space_id) as reader:
            started = perf_counter()
            lexical = await reader.search_lexical(
                case.text, len(dataset.facts), filters=filters, reference_time=_NOW
            )
            lexical_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            semantic = await reader.search_vectors(
                measured.vector,
                len(dataset.facts),
                filters=filters,
                reference_time=_NOW,
                embedding_space=measured.embedding_space,
            )
            vector_ms = (perf_counter() - started) * 1000
        expected = _BASELINE.rank(lexical, semantic)[:_TOP_K]
        if [item.fact_id for item in result.evidence] != expected:
            raise RuntimeError(f"{dataset.name}/{case.id} 的对照与公开召回排序不一致")
        traces.append(
            _Trace(
                case,
                lexical,
                semantic,
                {
                    "recall": recall_ms,
                    "embedding": measured.elapsed_ms,
                    "lexical_probe": lexical_ms,
                    "vector_probe": vector_ms,
                },
            )
        )
    return traces


def _measure_policy(policy: _Policy, traces: Sequence[_Trace]) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    positives = negatives = candidates = top1 = topk = false_recalls = 0
    for trace in traces:
        ranked = policy.rank(trace.lexical, trace.semantic)
        top = ranked[:_TOP_K]
        expected = trace.case.expected
        rank = next(
            (rank for rank, hit in enumerate(ranked, 1) if hit.value == expected), None
        )
        if expected is not None:
            positives += 1
            if rank is not None:
                candidates += 1
                top1 += rank == 1
                topk += rank <= _TOP_K
        else:
            negatives += 1
            false_recalls += bool(top)
        rows.append(
            {
                "id": trace.case.id,
                "expected_rank": rank,
                "returned": [hit.value for hit in top],
            }
        )
    return {
        "positive_queries": positives,
        "no_answer_queries": negatives,
        "expected_in_candidates": candidates,
        "hit_at_1": top1,
        "hit_at_k": topk,
        "no_answer_nonempty": false_recalls,
        "queries": rows,
    }


def _report_dataset(dataset: Dataset, traces: Sequence[_Trace]) -> dict[str, object]:
    latency = {}
    for stage in traces[0].elapsed_ms:
        samples = sorted(trace.elapsed_ms[stage] for trace in traces)
        latency[stage] = {
            "p50": round(samples[ceil(0.50 * len(samples)) - 1], 3),
            "p95": round(samples[ceil(0.95 * len(samples)) - 1], 3),
        }
    return {
        "fact_count": len(dataset.facts),
        "indexed_fact_count": sum(fact.indexed for fact in dataset.facts),
        "latency_ms": latency,
        "policies": {
            policy.name: _measure_policy(policy, traces) for policy in _POLICIES
        },
        "queries": [
            {
                "id": trace.case.id,
                "kind": trace.case.kind,
                "query": trace.case.text,
                "expected": trace.case.expected,
                "as_of": trace.case.as_of,
                "lexical": [(hit.value, score) for hit, score in trace.lexical],
                "semantic": [(hit.value, score) for hit, score in trace.semantic],
                "elapsed_ms": trace.elapsed_ms,
            }
            for trace in traces
        ],
    }


@dataclass(frozen=True)
class _RecencyTrial:
    expected_rank: int | None
    loaded_count: int
    elapsed_ms: float


async def _run_recency_suite(storage: Storage, embedder: Embedder) -> dict[str, object]:
    space_id = await _seed_dataset(storage, embedder, RECENCY_DATASET)
    trials: dict[str, list[_RecencyTrial]] = {"rrf": [], "prefer_recent": []}
    queries = []
    candidate_hits = 0
    recent_improved = False
    controls_passed = True
    for case in RECENCY_DATASET.queries:
        expected = FactId(case.expected) if case.expected is not None else None
        (vector,) = await embedder.embed((case.text,))
        query_embedder = _FixedQueryEmbedder(
            embedding_space=embedder.embedding_space,
            dim=embedder.dim,
            vector=tuple(vector),
        )
        memory = Memory.for_recall(
            space_id=space_id,
            storage=storage,
            embedder=query_embedder,
            clock=_FixedClock(),
        )
        filters = FactFilter(valid_at=case.as_of)
        async with storage.session(space_id) as reader:
            lexical = await reader.search_lexical(
                case.text, _DEFAULT_LIMIT, filters=filters, reference_time=_NOW
            )
            semantic = await reader.search_vectors(
                query_embedder.vector,
                _DEFAULT_LIMIT,
                filters=filters,
                reference_time=_NOW,
                embedding_space=query_embedder.embedding_space,
                min_score=_VECTOR_MIN_COSINE,
            )
        ranking = _rrf_scores(
            (fact_id for fact_id, _ in lexical), (fact_id for fact_id, _ in semantic)
        )
        candidate_hits += any(fact_id == expected for fact_id, _ in ranking)
        runs = {}
        for policy, prefer_recent in (("rrf", False), ("prefer_recent", True)):
            started = perf_counter()
            result = await memory.recall(
                case.text, top_k=_TOP_K, as_of=case.as_of, prefer_recent=prefer_recent
            )
            elapsed_ms = round((perf_counter() - started) * 1000, 3)
            if result.degraded_reason is not None:
                raise RuntimeError(f"{case.id} 发生推理降级，无法对照新近度")
            returned = [item.fact_id for item in result.evidence]
            if not prefer_recent and returned != [hit[0] for hit in ranking[:_TOP_K]]:
                raise RuntimeError(f"{case.id} 的基线与公开召回排序不一致")
            # 从相同条件下的候选重放批量读取范围，耗时来自公开召回调用。
            loaded = (
                _recency_candidates(ranking, _TOP_K)
                if prefer_recent and case.as_of is None
                else ranking[:_TOP_K]
            )
            rank = next(
                (i for i, fact_id in enumerate(returned, 1) if fact_id == expected),
                None,
            )
            trials[policy].append(_RecencyTrial(rank, len(loaded), elapsed_ms))
            runs[policy] = {
                "expected_rank": rank,
                "returned": [fact_id.value for fact_id in returned],
                "loaded_count": len(loaded),
                "elapsed_ms": elapsed_ms,
            }
        before = trials["rrf"][-1].expected_rank
        after = trials["prefer_recent"][-1].expected_rank
        if case.kind == "recent":
            recent_improved |= after is not None and (before is None or after < before)
        else:
            controls_passed &= before is not None and after is not None
        if case.as_of is not None:
            controls_passed &= (
                runs["rrf"]["returned"] == runs["prefer_recent"]["returned"]
            )
        queries.append(
            {
                **case._asdict(),
                "rrf_candidates": [
                    (fact_id.value, score) for fact_id, score in ranking
                ],
                "runs": runs,
            }
        )
    return {
        "reference_time": _NOW,
        "top_k": _TOP_K,
        "expected_in_candidates": candidate_hits,
        "recent_improved": recent_improved,
        "controls_passed": controls_passed,
        "passed": recent_improved and controls_passed,
        "facts": [case._asdict() for case in RECENCY_DATASET.facts],
        "policies": {
            policy: {
                "hit_at_1": sum(trial.expected_rank == 1 for trial in runs),
                "hit_at_k": sum(trial.expected_rank is not None for trial in runs),
                "total_loaded": sum(trial.loaded_count for trial in runs),
                "max_latency_ms": max(trial.elapsed_ms for trial in runs),
            }
            for policy, runs in trials.items()
        },
        "queries": queries,
    }


async def evaluate(
    admin_dsn: str,
    embedder: Embedder,
    *,
    model: str,
    revision: str,
    out: Path,
    suite: _Suite = "ordinary",
) -> None:
    """在独立临时库运行评测并记录模型身份、逐题对照和耗时。

    Args:
        admin_dsn: 专用测试实例上的建库连接串。
        embedder: 实际负责生成查询及事实向量的嵌入端口。
        model: 被测模型标识。
        revision: 被测模型的固定修订。
        out: 保存完整结果的新文件路径。
        suite: 对照普通召回、图路召回或按需新近度。
    """
    with _quality_database(admin_dsn) as dsn:
        with psycopg.connect(dsn) as conn:
            db = conn.execute(
                "SELECT current_setting('server_version'), extversion "
                "FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            assert db is not None
        async with AsyncConnectionPool(dsn, open=False, min_size=0, max_size=2) as pool:
            storage = PostgresStorage(pool, owner_id="recall-quality")
            if suite == "graph":
                details = await run_graph_suite(storage, embedder, dsn)
            elif suite == "recency":
                details = await _run_recency_suite(storage, embedder)
            else:
                details = {
                    "top_k": _TOP_K,
                    "default_candidate_limit": _DEFAULT_LIMIT,
                    "max_tested_depth": _MAX_TESTED_DEPTH,
                    "default_min_cosine": _VECTOR_MIN_COSINE,
                    "datasets": {
                        dataset.name: _report_dataset(
                            dataset, await _run_dataset(storage, embedder, dataset)
                        )
                        for dataset in DATASETS
                    },
                }
    with out.open("x", encoding="utf-8") as report:
        json.dump(
            {
                "model": model,
                "revision": revision,
                "embedding_space": embedder.embedding_space,
                "dimension": embedder.dim,
                "postgres_version": db[0],
                "pgvector_version": db[1],
                **details,
            },
            report,
            indent=2,
            ensure_ascii=False,
        )
        report.write("\n")
    if suite == "recency" and not details["passed"]:
        raise RuntimeError("新近度对照未通过，详见输出报告")


async def _configured_run(dsn: str, out: Path, suite: _Suite) -> None:
    if suite == "recency":
        await evaluate(
            dsn,
            _RecencyEmbedder(),
            model="synthetic-topic",
            revision="v1",
            out=out,
            suite=suite,
        )
        return
    async with open_inference(os.environ, embed=True) as clients:
        if clients.embedder is None:
            raise RuntimeError("缺少嵌入端口")
        await evaluate(
            dsn,
            clients.embedder,
            model=os.environ["MNEMOTRACE_NEXT_EMBED_MODEL"],
            revision=os.environ["MNEMOTRACE_NEXT_EMBED_REVISION"],
            out=out,
            suite=suite,
        )


def main() -> None:
    """使用显式配置的测试实例运行所选召回评测。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("ordinary", "graph", "recency"),
        default="ordinary",
        help="对照普通召回、图路召回或按需新近度",
    )
    parser.add_argument("--out", required=True, type=Path, help="新建的 JSON 结果路径")
    args = parser.parse_args()
    dsn = os.environ.get("MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if not dsn:
        parser.error("请设置 MNEMOTRACE_NEXT_TEST_POSTGRES_DSN 指向专用测试实例")
    if args.out.exists() or not args.out.parent.is_dir():
        parser.error("输出文件必须尚不存在，且父目录已经创建")
    asyncio.run(
        _configured_run(dsn, args.out, args.suite),
        loop_factory=asyncio.SelectorEventLoop,
    )


if __name__ == "__main__":
    main()
