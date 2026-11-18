"""联测嵌入适配器和 PostgreSQL 的向量身份隔离。"""

import json
from collections.abc import Sequence

import httpx
import pytest

from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.ports.queries import FactFilter

from .conftest import PgDatabase
from .sample import fact, pool, run, space

pytestmark = pytest.mark.postgres


def test_provider_vectors_are_queried_only_with_their_model_and_space(
    database: PgDatabase,
) -> None:
    """同维模型的向量分别可查询，另一个所有者的向量不参与排序。

    Args:
        database: 已迁移、仅供测试使用的数据库。
    """

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        leading = "茶" if payload["model"] == "synthetic-v1" else "咖啡"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "index": index,
                        "embedding": [1, 0] if text in (leading, "查询") else [0, 1],
                    }
                    for index, text in enumerate(payload["input"])
                ]
            },
        )

    async def embed(model: str) -> tuple[str, Sequence[Sequence[float]]]:
        async with open_inference(
            {
                "MNEMOTRACE_NEXT_EMBED_BASE_URL": "http://127.0.0.1:11434/v1",
                "MNEMOTRACE_NEXT_EMBED_MODEL": model,
                "MNEMOTRACE_NEXT_EMBED_REVISION": "test-r1",
                "MNEMOTRACE_NEXT_EMBED_DIM": "2",
            },
            embed=True,
            transport_factory=lambda: httpx.MockTransport(respond),
        ) as clients:
            assert clients.embedder is not None
            return clients.embedder.embedding_space, await clients.embedder.embed(
                ("茶", "咖啡", "查询")
            )

    async def exercise() -> None:
        first_id, (first_tea, first_coffee, first_query) = await embed("synthetic-v1")
        next_id, (next_tea, next_coffee, next_query) = await embed("synthetic-v2")
        assert first_id != next_id
        async with pool(database.runtime) as connections:
            own, other = space(), space("another-owner")
            storage = PostgresStorage(connections, owner_id=own.owner_id)
            foreign_storage = PostgresStorage(connections, owner_id=other.owner_id)
            await storage.create_space(own)
            await foreign_storage.create_space(other)

            subject, foreign_subject = Entity(own.id, "甲"), Entity(other.id, "乙")
            tea, coffee = (
                fact(subject, "tea", text="茶"),
                fact(subject, "coffee", text="咖啡"),
            )
            foreign_fact = fact(foreign_subject, "foreign")
            async with foreign_storage.session(other.id, write=True) as writer:
                await writer.upsert_entity(foreign_subject)
                await writer.save_facts((foreign_fact,), changed_at=15.0)
                await writer.upsert_vectors(
                    ((foreign_fact.id, first_tea),), embedding_space=first_id
                )

            async with storage.session(own.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((tea, coffee), changed_at=15.0)
                await writer.upsert_vectors(
                    ((tea.id, first_tea), (coffee.id, first_coffee)),
                    embedding_space=first_id,
                )
                await writer.upsert_vectors(
                    ((tea.id, next_tea), (coffee.id, next_coffee)),
                    embedding_space=next_id,
                )

            async with storage.session(own.id) as reader:
                for identity, query, expected in (
                    (first_id, first_query, [tea.id, coffee.id]),
                    (next_id, next_query, [coffee.id, tea.id]),
                ):
                    hits = await reader.search_vectors(
                        query,
                        10,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space=identity,
                    )
                    assert [fact_id for fact_id, _ in hits] == expected

    run(exercise())
