"""从 HTTP 入口验证召回、事实与来源原文的读取契约。"""

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from uuid import uuid4

import httpx
import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from mnemotrace_next import GraphRecallOptions, Memory
from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.identity import issue_key
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import FactId
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, Predicate
from mnemotrace_next.ports.inference import Embedder
from mnemotrace_next.server import app as server_app
from mnemotrace_next.server.settings import Settings
from tests.ports.fakes import FakeClock, FakeEmbedder

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


@dataclass(frozen=True)
class Corpus:
    """记录合成事实的空间及读测使用的凭据。"""

    scope: Space
    sibling: Space
    headers: dict[str, str]
    foreign_headers: dict[str, str]

    @property
    def path(self) -> str:
        return f"/v1/spaces/{self.scope.id.value}"


@pytest.fixture
def corpus(database: PgDatabase) -> Corpus:
    """在实际存储中写入时态变更、图边与敏感事实。"""
    owner = uuid4().hex
    scope, sibling = space(owner), space(owner)
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, owner)
        foreign_key = issue_key(conn, uuid4().hex)

    async def seed() -> None:
        source = message(scope, "source", at=21.0, content="默认配发 XLSX 附件。")
        root, mid, tail = (
            Entity(scope.id, name) for name in ("组长", "发布组", "工程部")
        )
        predicate = Predicate("隶属")
        records = (
            replace(fact(root, "old", text="dispatchtag CSV"), valid_at=10.0).close(
                20.0, 21.0
            ),
            replace(
                fact(root, "current", text="dispatchtag XLSX", provenance=(source.id,)),
                valid_at=20.0,
                recorded_at=21.0,
            ),
            replace(
                fact(root, "hidden", text="dispatchtag 敏感"),
                valid_at=10.0,
                sensitive=True,
            ),
            replace(
                fact(root, "seed", text="dispatchtag 组长属于发布组"),
                valid_at=10.0,
                predicate=predicate,
                object=EntityRef(mid.id),
            ),
            replace(
                fact(mid, "tail", text="发布组归工程部"),
                valid_at=10.0,
                predicate=predicate,
                object=EntityRef(tail.id),
            ),
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=owner)
            await storage.create_space(scope)
            await storage.create_space(sibling)
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                for entity in (root, mid, tail):
                    await writer.upsert_entity(entity)
                await writer.save_facts(records, changed_at=21.0)
                await writer.upsert_vectors(
                    ((FactId("current"), (1.0, 0.0)),),
                    embedding_space=FakeEmbedder().embedding_space,
                )

    run(seed())
    return Corpus(
        scope,
        sibling,
        {"Authorization": f"Bearer {key.token}"},
        {"Authorization": f"Bearer {foreign_key.token}"},
    )


@asynccontextmanager
async def _http(
    settings: Settings, embedder: Embedder | None = None
) -> AsyncIterator[AsyncClient]:
    app = server_app.create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        if embedder is not None:
            app.state.embedder = embedder
        yield client


def test_http_recall_matches_core_order_and_boundaries(
    database: PgDatabase, corpus: Corpus
) -> None:
    """普通、历史和图召回沿用核心排序，越权查询不触发模型。"""

    class CountingEmbedder(FakeEmbedder):
        calls = 0

        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            self.calls += 1
            return await super().embed(texts)

    async def exercise() -> None:
        embedder = CountingEmbedder()
        path = corpus.path + "/recall"
        async with (
            _http(Settings(database.runtime), embedder) as client,
            pool(database.runtime) as connections,
        ):
            reader = Memory.for_recall(
                space_id=corpus.scope.id,
                storage=PostgresStorage(connections, owner_id=corpus.scope.owner_id),
                embedder=embedder,
                clock=FakeClock(),
                exclude_sensitive=True,
            )
            denied = await client.post(
                path, headers=corpus.foreign_headers, json={"query": "dispatchtag"}
            )
            assert denied.status_code == 404
            assert embedder.calls == 0
            invalid_inputs: tuple[dict[str, object], ...] = (
                {"query": "\x1c"},
                {"as_of": 20},
                {"as_of": "1970-01-01T00:00:20"},
                {"top_k": True},
                {"graph": {"predicates": []}},
                {"graph": {"max_hops": 5}},
                {"include_sensitive": "true"},
            )
            for invalid in invalid_inputs:
                response = await client.post(
                    path,
                    headers=corpus.headers,
                    json={"query": "dispatchtag", **invalid},
                )
                assert response.status_code == 422
            assert embedder.calls == 0

            for request, as_of, graph in (
                ({}, None, None),
                ({"as_of": "1970-01-01T08:00:19+08:00"}, 19.0, None),
                ({"as_of": "1970-01-01T00:00:20Z"}, 20.0, None),
                ({"graph": {}}, None, GraphRecallOptions()),
                (
                    {"graph": {"origin_entity_ids": []}},
                    None,
                    GraphRecallOptions(origin_entity_ids=()),
                ),
            ):
                actual = await client.post(
                    path,
                    headers=corpus.headers,
                    json={"query": "dispatchtag", **request},
                )
                assert actual.status_code == 200, actual.text
                expected = await reader.recall("dispatchtag", as_of=as_of, graph=graph)
                evidence = actual.json()["evidence"]
                ids = [item["fact_id"] for item in evidence]
                assert ids == [item.fact_id.value for item in expected.evidence]
                assert "hidden" not in ids
                assert all(
                    "recorded_at" not in item and "expired_at" not in item
                    for item in evidence
                )
                for item, expected_item in zip(
                    evidence, expected.evidence, strict=True
                ):
                    relation = expected_item.relation
                    if relation is not None:
                        assert item["relation"] == {
                            "subject_entity_id": relation.subject_id.value,
                            "predicate": relation.predicate.value,
                            "object_entity_id": relation.object_entity_id.value,
                        }
                assert actual.json()["degraded_reason"] is None
                if as_of == 19:
                    assert "old" in ids and "current" not in ids
                if as_of == 20:
                    assert "current" in ids and "old" not in ids
                if graph == GraphRecallOptions():
                    assert (
                        next(item for item in evidence if item["fact_id"] == "tail")[
                            "relation"
                        ]["predicate"]
                        == "隶属"
                    )
                else:
                    assert "tail" not in ids

            full = await client.post(
                path,
                headers=corpus.headers,
                json={"query": "dispatchtag", "include_sensitive": True},
            )
            assert "hidden" in [item["fact_id"] for item in full.json()["evidence"]]

    run(exercise())


