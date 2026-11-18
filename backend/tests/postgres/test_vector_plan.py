"""用合成数据验证精确向量查询的实际候选范围。"""

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import cast

import psycopg
import pytest
from psycopg import sql

from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.fact_filters import fact_conditions
from mnemotrace_next.adapters.postgres.search import _RANK_HITS, _VECTOR_SEARCH
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.values import Label
from mnemotrace_next.ports.queries import FactFilter

from .conftest import PgDatabase
from .sample import fact, pool, run, space

pytestmark = pytest.mark.postgres


def _plan_nodes(node: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
    yield node
    for child in cast(list[dict[str, object]], node.get("Plans", [])):
        yield from _plan_nodes(child)


def test_vector_plan_scores_only_eligible_rows(database: PgDatabase) -> None:
    """比较宽查询和约 1% 标签命中时的实际评分输入与结果。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            label = Label("food")
            vector = (1.0, *(0.0 for _ in range(63)))
            for target, size in ((scope, 1200), (sibling, 600)):
                await storage.create_space(target)
                subject = Entity(target.id, "合成实体")
                records = tuple(
                    replace(
                        fact(subject, f"fact-{number:04}"),
                        labels=(label,) if number % 100 == 0 else (),
                    )
                    for number in range(size)
                )
                async with storage.session(target.id, write=True) as writer:
                    await writer.upsert_entity(subject)
                    await writer.save_facts(records, changed_at=10.0)
                    await writer.upsert_vectors(
                        tuple((item.id, vector) for item in records),
                        embedding_space="plan",
                    )
            async with await psycopg.AsyncConnection.connect(
                database.admin, autocommit=True
            ) as conn:
                await conn.execute(
                    "ANALYZE mnemotrace_next.facts, mnemotrace_next.fact_vectors"
                )
                for name, filters, expected_count, ids in (
                    ("宽查询", FactFilter(), 1200, (0, 1, 2)),
                    ("标签查询", FactFilter(labels=(label,)), 12, (0, 100, 200)),
                ):
                    conditions = fact_conditions(scope.id, filters, reference_time=20.0)
                    statement = (
                        sql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ")
                        + _VECTOR_SEARCH.format(conditions.where())
                        + _RANK_HITS
                    )
                    cursor = await conn.execute(
                        statement,
                        {
                            **conditions.params,
                            "vector": list(vector),
                            "embedding_space": "plan",
                            "min_score": None,
                            "limit": 3,
                        },
                    )
                    row = await cursor.fetchone()
                    assert row is not None
                    report = row[0][0]
                    (scan,) = (
                        node
                        for node in _plan_nodes(report["Plan"])
                        if node.get("CTE Name") == "eligible"
                    )
                    assert scan["Actual Rows"] == expected_count
                    print(
                        f"{name}评分 {expected_count} 条，执行 {report['Execution Time']} ms"
                    )
                    async with storage.session(scope.id) as reader:
                        hits = await reader.search_vectors(
                            vector,
                            3,
                            filters=filters,
                            reference_time=20.0,
                            embedding_space="plan",
                        )
                    assert [(item.value, score) for item, score in hits] == [
                        (f"fact-{number:04}", 1.0) for number in ids
                    ]

    run(exercise())
