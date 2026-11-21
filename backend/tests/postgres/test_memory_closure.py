"""验证实际写入的事实可经公开召回入口读取和擦除。"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import uuid4

import pytest

from mnemotrace_next import (
    GraphRecallOptions,
    Memory,
    MessageInput,
    RecallEvidence,
    RecallRelation,
)
from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import FactId, MessageId, SessionId
from mnemotrace_next.domain.values import EntityRef

from ..inference.adjudication_samples import FactIds
from ..ports.fakes import FakeClock, FakeEmbedder, FakeLLM
from .conftest import PgDatabase
from .sample import pool, run, space

pytestmark = pytest.mark.postgres

_OLD = "星河项目默认使用中文"
_NEW = "星河项目默认改用英文"
_BRIDGE = "Atlas-71 项目由林北负责维护"
_TARGET = "林北隶属潮汐运维组"
_SHARED = "潮汐运维组使用值班系统海鸥"
_GRAPH_QUERY = "Atlas-71"


@dataclass(frozen=True)
class _Assertion:
    subject: str
    predicate: str
    object_kind: str
    object_value: str


_ASSERTIONS = {
    _OLD: _Assertion("星河项目", "default_language", "literal", "中文"),
    _NEW: _Assertion("星河项目", "default_language", "literal", "英文"),
    _BRIDGE: _Assertion("Atlas-71", "maintained_by", "entity", "林北"),
    _TARGET: _Assertion("林北", "belongs_to", "entity", "潮汐运维组"),
    _SHARED: _Assertion("潮汐运维组", "uses", "entity", "海鸥"),
}


class _Model(FakeLLM):
    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        payload = json.loads(prompt)
        if "messages" in payload:
            (message,) = payload["messages"]
            text = message["content"]
            source = message["number"]
            item = _ASSERTIONS[text]
            return {
                "facts": [
                    {
                        "subject": {"kind": "entity", "value": item.subject},
                        "predicate": item.predicate,
                        "object": {
                            "kind": item.object_kind,
                            "value": item.object_value,
                        },
                        "text": text,
                        "valid_at_evidence": {
                            "kind": "message_time",
                            "source_number": source,
                            "value": None,
                            "days": None,
                        },
                        "invalid_at_evidence": None,
                        "source_numbers": [source],
                        "labels": [],
                        "sensitive": False,
                    }
                ]
            }
        facts = {row["id"]: row["text"] for row in payload["facts"]}
        return {
            "decisions": [
                {
                    "id": pair["id"],
                    "verdict": _verdict(facts[pair["left"]], facts[pair["right"]]),
                }
                for pair in payload["pairs"]
            ]
        }


def _verdict(left: str, right: str) -> str:
    if left == right:
        return "duplicate"
    return "conflict" if {left, right} == {_OLD, _NEW} else "coexists"


class _Embedder(FakeEmbedder):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return tuple(
            (1.0, 0.0) if text in {_GRAPH_QUERY, _BRIDGE} else (0.0, 1.0)
            for text in texts
        )


class _Ids(FactIds):
    def new_session_id(self) -> SessionId:
        return SessionId(uuid4().hex)

    def new_message_id(self) -> MessageId:
        return MessageId(uuid4().hex)


def _matching(
    evidence: Sequence[RecallEvidence], *facts: Fact
) -> dict[FactId, RecallEvidence]:
    ids = {fact.id for fact in facts}
    return {item.fact_id: item for item in evidence if item.fact_id in ids}


def test_cross_session_change_is_visible_at_the_effective_boundary(
    database: PgDatabase,
) -> None:
    """两次写入后，旧事实只在新事实生效前被召回。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            memory = Memory(
                space_id=scope.id,
                storage=storage,
                llm=_Model(),
                embedder=_Embedder(),
                clock=FakeClock(),
                ids=_Ids(),
            )
            first = await memory.remember(
                (MessageInput("user", _OLD, 10.0),),
                session_id=await memory.create_session(),
            )
            second = await memory.remember(
                (MessageInput("user", _NEW, 50.0),),
                session_id=await memory.create_session(),
            )
            (old,) = (
                change.after for change in first.fact_changes if change.before is None
            )
            (new,) = (
                change.after for change in second.fact_changes if change.before is None
            )
            assert new.valid_at == 50.0
            for at, expected, until, provenance in (
                (49.0, old, 50.0, first.message_ids),
                (50.0, new, None, second.message_ids),
                (None, new, None, second.message_ids),
            ):
                result = await memory.recall("星河项目 默认语言", top_k=5, as_of=at)
                found = _matching(result.evidence, old, new)
                assert set(found) == {expected.id}
                evidence = found[expected.id]
                assert (
                    evidence.valid_at,
                    evidence.invalid_at,
                    evidence.source_message_ids,
                ) == (expected.valid_at, until, provenance)

    run(exercise())


def test_written_graph_and_session_erasure_close_through_recall(
    database: PgDatabase,
) -> None:
    """真实写入的二跳边只在足够深度下出现，擦除后按来源去留。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            scope = space()
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            memory = Memory(
                space_id=scope.id,
                storage=storage,
                llm=_Model(),
                embedder=_Embedder(),
                clock=FakeClock(),
                ids=_Ids(),
            )
            erased, kept = await memory.create_session(), await memory.create_session()
            bridge_write = await memory.remember(
                (MessageInput("user", _BRIDGE, 10.0),), session_id=kept
            )
            target_write = await memory.remember(
                (MessageInput("user", _TARGET, 20.0),), session_id=erased
            )
            shared_write = await memory.remember(
                (MessageInput("user", _SHARED, 30.0),), session_id=erased
            )
            support = await memory.remember(
                (MessageInput("user", _SHARED, 30.0),), session_id=kept
            )
            bridge, target, shared = (
                write.fact_changes[0].after
                for write in (bridge_write, target_write, shared_write)
            )
            origin = (bridge.subject.entity_id,)
            one = GraphRecallOptions(origin_entity_ids=origin, max_hops=1)
            two = GraphRecallOptions(origin_entity_ids=origin, max_hops=2)
            for options in (None, one, two):
                result = await memory.recall(_GRAPH_QUERY, top_k=5, graph=options)
                found = _matching(result.evidence, target)
                assert (target.id in found) is (options is two)
                if found:
                    evidence = found[target.id]
                    assert isinstance(target.object, EntityRef)
                    assert evidence.relation == RecallRelation(
                        target.subject.entity_id,
                        target.predicate,
                        target.object.entity_id,
                    )
                    assert evidence.source_message_ids == target_write.message_ids

            before = await memory.recall(_TARGET, top_k=5, as_of=40.0)
            assert (
                _matching(before.evidence, target)[target.id].source_message_ids
                == target_write.message_ids
            )
            before = await memory.recall(_SHARED, top_k=5)
            assert set(
                _matching(before.evidence, shared)[shared.id].source_message_ids
            ) == {*shared_write.message_ids, *support.message_ids}
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.delete_session_data(erased) == {target.id}

            for at in (None, 40.0):
                result = await memory.recall(_TARGET, top_k=5, as_of=at)
                assert not _matching(result.evidence, target)
                result = await memory.recall(_GRAPH_QUERY, top_k=5, graph=two, as_of=at)
                assert not _matching(result.evidence, target)
                result = await memory.recall(_SHARED, top_k=5, as_of=at)
                assert (
                    _matching(result.evidence, shared)[shared.id].source_message_ids
                    == support.message_ids
                )

    run(exercise())