def test_http_fact_pages_and_message_scope(
    database: PgDatabase, corpus: Corpus
) -> None:
    """列表沿用评估时点和可见性，事实详情与原文分别按空间授权。"""

    async def exercise() -> None:
        async with _http(Settings(database.runtime)) as client:
            facts = corpus.path + "/facts"
            first = await client.get(
                facts, headers=corpus.headers, params={"state": "all", "limit": 2}
            )
            assert first.status_code == 200, first.text
            page = first.json()
            assert [item["fact_id"] for item in page["items"]] == ["current", "old"]
            second = await client.get(
                facts,
                headers=corpus.headers,
                params={"state": "all", "cursor": page["next_cursor"]},
            )
            assert second.status_code == 200, second.text
            assert second.json()["evaluated_at"] == page["evaluated_at"]
            assert [item["fact_id"] for item in second.json()["items"]] == [
                "seed",
                "tail",
            ]
            assert second.json()["next_cursor"] is None
            historical = await client.get(
                facts,
                headers=corpus.headers,
                params={"state": "history", "as_of": "1970-01-01T08:00:20+08:00"},
            )
            assert historical.status_code == 200, historical.text
            assert historical.json()["evaluated_at"] == "1970-01-01T00:00:20Z"
            assert [item["fact_id"] for item in historical.json()["items"]] == ["old"]
            invalid_history = await client.get(
                facts,
                headers=corpus.headers,
                params={"state": "all", "as_of": "1970-01-01T00:00:20Z"},
            )
            assert invalid_history.status_code == 422
            for url, params in (
                (facts, {"cursor": page["next_cursor"]}),
                (
                    facts,
                    {
                        "state": "all",
                        "cursor": page["next_cursor"],
                        "include_sensitive": "true",
                    },
                ),
                (
                    f"/v1/spaces/{corpus.sibling.id.value}/facts",
                    {"state": "all", "cursor": page["next_cursor"]},
                ),
                (facts, {"cursor": "invalid"}),
            ):
                response = await client.get(url, headers=corpus.headers, params=params)
                assert response.status_code == 422
                assert response.json()["code"] == "invalid_cursor"

            old = (await client.get(facts + "/old", headers=corpus.headers)).json()
            assert (
                old["valid_at"],
                old["invalid_at"],
                old["recorded_at"],
                old["expired_at"],
            ) == (
                "1970-01-01T00:00:10Z",
                "1970-01-01T00:00:20Z",
                "1970-01-01T00:00:10Z",
                "1970-01-01T00:00:21Z",
            )
            hidden = await client.get(facts + "/hidden", headers=corpus.headers)
            assert hidden.status_code == 404
            allowed = await client.get(
                facts + "/hidden",
                headers=corpus.headers,
                params={"include_sensitive": "true"},
            )
            assert allowed.json()["sensitive"] is True
            current = (
                await client.get(facts + "/current", headers=corpus.headers)
            ).json()
            assert current["source_message_ids"] == ["source"]
            source = await client.get(
                corpus.path + "/messages/source", headers=corpus.headers
            )
            assert source.json()["content"] == "默认配发 XLSX 附件。"
            assert source.json()["event_time"] == "1970-01-01T00:00:21Z"
            assert source.headers["cache-control"] == "no-store"
            assert (
                await client.get(
                    corpus.path + "/messages/source", headers=corpus.foreign_headers
                )
            ).status_code == 404
            wrong_path = f"/v1/spaces/{corpus.sibling.id.value}/messages/source"
            missing = await client.get(wrong_path, headers=corpus.headers)
            assert missing.status_code == 404
            assert missing.json()["code"] == "message_not_found"

    run(exercise())


