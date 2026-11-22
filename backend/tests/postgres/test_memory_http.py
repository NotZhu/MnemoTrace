"""从 HTTP 入口验证召回、事实与来源原文的读取契约。"""

import asyncio
import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from uuid import uuid4

import httpx
import psycopg
import pytest
from httpx import ASGITransport, AsyncClient

from mnemotrace_next import GraphRecallOptions, Memory, RecallFilters
from mnemotrace_next.adapters.inference import open_inference
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.adapters.postgres.identity import issue_key
from mnemotrace_next.application.salience import reinforce_context_facts
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Source
from mnemotrace_next.domain.ids import FactId, SessionId, SpaceId
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, Label, Predicate
from mnemotrace_next.ports.inference import Embedder
from mnemotrace_next.ports.reranker import Reranker
from mnemotrace_next.ports.runtime import Clock
from mnemotrace_next.ports.storage import Storage
from mnemotrace_next.runtime_settings import RuntimeSettings
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
    settings: Settings,
    embedder: Embedder | None = None,
    *,
    reranker: Reranker | None = None,
) -> AsyncIterator[AsyncClient]:
    app = server_app.create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        if embedder is not None:
            app.state.embedder = embedder
        if reranker is not None:
            app.state.reranker = reranker
        yield client


def test_recall_filters_candidates_before_ranking(
    database: PgDatabase, corpus: Corpus
) -> None:
    """词法、向量和图路的候选在截取排名前应用相同过滤。"""

    class ChannelEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return tuple(
                (1.0, 0.0) if text == "semanticquery" else (0.0, 1.0) for text in texts
            )

    async def exercise() -> None:
        scope = corpus.sibling
        root, middle, tail, blocked = (
            Entity(scope.id, name) for name in ("起点", "中点", "末端", "断开")
        )
        original = message(scope, "original")
        technology, professional, food = map(
            Label, ("technology", "professional_details", "food")
        )
        lexical = replace(
            fact(root, "z-lexical", text="channellex", provenance=(original.id,)),
            valid_at=10.0,
            labels=(technology,),
        )
        semantic = replace(
            lexical, id=FactId("z-semantic"), text="合成向量", labels=(professional,)
        )
        hidden = replace(lexical, id=FactId("z-hidden"), sensitive=True)
        future = replace(lexical, id=FactId("z-future"), valid_at=20.0)
        path = replace(
            fact(root, "z-path", text="首跳", provenance=(original.id,)),
            valid_at=10.0,
            labels=(technology,),
            object=EntityRef(middle.id),
        )
        final = replace(
            fact(middle, "z-tail", text="末跳", provenance=(original.id,)),
            valid_at=10.0,
            labels=(professional,),
            object=EntityRef(tail.id),
        )
        bridge = replace(
            fact(root, "a-bridge", text="不匹配的桥", provenance=(original.id,)),
            labels=(food,),
            object=EntityRef(blocked.id),
        )
        blocked_tail = replace(
            fact(blocked, "z-blocked", text="不可达的末跳", provenance=(original.id,)),
            labels=(technology,),
            object=EntityRef(tail.id),
        )
        decoys = tuple(
            replace(
                fact(root, f"a-{index:02}", text="channellex"),
                valid_at=10.0,
                labels=(technology,),
            )
            for index in range(36)
        )
        wrong_edges = tuple(
            replace(
                fact(root, f"a-edge-{index}", text="错误首跳"),
                labels=(technology,),
                object=EntityRef(blocked.id),
            )
            for index in range(8)
        )
        embedder = ChannelEmbedder()
        settings = Settings(RuntimeSettings(database.runtime))
        async with (
            pool(database.runtime) as connections,
            _http(settings, embedder) as client,
        ):
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(original.session_id)
                await writer.append_messages((original,))
                for entity in (root, middle, tail, blocked):
                    await writer.upsert_entity(entity)
                await writer.save_facts(
                    (
                        lexical,
                        semantic,
                        hidden,
                        future,
                        path,
                        final,
                        bridge,
                        blocked_tail,
                        *decoys,
                        *wrong_edges,
                    ),
                    changed_at=30.0,
                )
                await writer.upsert_vectors(
                    (
                        (semantic.id, (4.0, 1.0)),
                        *((item.id, (1.0, 0.0)) for item in decoys),
                    ),
                    embedding_space=embedder.embedding_space,
                )
            url = f"/v1/spaces/{scope.id.value}/recall"
            filters = {
                "labels": [technology.value, professional.value],
                "source": "extracted",
            }

            async def recall(query: str, **options: object) -> list[str]:
                response = await client.post(
                    url,
                    headers=corpus.headers,
                    json={
                        "query": query,
                        "filters": filters,
                        "track_usage": False,
                        "rerank": False,
                        **options,
                    },
                )
                assert response.status_code == 200, response.text
                return [item["fact_id"] for item in response.json()["evidence"]]

            as_of = "1970-01-01T00:00:10Z"
            assert await recall("channellex", top_k=1, as_of=as_of) == ["z-lexical"]
            assert await recall("semanticquery", top_k=1) == ["z-semantic"]
            graph = {"origin_entity_ids": [root.id.value]}
            assert await recall("graphquery", top_k=1, graph=graph) == ["z-path"]
            assert await recall("graphquery", graph=graph) == ["z-path", "z-tail"]
            assert await recall("channellex", filters={"labels": ["sports"]}) == []
            assert await recall(
                "channellex", filters={"labels": [], "source": "manual"}, top_k=1
            ) == ["a-00"]
            reader = Memory.for_recall(
                space_id=scope.id,
                storage=storage,
                embedder=embedder,
                clock=FakeClock(),
                exclude_sensitive=False,
            )
            expected = await reader.recall(
                "channellex",
                as_of=10.0,
                filters=RecallFilters((technology, professional), Source.EXTRACTED),
            )
            included = await recall("channellex", as_of=as_of, include_sensitive=True)
            assert included == [item.fact_id.value for item in expected.evidence]
            assert set(included) == {"z-hidden", "z-lexical"}

    run(exercise())


