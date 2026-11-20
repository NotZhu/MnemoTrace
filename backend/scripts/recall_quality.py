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
from typing import Literal
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool
from recall_graph_quality import run_graph_suite
from recall_quality_cases import DATASETS, Dataset, FactCase, QueryCase

from mnemotrace_next import Memory
from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.migrate import apply_migrations
from mnemotrace_next.application.recall import (
    _VECTOR_MIN_COSINE,
    _candidate_limit,
    _fuse_rankings,
)
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, SpaceId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, LiteralValue, Predicate
from mnemotrace_next.ports.inference import Embedder
from mnemotrace_next.ports.queries import FactFilter

_TOP_K = 5
_MAX_TESTED_DEPTH = _candidate_limit(100)
_NOW = 100.0
_DEFAULT_LIMIT = _candidate_limit(_TOP_K)
type Hits = Sequence[tuple[FactId, float]]
type _Suite = Literal["ordinary", "graph"]


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


async def _run_dataset(
    storage: PostgresStorage, embedder: Embedder, dataset: Dataset
) -> list[_Trace]:
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

    measured = _MeasuredEmbedder(embedder)
    memory = Memory.for_recall(
        space_id=scope.id, storage=storage, embedder=measured, clock=_FixedClock()
    )
    traces = []
    for case in dataset.queries:
        started = perf_counter()
        result = await memory.recall(case.text, top_k=_TOP_K, as_of=case.as_of)
        recall_ms = (perf_counter() - started) * 1000
        if result.degraded_reason is not None:
            raise RuntimeError(f"{dataset.name}/{case.id} 发生降级，无法标定双路质量")

        filters = FactFilter(valid_at=case.as_of)
        async with storage.session(scope.id) as reader:
            started = perf_counter()
            lexical = await reader.search_lexical(
                case.text, len(facts), filters=filters, reference_time=_NOW
            )
            lexical_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            semantic = await reader.search_vectors(
                measured.vector,
                len(facts),
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
        suite: ordinary 对照普通召回，graph 对照图路召回。
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


async def _configured_run(dsn: str, out: Path, suite: _Suite) -> None:
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
    """读取显式配置的测试实例和嵌入端点并运行评测。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=("ordinary", "graph"),
        default="ordinary",
        help="对照普通召回或图路召回",
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