@pytest.mark.parametrize(
    "fact_id", ["x" * 257, "x" * 3200], ids=["legacy-id", "long-cursor"]
)
def test_http_fact_pages_accept_legacy_ids(
    database: PgDatabase, corpus: Corpus, fact_id: str
) -> None:
    """可用存量长事实 ID 生成、重传游标和读取详情。"""

    async def exercise() -> None:
        subject = Entity(corpus.scope.id, "旧事实主体")
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts(
                    (replace(fact(subject, fact_id), recorded_at=22.0),),
                    changed_at=22.0,
                )

        path = corpus.path + "/facts"
        async with _http(Settings(database.runtime)) as client:
            first = await client.get(path, headers=corpus.headers, params={"limit": 1})
            assert first.status_code == 200
            assert [item["fact_id"] for item in first.json()["items"]] == [fact_id]
            cursor = first.json()["next_cursor"]
            assert cursor
            second = await client.get(
                path, headers=corpus.headers, params={"cursor": cursor, "limit": 1}
            )
            assert second.status_code == 200
            assert [item["fact_id"] for item in second.json()["items"]] == ["current"]
            detail = await client.get(f"{path}/{fact_id}", headers=corpus.headers)
            assert detail.status_code == 200
            assert detail.json()["fact_id"] == fact_id

    run(exercise())


def test_http_legacy_message_keeps_content_with_out_of_range_event_time(
    database: PgDatabase, corpus: Corpus
) -> None:
    """保留旧原文正文和收录时间，将超范围的事件时间视为未知。"""

    async def exercise() -> None:
        original = replace(
            message(corpus.scope, "legacy-event", at=22.0, content="合成旧原文"),
            event_time=1e20,
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.append_messages((original,))
        async with _http(Settings(database.runtime)) as client:
            read = await client.get(
                corpus.path + "/messages/legacy-event", headers=corpus.headers
            )
            assert read.status_code == 200
            assert read.json()["content"] == original.content
            assert read.json()["event_time"] is None
            assert read.json()["recorded_at"] == "1970-01-01T00:00:22Z"

    run(exercise())


def test_http_recall_inference_configuration_and_errors(
    database: PgDatabase, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用固定上游响应验证服务装配、超时降级和坏响应。"""

    def embeddings(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        inputs = json.loads(request.content)["input"]
        if inputs == ["timeout"]:
            raise httpx.ReadTimeout("synthetic delay", request=request)
        if inputs == ["invalid"]:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        )

    monkeypatch.setattr(
        server_app,
        "open_inference",
        partial(
            open_inference, transport_factory=lambda: httpx.MockTransport(embeddings)
        ),
    )
    settings = Settings.from_env(
        {
            "MNEMOTRACE_NEXT_POSTGRES_DSN": database.runtime,
            "MNEMOTRACE_NEXT_EMBED_BASE_URL": "http://127.0.0.1:9/v1",
            "MNEMOTRACE_NEXT_EMBED_MODEL": "synthetic",
            "MNEMOTRACE_NEXT_EMBED_REVISION": "test",
            "MNEMOTRACE_NEXT_EMBED_DIM": "2",
        }
    )

    async def exercise() -> None:
        path = corpus.path + "/recall"
        async with _http(Settings(database.runtime)) as client:
            response = await client.post(
                path, headers=corpus.headers, json={"query": "dispatchtag"}
            )
            assert response.status_code == 503
            assert response.json()["code"] == "embedding_not_configured"
        async with _http(settings) as client:
            response = await client.post(
                path, headers=corpus.headers, json={"query": "dispatchtag"}
            )
            assert response.status_code == 200
            assert response.json()["evidence"]
            timeout = await client.post(
                path, headers=corpus.headers, json={"query": "timeout"}
            )
            assert timeout.status_code == 200
            assert timeout.json() == {
                "evidence": [],
                "degraded_reason": "embedding_timeout",
            }
            failure = await client.post(
                path, headers=corpus.headers, json={"query": "invalid"}
            )
            assert failure.status_code == 502
            assert failure.json()["code"] == "inference_failed"

    run(exercise())