def test_fact_filter_cursor_canonicalizes_labels_and_reads_old_tokens(
    database: PgDatabase, corpus: Corpus
) -> None:
    """续页用规范化筛选，旧游标仍可读取未筛选页面。"""

    async def exercise() -> None:
        settings = Settings(RuntimeSettings(database.runtime))
        scope = corpus.sibling
        subject = Entity(scope.id, "筛选分页")
        original = message(scope, "original")
        technology, food = map(Label, ("technology", "food"))
        records = (
            replace(fact(subject, "a"), labels=(technology,)),
            replace(fact(subject, "b"), labels=(food,)),
            replace(
                fact(subject, "c", provenance=(original.id,)), labels=(technology,)
            ),
            fact(subject, "d"),
        )
        async with pool(database.runtime) as connections, _http(settings) as client:
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.register_session(original.session_id)
                await writer.append_messages((original,))
                await writer.save_facts(records, changed_at=10.0)
            url = f"/v1/spaces/{scope.id.value}/facts"

            async def page(params: Sequence[tuple[str, str]]) -> httpx.Response:
                return await client.get(
                    url, headers=corpus.headers, params=list(params)
                )

            def ids(response: httpx.Response) -> list[str]:
                assert response.status_code == 200, response.text
                return [item["fact_id"] for item in response.json()["items"]]

            first = await page(
                (
                    ("limit", "1"),
                    ("source", "manual"),
                    ("labels", "technology"),
                    ("labels", "food"),
                    ("labels", "technology"),
                )
            )
            assert ids(first) == ["a"]
            cursor = first.json()["next_cursor"]
            assert cursor
            next_page = await page(
                (
                    ("cursor", cursor),
                    ("labels", "food"),
                    ("labels", "technology"),
                    ("source", "manual"),
                )
            )
            assert ids(next_page) == ["b"]
            assert next_page.json()["next_cursor"] is None
            for params in (
                (("labels", "technology"), ("source", "manual")),
                (("labels", "technology"), ("labels", "food"), ("source", "extracted")),
            ):
                response = await page((("cursor", cursor), *params))
                assert (response.status_code, response.json()["code"]) == (
                    422,
                    "invalid_cursor",
                )
            for invalid_params in (
                (("labels", ""),),
                (("labels", "unknown"),),
                (("source", "invalid"),),
            ):
                assert (await page(invalid_params)).status_code == 422

            unfiltered = await page((("limit", "1"),))
            assert ids(unfiltered) == ["a"]
            token = unfiltered.json()["next_cursor"]
            old = json.loads(urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            del old["scope"]["labels"], old["scope"]["source"]
            old_token = urlsafe_b64encode(json.dumps(old).encode()).decode().rstrip("=")
            assert ids(await page((("cursor", old_token),))) == ["b", "c", "d"]
            changed = await page((("cursor", old_token), ("labels", "technology")))
            assert (changed.status_code, changed.json()["code"]) == (
                422,
                "invalid_cursor",
            )

    run(exercise())


def test_delete_fact_keeps_original_message_and_clears_search_and_history(
    database: PgDatabase, corpus: Corpus
) -> None:
    """单独删事实时清理索引与审计，并保留原文。"""

    async def exercise() -> None:
        path = corpus.path + "/facts/current"
        async with _http(
            Settings(RuntimeSettings(database.runtime)), FakeEmbedder()
        ) as client:
            assert (
                await client.delete(path, headers=corpus.foreign_headers)
            ).status_code == 404
            wrong_space = f"/v1/spaces/{corpus.sibling.id.value}/facts/current"
            assert (await client.delete(wrong_space, headers=corpus.headers)).json()[
                "code"
            ] == "fact_not_found"
            response = await client.delete(path, headers=corpus.headers)
            assert response.status_code == 204 and response.content == b""
            assert (
                await client.delete(path, headers=corpus.headers)
            ).status_code == 404
            assert (await client.get(path, headers=corpus.headers)).status_code == 404
            original = await client.get(
                corpus.path + "/messages/source", headers=corpus.headers
            )
            assert (
                original.status_code == 200
                and original.json()["content"] == "默认配发 XLSX 附件。"
            )
            for recall in (
                {"query": "dispatchtag"},
                {"query": "dispatchtag", "as_of": "1970-01-01T00:00:21Z"},
            ):
                result = await client.post(
                    corpus.path + "/recall", json=recall, headers=corpus.headers
                )
                assert result.status_code == 200
                assert "current" not in {
                    item["fact_id"] for item in result.json()["evidence"]
                }
            hidden = await client.delete(
                corpus.path + "/facts/hidden", headers=corpus.headers
            )
            assert hidden.status_code == 204
        async with await psycopg.AsyncConnection.connect(database.runtime) as conn:
            for table in ("fact_vectors", "fact_provenance", "fact_changes"):
                cursor = await conn.execute(
                    psycopg.sql.SQL(
                        "SELECT 1 FROM mnemotrace_next.{} WHERE space_id = %s AND fact_id = 'current'"
                    ).format(psycopg.sql.Identifier(table)),
                    (corpus.scope.id.value,),
                )
                assert await cursor.fetchone() is None

    run(exercise())


def test_erase_session_preserves_facts_with_an_independent_support_group(
    database: PgDatabase, corpus: Corpus
) -> None:
    """擦除目标会话时只撤下依赖该原文的证据。"""

    async def exercise() -> None:
        kept = message(corpus.scope, "kept", session=SessionId("kept-session"), at=22.0)
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                current = await writer.get_fact(FactId("current"))
                assert current is not None
                await writer.register_session(kept.session_id)
                await writer.append_messages((kept,))
                shared = replace(
                    current,
                    id=FactId("shared"),
                    provenance=(*current.provenance, kept.id),
                )
                await writer.save_facts(
                    (shared,),
                    changed_at=22.0,
                    support_groups={shared.id: (current.provenance, (kept.id,))},
                )
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
            original = await client.get(
                corpus.path + "/messages/source", headers=corpus.headers
            )
            session = corpus.path + "/sessions/" + original.json()["session_id"]
            assert (
                await client.delete(session, headers=corpus.foreign_headers)
            ).status_code == 404
            response = await client.delete(session, headers=corpus.headers)
            assert response.status_code == 204 and response.content == b""
            for resource in (
                session,
                corpus.path + "/messages/source",
                corpus.path + "/facts/current",
            ):
                assert (
                    await client.get(resource, headers=corpus.headers)
                ).status_code == 404
            repeated = await client.delete(session, headers=corpus.headers)
            assert (repeated.status_code, repeated.json()["code"]) == (
                404,
                "session_not_found",
            )
            fact_response = await client.get(
                corpus.path + "/facts/shared", headers=corpus.headers
            )
            assert fact_response.status_code == 200
            assert fact_response.json()["source_message_ids"] == [kept.id.value]
            assert (
                await client.get(corpus.path + "/messages/kept", headers=corpus.headers)
            ).status_code == 200

    run(exercise())


def test_delete_space_removes_embedding_registration_and_only_the_target_space(
    database: PgDatabase, corpus: Corpus
) -> None:
    """空间删除移除模型维度注册及全部资源，保留账号的其他空间和凭据。"""

    async def exercise() -> None:
        with psycopg.connect(database.admin) as conn:
            conn.execute(
                "INSERT INTO mnemotrace_next.space_purge_keys (space_id, key_hash) VALUES (%s, %s)",
                (corpus.scope.id.value, b"p" * 32),
            )
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
            assert (
                await client.delete(corpus.path, headers=corpus.foreign_headers)
            ).status_code == 404
            deleted = await client.delete(corpus.path, headers=corpus.headers)
            assert deleted.status_code == 204 and deleted.content == b""
            for suffix in ("", "/facts", "/sessions", "/messages/source"):
                inaccessible = await client.get(
                    corpus.path + suffix, headers=corpus.headers
                )
                assert (inaccessible.status_code, inaccessible.json()["code"]) == (
                    404,
                    "space_not_found",
                )
            other = await client.get(
                f"/v1/spaces/{corpus.sibling.id.value}", headers=corpus.headers
            )
            assert other.status_code == 200
            assert (
                await client.delete(corpus.path, headers=corpus.headers)
            ).status_code == 404
        with psycopg.connect(database.admin) as conn:
            for table in (
                "spaces",
                "embedding_spaces",
                "fact_vectors",
                "sessions",
                "messages",
                "space_purge_keys",
            ):
                assert (
                    conn.execute(
                        psycopg.sql.SQL(
                            "SELECT 1 FROM mnemotrace_next.{} WHERE space_id = %s"
                        ).format(psycopg.sql.Identifier(table)),
                        (corpus.scope.id.value,),
                    ).fetchone()
                    is None
                )

    run(exercise())


def test_http_recall_matches_core_order_and_boundaries(
    database: PgDatabase, corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通、历史和图召回沿用核心排序，越权查询不触发模型。"""

    class CountingEmbedder(FakeEmbedder):
        calls = 0

        async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            self.calls += 1
            if texts == ("recencytag",):
                return ((0.0, 1.0),)
            return await super().embed(texts)

    class FixedReranker:
        async def rerank(
            self, query: str, documents: Sequence[tuple[FactId, str]]
        ) -> Sequence[tuple[FactId, float]]:
            return tuple(
                (fact_id, index / len(documents))
                for index, (fact_id, _) in enumerate(documents, start=1)
            )

    clock = FakeClock()
    monkeypatch.setattr("mnemotrace_next.server.v1.memories._clock", clock)

    async def exercise() -> None:
        embedder = CountingEmbedder()
        reranker = FixedReranker()
        path = corpus.path + "/recall"
        async with (
            _http(
                Settings(RuntimeSettings(database.runtime)), embedder, reranker=reranker
            ) as client,
            pool(database.runtime) as connections,
        ):
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            reader = Memory.for_recall(
                space_id=corpus.scope.id,
                storage=storage,
                embedder=embedder,
                clock=clock,
                exclude_sensitive=True,
                reranker=reranker,
            )
            denied = await client.post(
                path,
                headers=corpus.foreign_headers,
                json={
                    "query": "dispatchtag",
                    "prefer_recent": True,
                    "track_usage": True,
                },
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
                {"prefer_recent": "true"},
                {"prefer_recent": 1},
                {"prefer_recent": None},
                {"track_usage": "true"},
                {"track_usage": 1},
                {"track_usage": None},
                {"rerank": "false"},
                {"rerank": 1},
                {"filters": {"labels": ["unknown"]}},
                {"filters": {"labels": "technology"}},
                {"filters": {"source": "invalid"}},
                {"filters": {"unexpected": True}},
                {"max_evidence_chars": False},
                {"max_evidence_chars": "5"},
                {"max_evidence_chars": 0},
                {"prefer_salient": True},
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
                    json={
                        "query": "dispatchtag",
                        "rerank": True,
                        "include_sensitive": False,
                        "track_usage": False,
                        **request,
                    },
                )
                assert actual.status_code == 200, actual.text
                expected = await reader.recall(
                    "dispatchtag", as_of=as_of, graph=graph, rerank=True
                )
                assert actual.json()["rerank_applied"] is expected.rerank_applied
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
                    assert item["source_message_ids"] == [
                        item.value for item in expected_item.source_message_ids
                    ]
                    for field in ("valid_at", "invalid_at"):
                        stamp = item[field]
                        assert (
                            datetime.fromisoformat(stamp).timestamp()
                            if stamp is not None
                            else None
                        ) == getattr(expected_item, field)
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

            for rerank_options, applied in (
                ({}, True),
                ({"rerank": None}, True),
                ({"rerank": False}, False),
                ({"rerank": True}, True),
            ):
                response = await client.post(
                    path,
                    headers=corpus.headers,
                    json={"query": "dispatchtag", **rerank_options},
                )
                assert response.status_code == 200
                assert response.json()["rerank_applied"] is applied

            full = await client.post(
                path,
                headers=corpus.headers,
                json={"query": "dispatchtag", "include_sensitive": True},
            )
            assert "hidden" in [item["fact_id"] for item in full.json()["evidence"]]

            now = clock.now()
            subject = Entity(corpus.scope.id, "近期排序合成主体")
            older = replace(
                fact(subject, "recency-older", text="recencytag older"),
                valid_at=now - 90 * 86_400,
            )
            recent = replace(
                fact(subject, "recency-recent", text="recencytag recent"), valid_at=now
            )
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((older, recent), changed_at=now)
                await writer.save_salience({recent.id: FactSalience(2.0, now)})

            for enabled, options, expected_id in (
                (False, {}, older.id),
                (False, {"prefer_recent": False}, older.id),
                (False, {"prefer_recent": True}, recent.id),
                (True, {}, recent.id),
                (True, {"prefer_recent": True}, recent.id),
                (
                    True,
                    {"prefer_recent": True, "as_of": "1970-01-01T00:01:40Z"},
                    older.id,
                ),
            ):
                patch = await client.patch(
                    corpus.path, headers=corpus.headers, json={"memory_decay": enabled}
                )
                assert (
                    patch.status_code == 200 and patch.json()["memory_decay"] is enabled
                )
                response = await client.post(
                    path,
                    headers=corpus.headers,
                    json={
                        "query": "recencytag",
                        "top_k": 1,
                        "rerank": False,
                        "track_usage": False,
                        **options,
                    },
                )
                assert response.status_code == 200, response.text
                assert [hit["fact_id"] for hit in response.json()["evidence"]] == [
                    expected_id.value
                ]

    run(exercise())


def test_http_graph_predicate_round_trips_without_id_length_limit(
    database: PgDatabase, corpus: Corpus
) -> None:
    """图召回返回的长谓词可直接用于筛选下一次遍历。"""

    async def exercise() -> None:
        root, target = (Entity(corpus.scope.id, name) for name in ("入口", "目标"))
        long_edge = replace(
            fact(root, "long-predicate"),
            predicate=Predicate("p" * 257),
            object=EntityRef(target.id),
        )
        other_edge = replace(
            fact(root, "other-predicate"),
            object=EntityRef(target.id),
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.upsert_entity(root)
                await writer.upsert_entity(target)
                await writer.save_facts((long_edge, other_edge), changed_at=21.0)

        path = corpus.path + "/recall"
        request = {
            "query": "unmatchedpredicateprobe",
            "graph": {"origin_entity_ids": [root.id.value]},
            "track_usage": False,
        }
        async with _http(
            Settings(RuntimeSettings(database.runtime)), FakeEmbedder()
        ) as client:
            initial = await client.post(path, headers=corpus.headers, json=request)
            assert initial.status_code == 200, initial.text
            evidence = {item["fact_id"]: item for item in initial.json()["evidence"]}
            assert other_edge.id.value in evidence
            predicate = evidence[long_edge.id.value]["relation"]["predicate"]
            assert predicate == long_edge.predicate.value

            filtered = await client.post(
                path,
                headers=corpus.headers,
                json={
                    **request,
                    "graph": {
                        "origin_entity_ids": [root.id.value],
                        "predicates": [predicate],
                    },
                },
            )
            assert filtered.status_code == 200, filtered.text
            ids = {item["fact_id"] for item in filtered.json()["evidence"]}
            assert long_edge.id.value in ids
            assert other_edge.id.value not in ids

    run(exercise())


def test_http_fact_pages_and_message_scope(
    database: PgDatabase, corpus: Corpus
) -> None:
    """列表沿用评估时点和可见性，事实详情与原文分别按空间授权。"""

    async def exercise() -> None:
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
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
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
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


def test_out_of_range_fact_times_survive_recall_detail_and_pagination(
    database: PgDatabase, corpus: Corpus
) -> None:
    """越界字段单独标记，业务过滤与键集分页使用原始时刻。"""

    async def exercise() -> None:
        subject = Entity(corpus.scope.id, "旧档案")
        old = replace(
            fact(subject, "outside-calendar", text="temporalfixture"),
            valid_at=-62_135_596_801.0,
            invalid_at=253_402_300_800.0,
            recorded_at=253_402_300_800.0,
            expired_at=253_402_300_800.0,
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((old,), changed_at=old.recorded_at)

        path = corpus.path + "/facts"
        params = {"state": "all", "limit": "1"}
        async with _http(
            Settings(RuntimeSettings(database.runtime)), FakeEmbedder()
        ) as client:
            first = await client.get(path, headers=corpus.headers, params=params)
            assert first.status_code == 200
            (item,) = first.json()["items"]
            assert item["fact_id"] == old.id.value
            assert item["text"] == old.text
            names = ["valid_at", "invalid_at", "recorded_at", "expired_at"]
            assert item["out_of_range_fields"] == names
            assert all(item[name] is None for name in names)
            detail = await client.get(
                path + "/outside-calendar", headers=corpus.headers
            )
            assert detail.status_code == 200 and detail.json() == item
            cursor = first.json()["next_cursor"]
            assert cursor
            second = await client.get(
                path, headers=corpus.headers, params={**params, "cursor": cursor}
            )
            assert second.status_code == 200
            (other,) = second.json()["items"]
            assert other["fact_id"] == "current"
            assert other["invalid_at"] is None and other["out_of_range_fields"] == []
            history = await client.get(
                path, headers=corpus.headers, params={"state": "history"}
            )
            assert history.status_code == 200
            assert old.id.value not in {
                item["fact_id"] for item in history.json()["items"]
            }
            recalled = await client.post(
                corpus.path + "/recall",
                headers=corpus.headers,
                json={"query": old.text, "as_of": "1970-01-01T00:00:21Z"},
            )
            assert recalled.status_code == 200
            evidence = next(
                entry
                for entry in recalled.json()["evidence"]
                if entry["fact_id"] == old.id.value
            )
            assert evidence["out_of_range_fields"] == ["valid_at", "invalid_at"]
            assert evidence["valid_at"] is None and evidence["invalid_at"] is None

    run(exercise())


def test_legacy_path_ids_round_trip_through_resources_and_locations(
    database: PgDatabase,
) -> None:
    """存量 ID 可从响应回传到子路由，编码碰撞和越权均受控制。"""
    owner = uuid4().hex
    scope = replace(space(owner), id=SpaceId(owner + "/archive"))
    expected = "~" + urlsafe_b64encode(scope.id.value.encode()).decode().rstrip("=")
    collision = replace(space(owner), id=SpaceId(expected))
    with psycopg.connect(database.admin) as conn:
        key, foreign = issue_key(conn, owner), issue_key(conn, uuid4().hex)
    settings = Settings.from_env(
        {
            "MNEMOTRACE_NEXT_POSTGRES_DSN": database.runtime,
            "MNEMOTRACE_NEXT_LLM_BASE_URL": "http://127.0.0.1:1/v1",
            "MNEMOTRACE_NEXT_LLM_MODEL": "fixture",
            "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_schema",
            "MNEMOTRACE_NEXT_EMBED_BASE_URL": "http://127.0.0.1:1/v1",
            "MNEMOTRACE_NEXT_EMBED_MODEL": "fixture",
            "MNEMOTRACE_NEXT_EMBED_REVISION": "test",
            "MNEMOTRACE_NEXT_EMBED_DIM": "2",
        }
    )

    async def exercise() -> None:
        subject = Entity(scope.id, "旧对话")
        original = message(
            scope, "~原文/source", session=SessionId("session/archive"), at=25.0
        )
        old = replace(
            fact(
                subject, "fact/archive", text="pathfixture", provenance=(original.id,)
            ),
            recorded_at=25.0,
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=owner)
            await storage.create_space(scope)
            await storage.create_space(collision)
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(original.session_id)
                await writer.append_messages((original,))
                await writer.upsert_entity(subject)
                await writer.save_facts((old,), changed_at=25.0)

            headers = {"Authorization": f"Bearer {key.token}"}
            async with _http(settings, FakeEmbedder()) as client:
                first = await client.get(
                    "/v1/spaces", headers=headers, params={"limit": 1}
                )
                assert first.status_code == 200
                (listed,) = first.json()["items"]
                assert listed["space_id"] == expected
                cursor = first.json()["next_cursor"]
                assert cursor
                second = await client.get(
                    "/v1/spaces", headers=headers, params={"limit": 1, "cursor": cursor}
                )
                (other,) = second.json()["items"]
                assert (
                    other["space_id"].startswith("~") and other["space_id"] != expected
                )
                path = "/v1/spaces/" + expected
                paths = (path, "/v1/spaces/" + other["space_id"])
                for target, expected_id in zip(
                    paths, (expected, other["space_id"]), strict=True
                ):
                    detail = await client.get(target, headers=headers)
                    assert (
                        detail.status_code == 200
                        and detail.json()["space_id"] == expected_id
                    )
                denied = await client.get(
                    path, headers={"Authorization": f"Bearer {foreign.token}"}
                )
                assert (denied.status_code, denied.json()["code"]) == (
                    404,
                    "space_not_found",
                )
                for malformed in ("~", "~a=", "~_w", "~YTli", "~AA"):
                    invalid = await client.get(
                        "/v1/spaces/" + malformed, headers=headers
                    )
                    assert (invalid.status_code, invalid.json()["code"]) == (
                        422,
                        "invalid_request",
                    )

                sessions = await client.get(path + "/sessions", headers=headers)
                (registered,) = sessions.json()["items"]
                session_id = registered["session_id"]
                assert session_id.startswith("~")
                session_path = f"{path}/sessions/{session_id}"
                assert (
                    await client.get(session_path, headers=headers)
                ).json() == registered
                created = await client.post(path + "/sessions", headers=headers)
                assert created.status_code == 201
                assert created.headers["Location"].startswith(path + "/sessions/")
                assert (
                    await client.get(created.headers["Location"], headers=headers)
                ).json() == created.json()
                recalled = await client.post(
                    path + "/recall", headers=headers, json={"query": old.text}
                )
                assert recalled.status_code == 200
                (evidence,) = recalled.json()["evidence"]
                assert evidence["text"] == old.text
                (message_id,) = evidence["source_message_ids"]
                assert message_id.startswith("~")
                raw = await client.get(f"{path}/messages/{message_id}", headers=headers)
                assert (
                    raw.status_code == 200 and raw.json()["content"] == original.content
                )
                assert raw.json()["session_id"] == session_id
                assert raw.json()["message_id"] == message_id
                fact_id = evidence["fact_id"]
                assert fact_id.startswith("~")
                detail = await client.get(f"{path}/facts/{fact_id}", headers=headers)
                assert detail.status_code == 200 and detail.json()["fact_id"] == fact_id
                assert detail.json()["source_message_ids"] == [message_id]

                accepted = await client.post(
                    session_path + "/remember",
                    headers={**headers, "Idempotency-Key": "legacy"},
                    json={"messages": [{"role": "user", "content": "合成续写"}]},
                )
                try:
                    assert accepted.status_code == 202
                    assert accepted.headers["Location"].startswith(
                        path + "/operations/"
                    )
                    task = await client.get(
                        accepted.headers["Location"], headers=headers
                    )
                    assert (
                        task.status_code == 200
                        and task.json()["session_id"] == session_id
                    )
                finally:
                    assert (
                        await client.delete(session_path, headers=headers)
                    ).status_code == 204

    run(exercise())


def test_http_legacy_space_time_keeps_reads_and_updates_available(
    database: PgDatabase,
) -> None:
    """旧空间的创建时间越界时，列表、详情和改名仍然可用。"""
    owner = uuid4().hex
    old = replace(space(owner), created_at=1e20)
    with psycopg.connect(database.admin) as conn:
        key = issue_key(conn, owner)

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            await PostgresStorage(connections, owner_id=owner).create_space(old)
        headers = {"Authorization": f"Bearer {key.token}"}
        path = f"/v1/spaces/{old.id.value}"
        expected = {
            "space_id": old.id.value,
            "name": old.name,
            "memory_decay": False,
            "profile_auto_refresh": False,
            "session_summary_enabled": False,
            "created_at": None,
            "out_of_range_fields": ["created_at"],
        }
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
            listed = await client.get("/v1/spaces", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["items"] == [expected]
            detail = await client.get(path, headers=headers)
            assert detail.status_code == 200
            assert detail.json() == expected
            renamed = await client.patch(
                path, headers=headers, json={"name": "Renamed"}
            )
            assert renamed.status_code == 200
            assert renamed.json() == {**expected, "name": "Renamed"}
            assert (await client.get(path, headers=headers)).json() == renamed.json()

    run(exercise())


@pytest.mark.parametrize(
    ("recorded_at", "public_time", "out_of_range"),
    (
        (22.0, "1970-01-01T00:00:22Z", []),
        (1e20, None, ["recorded_at"]),
    ),
)
def test_http_legacy_message_keeps_content_with_out_of_range_times(
    database: PgDatabase,
    corpus: Corpus,
    recorded_at: float,
    public_time: str | None,
    out_of_range: list[str],
) -> None:
    """保留旧原文正文，事件时刻未知和收录时刻越界各自按契约返回。"""

    async def exercise() -> None:
        original = replace(
            message(corpus.scope, "legacy-event", at=22.0, content="合成旧原文"),
            event_time=1e20,
            recorded_at=recorded_at,
        )
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=corpus.scope.owner_id)
            async with storage.session(corpus.scope.id, write=True) as writer:
                await writer.append_messages((original,))
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
            read = await client.get(
                corpus.path + "/messages/legacy-event", headers=corpus.headers
            )
            assert read.status_code == 200
            assert read.json()["content"] == original.content
            assert read.json()["event_time"] is None
            assert read.json()["recorded_at"] == public_time
            assert read.json()["out_of_range_fields"] == out_of_range

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
        async with _http(Settings(RuntimeSettings(database.runtime))) as client:
            response = await client.post(
                path,
                headers=corpus.headers,
                json={"query": "dispatchtag", "rerank": True},
            )
            assert response.status_code == 503
            assert response.json()["code"] == "embedding_not_configured"
        async with _http(settings) as client:
            response = await client.post(
                path, headers=corpus.headers, json={"query": "dispatchtag"}
            )
            assert response.status_code == 200
            assert response.json()["evidence"]
            assert response.json()["rerank_applied"] is False
            for headers, status, code in (
                (corpus.headers, 503, "reranker_not_configured"),
                (corpus.foreign_headers, 404, "space_not_found"),
            ):
                unavailable = await client.post(
                    path, headers=headers, json={"query": "dispatchtag", "rerank": True}
                )
                assert (unavailable.status_code, unavailable.json()["code"]) == (
                    status,
                    code,
                )
            timeout = await client.post(
                path, headers=corpus.headers, json={"query": "timeout"}
            )
            assert timeout.status_code == 200
            assert timeout.json() == {
                "evidence": [],
                "degraded_reason": "embedding_timeout",
                "rerank_applied": False,
                "truncated": False,
            }
            failure = await client.post(
                path, headers=corpus.headers, json={"query": "invalid"}
            )
            assert failure.status_code == 502
            assert failure.json()["code"] == "inference_failed"

    run(exercise())


@pytest.mark.parametrize(
    "after_queue", ("write", "disable", "purge", "expire", "sensitive", "budget")
)
def test_http_tracks_only_final_reranked_evidence_and_rechecks_visibility(
    database: PgDatabase,
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    after_queue: str,
) -> None:
    """先用固定的 Cohere 分数重排，再验证最终结果与写回快照保护。"""

    class MutableClock(FakeClock):
        value = 100.0

        def now(self) -> float:
            return self.value

    clock = MutableClock()
    now = clock.now()
    scope_id = corpus.sibling.id
    subject = Entity(scope_id, "合成重排")
    items = tuple(
        replace(fact(subject, f"rank-{i}", text=f"rankedtag {i}"), valid_at=20.0)
        for i in range(6)
    )
    hidden = replace(fact(subject, "hidden", text="rankedtag hidden"), sensitive=True)
    calls: list[tuple[FactId, ...]] = []
    provider_mode = "ok"
    gateway_attempts = 0

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal gateway_attempts
        payload = json.loads(request.content)
        if request.url.path == "/v2/embeddings":
            if provider_mode == "embed-timeout":
                raise httpx.ReadTimeout("synthetic delay", request=request)
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            )
        assert request.method == "POST"
        assert request.url.path == "/v2/rerank"
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        assert payload == {
            "model": "synthetic-rank",
            "query": "rankedtag",
            "documents": [item.text for item in items],
            "top_n": 6,
        }
        if provider_mode == "rank-timeout":
            raise httpx.ReadTimeout("synthetic delay", request=request)
        if provider_mode == "rank-gateway-timeout":
            gateway_attempts += 1
            return httpx.Response(504, headers={"Retry-After": "0"})
        scores = (0.9, 0.8, 0.7, 0.65, 0.60, 0.59)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": i, "relevance_score": score}
                    for i, score in enumerate(scores)
                    if provider_mode != "bad-score" or i != 5
                ]
            },
        )

    monkeypatch.setattr("mnemotrace_next.server.v1.memories._clock", clock)
    monkeypatch.setattr(
        server_app,
        "open_inference",
        partial(
            open_inference, transport_factory=lambda: httpx.MockTransport(provider)
        ),
    )
    settings = Settings.from_env(
        {
            "MNEMOTRACE_NEXT_POSTGRES_DSN": database.runtime,
            "MNEMOTRACE_NEXT_EMBED_BASE_URL": "https://synthetic.invalid/v2",
            "MNEMOTRACE_NEXT_EMBED_API_KEY": "synthetic-key",
            "MNEMOTRACE_NEXT_EMBED_MODEL": "synthetic-embed",
            "MNEMOTRACE_NEXT_EMBED_REVISION": "test",
            "MNEMOTRACE_NEXT_EMBED_DIM": "2",
            "MNEMOTRACE_NEXT_RERANK_BASE_URL": "https://synthetic.invalid/v2",
            "MNEMOTRACE_NEXT_RERANK_API_KEY": "synthetic-key",
            "MNEMOTRACE_NEXT_RERANK_MODEL": "synthetic-rank",
        }
    )

    async def exercise() -> None:
        nonlocal provider_mode
        queued, resume, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def pause_write(
            storage: Storage,
            clock: Clock,
            space_id: SpaceId,
            fact_ids: Sequence[FactId],
            *,
            reference_time: float,
            expected_erasure_token: str,
            exclude_sensitive: bool,
            require_memory_decay: bool,
        ) -> None:
            calls.append(tuple(fact_ids))
            assert space_id == scope_id
            assert reference_time == now and exclude_sensitive and require_memory_decay
            queued.set()
            await resume.wait()
            await reinforce_context_facts(
                storage,
                clock,
                space_id,
                fact_ids,
                reference_time=reference_time,
                expected_erasure_token=expected_erasure_token,
                exclude_sensitive=exclude_sensitive,
                require_memory_decay=require_memory_decay,
            )
            finished.set()

        monkeypatch.setattr(
            "mnemotrace_next.server.recall_usage.reinforce_context_facts", pause_write
        )
        async with (
            pool(database.runtime) as connections,
            _http(settings) as client,
            asyncio.timeout(10),
        ):
            storage = PostgresStorage(connections, owner_id=corpus.sibling.owner_id)
            path = f"/v1/spaces/{scope_id.value}"
            headers = corpus.headers
            original_salience = FactSalience(2.0, now)

            async def seed() -> None:
                async with storage.session(scope_id, write=True) as writer:
                    await writer.upsert_entity(subject)
                    await writer.save_facts((*items, hidden), changed_at=now)
                    await writer.save_salience({items[5].id: original_salience})

            async def recall(**options: object) -> httpx.Response:
                return await client.post(
                    path + "/recall",
                    headers=headers,
                    json={"query": "rankedtag", "top_k": 5, **options},
                )

            def evidence(response: httpx.Response) -> tuple[FactId, ...]:
                assert response.status_code == 200, response.text
                return tuple(
                    FactId(item["fact_id"]) for item in response.json()["evidence"]
                )

            await seed()
            basic = tuple(item.id for item in items[:5])
            boosted = (*basic[:4], items[5].id)
            assert evidence(await recall()) == basic
            for payload in (
                {},
                {"memory_decay": None},
                {"memory_decay": "true"},
                {"name": None},
            ):
                assert (
                    await client.patch(path, headers=headers, json=payload)
                ).status_code == 422
            assert (
                await client.patch(
                    path, headers=corpus.foreign_headers, json={"memory_decay": True}
                )
            ).status_code == 404
            patch = await client.patch(
                path, headers=headers, json={"memory_decay": True}
            )
            assert patch.status_code == 200
            assert patch.json()["name"] == corpus.sibling.name
            listed = await client.get("/v1/spaces", headers=headers)
            assert {
                item["space_id"]: item["memory_decay"]
                for item in listed.json()["items"]
            } == {
                scope_id.value: True,
                corpus.scope.id.value: False,
            }
            assert evidence(await recall(track_usage=False)) == boosted
            assert (
                evidence(await recall(as_of="1970-01-01T00:01:40Z", prefer_recent=True))
                == basic
            )
            assert evidence(await recall(query="nohits")) == ()
            provider_mode = "rank-timeout"
            degraded = await recall()
            local_order = evidence(degraded)
            assert local_order
            assert degraded.json()["degraded_reason"] == "reranker_timeout"
            assert degraded.json()["rerank_applied"] is False
            provider_mode = "rank-gateway-timeout"
            degraded = await recall()
            assert gateway_attempts == 2
            assert evidence(degraded) == local_order
            assert degraded.json()["degraded_reason"] == "reranker_timeout"
            provider_mode = "embed-timeout"
            degraded = await recall()
            assert evidence(degraded)
            assert degraded.json()["degraded_reason"] == "embedding_timeout"
            provider_mode = "bad-score"
            invalid = await recall()
            assert (invalid.status_code, invalid.json()["code"]) == (
                502,
                "inference_failed",
            )
            provider_mode = "ok"
            assert not calls

            if after_queue == "budget":
                budget = len(items[0].text)
                empty = await recall(max_evidence_chars=budget - 1)
                assert evidence(empty) == () and empty.json()["truncated"] is True
                expected = boosted[:1]
                response = await recall(max_evidence_chars=budget)
                assert response.json()["truncated"] is True
            else:
                expected = boosted
                response = await recall()
            assert evidence(response) == expected
            await queued.wait()
            assert calls == [expected]
            if after_queue == "disable":
                assert (
                    await client.patch(
                        path, headers=headers, json={"memory_decay": False}
                    )
                ).status_code == 200
            elif after_queue == "purge":
                assert (
                    await client.post(
                        path + "/purge", headers={**headers, "Idempotency-Key": "queue"}
                    )
                ).status_code == 204
                await seed()
            elif after_queue in {"expire", "sensitive"}:
                current = items[5]
                updated = (
                    current.close(now + 1, now + 1)
                    if after_queue == "expire"
                    else replace(current, sensitive=True)
                )
                clock.value = now + 1
                async with storage.session(scope_id, write=True) as writer:
                    await writer.save_facts((updated,), changed_at=clock.now())
            resume.set()
            await finished.wait()

            async with storage.session(scope_id) as reader:
                states = await reader.get_salience(tuple(item.id for item in items))
            untouched = after_queue in {"disable", "purge"}
            for item in items[:5]:
                assert states[item.id] == (
                    FactSalience()
                    if untouched or item.id not in expected
                    else FactSalience(1.5, clock.now())
                )
            assert states[items[5].id] == (
                original_salience.reinforce(now)
                if after_queue == "write"
                else original_salience
            )

    run(exercise())
