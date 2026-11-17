"""用合成数据演示端口的组合调用。"""

from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from itertools import count
from typing import Literal

from mnemotrace_next.domain.digest import Digest
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import (
    DigestId,
    EntityId,
    FactId,
    MessageId,
    SessionId,
    SpaceId,
)
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.profile import Profile
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, Predicate
from mnemotrace_next.ports.inference import Completion
from mnemotrace_next.ports.queries import FactFilter, FactQuery
from mnemotrace_next.ports.storage import FactChange, SpaceSession

_MUTABLE_FACT_FIELDS = frozenset(
    {"invalid_at", "expired_at", "labels", "sensitive", "provenance"}
)


def _validate_fact_update(before: Fact, after: Fact) -> None:
    for field in fields(before):
        if (
            field.init
            and field.name not in _MUTABLE_FACT_FIELDS
            and getattr(before, field.name) != getattr(after, field.name)
        ):
            raise ValueError(f"同标识的事实不能修改 {field.name}")

    if before.invalid_at is None:
        return
    if after.invalid_at is None or after.invalid_at > before.invalid_at:
        raise ValueError("已有的 invalid_at 只能提前")
    if after.expired_at != before.expired_at:
        raise ValueError("expired_at 必须保留首次获知时刻")


def _matches_time(fact: Fact, filters: FactFilter, reference_time: float) -> bool:
    if filters.state == "all":
        return True
    at = reference_time if filters.valid_at is None else filters.valid_at
    if filters.valid_at is not None and fact.valid_at is None:
        return False
    if filters.state == "history":
        return fact.invalid_at is not None and fact.invalid_at <= at
    return (fact.valid_at is None or fact.valid_at <= at) and (
        fact.invalid_at is None or at < fact.invalid_at
    )


def _edge_order(fact: Fact) -> tuple[bool, float, str]:
    if fact.valid_at is None:
        return (True, 0.0, fact.id.value)
    return (False, -fact.valid_at, fact.id.value)


def _rank_hits(
    hits: Iterable[tuple[FactId, float]], limit: int, min_score: float | None
) -> Sequence[tuple[FactId, float]]:
    eligible = (hit for hit in hits if min_score is None or hit[1] >= min_score)
    return tuple(sorted(eligible, key=lambda hit: (-hit[1], hit[0].value))[:limit])


class FakeSpaceSession:
    """保存测试使用的空间记录和索引项。"""

    def __init__(self, space: Space) -> None:
        self.space_id = space.id
        self._space = space
        self._purge_count = 0
        self._messages: dict[MessageId, Message] = {}
        self._facts: dict[FactId, Fact] = {}
        self._fact_salience: dict[FactId, FactSalience] = {}
        self._entities: dict[EntityId, Entity] = {}
        self._digests: dict[DigestId, Digest] = {}
        self._profile: Profile | None = None
        self._changes: list[FactChange] = []
        self._change_cursors = count(1)
        self._vectors: dict[str, dict[FactId, Sequence[float]]] = {}

    async def get_space(self) -> Space:
        return self._space

    async def get_purge_token(self) -> str:
        return str(self._purge_count)

    async def append_messages(self, messages: Sequence[Message]) -> None:
        pending: dict[MessageId, Message] = {}
        for message in messages:
            existing = self._messages.get(message.id)
            if existing is None:
                existing = pending.setdefault(message.id, message)
            if existing != replace(message, recorded_at=existing.recorded_at):
                raise ValueError("同 ID 的消息字段冲突")
        self._messages.update(pending)

    async def last_messages(
        self, session_id: SessionId, limit: int
    ) -> Sequence[Message]:
        messages = tuple(
            message
            for message in self._messages.values()
            if message.session_id == session_id
        )
        return messages[-limit:]

    async def get_messages(
        self, message_ids: Sequence[MessageId]
    ) -> Mapping[MessageId, Message]:
        return {
            message_id: self._messages[message_id]
            for message_id in message_ids
            if message_id in self._messages
        }

    async def save_facts(self, facts: Sequence[Fact], *, changed_at: float) -> None:
        snapshots = [(self._facts.get(fact.id), fact) for fact in facts]
        for before, fact in snapshots:
            if fact.space_id != self.space_id:
                raise ValueError("事实的 space_id 必须属于绑定空间")
            if before is not None:
                _validate_fact_update(before, fact)
            elif any(predecessor not in self._facts for predecessor in fact.supersedes):
                raise ValueError("supersedes 必须指向绑定空间内已存在的事实")

        for before, fact in snapshots:
            if before == fact:
                continue
            self._facts[fact.id] = fact
            self._changes.append(
                FactChange(next(self._change_cursors), changed_at, before, fact)
            )

    async def save_salience(self, states: Mapping[FactId, FactSalience]) -> None:
        if states.keys() - self._facts.keys():
            raise ValueError("显著度必须属于绑定空间内已存在的事实")
        self._fact_salience.update(states)

    def _matching_facts(
        self, filters: FactFilter, reference_time: float
    ) -> Iterator[Fact]:
        labels = set(filters.labels)
        provenance = set(filters.provenance)
        return (
            fact
            for fact in self._facts.values()
            if (filters.subject is None or fact.subject == filters.subject)
            and (filters.predicate is None or fact.predicate == filters.predicate)
            and (not labels or not labels.isdisjoint(fact.labels))
            and (not provenance or not provenance.isdisjoint(fact.provenance))
            and (not filters.exclude_sensitive or not fact.sensitive)
            and _matches_time(fact, filters, reference_time)
        )

    async def facts_in_slot(
        self,
        subject: EntityRef,
        predicate: Predicate,
        *,
        valid_at: float | None = None,
        limit: int,
    ) -> Sequence[Fact]:
        filters = FactFilter(
            state="all" if valid_at is None else "current",
            subject=subject,
            predicate=predicate,
            valid_at=valid_at,
        )
        return await self.query_facts(
            FactQuery(filters=filters, limit=limit),
            reference_time=0.0 if valid_at is None else valid_at,
        )

    async def get_fact(
        self, fact_id: FactId, *, exclude_sensitive: bool = False
    ) -> Fact | None:
        fact = self._facts.get(fact_id)
        if fact is None or (exclude_sensitive and fact.sensitive):
            return None
        return fact

    async def get_facts(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, Fact]:
        return {
            fact_id: fact
            for fact_id in ids
            if (fact := self._facts.get(fact_id)) is not None
            and (not exclude_sensitive or not fact.sensitive)
        }

    async def get_salience(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, FactSalience]:
        facts = await self.get_facts(ids, exclude_sensitive=exclude_sensitive)
        default = FactSalience()
        return {fact_id: self._fact_salience.get(fact_id, default) for fact_id in facts}

    async def query_facts(
        self, spec: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        ordered = sorted(
            self._matching_facts(spec.filters, reference_time),
            key=lambda fact: (-fact.recorded_at, fact.id.value),
        )
        if spec.after is not None:
            cursor = (-spec.after.recorded_at, spec.after.fact_id.value)
            ordered = [
                fact for fact in ordered if (-fact.recorded_at, fact.id.value) > cursor
            ]
        return tuple(ordered[: spec.limit])

    async def list_fact_changes(
        self, fact_id: FactId, *, after: int | None = None, limit: int = 100
    ) -> Sequence[FactChange]:
        changes = tuple(
            change
            for change in reversed(self._changes)
            if change.after.id == fact_id and (after is None or change.cursor < after)
        )
        return changes[:limit]

    async def delete_facts(self, fact_ids: Sequence[FactId]) -> None:
        deleted = set(fact_ids).intersection(self._facts)
        for fact_id in deleted:
            del self._facts[fact_id]
            self._fact_salience.pop(fact_id, None)
        for vectors in self._vectors.values():
            for fact_id in deleted:
                vectors.pop(fact_id, None)
        self._changes = [
            change for change in self._changes if change.after.id not in deleted
        ]
        for fact_id, fact in self._facts.items():
            if deleted.intersection(fact.supersedes):
                self._facts[fact_id] = replace(
                    fact,
                    supersedes=tuple(
                        predecessor
                        for predecessor in fact.supersedes
                        if predecessor not in deleted
                    ),
                )

    async def delete_session_data(self, session_id: SessionId) -> frozenset[FactId]:
        messages = {
            message.id
            for message in self._messages.values()
            if message.session_id == session_id
        }
        deleted = frozenset(
            fact.id
            for fact in self._facts.values()
            if messages.intersection(fact.provenance)
        )
        await self.delete_facts(tuple(deleted))
        self._changes = [
            change
            for change in self._changes
            if messages.isdisjoint(change.after.provenance)
            and (change.before is None or messages.isdisjoint(change.before.provenance))
        ]
        self._digests = {
            digest.id: digest
            for digest in self._digests.values()
            if digest.session_id != session_id
        }
        for message_id in messages:
            del self._messages[message_id]
        return deleted

    async def purge_space(self) -> None:
        self._messages.clear()
        self._facts.clear()
        self._fact_salience.clear()
        self._entities.clear()
        self._digests.clear()
        self._profile = None
        self._changes.clear()
        self._vectors.clear()
        self._purge_count += 1

    async def upsert_entity(self, entity: Entity) -> Entity:
        self._entities[entity.id] = entity
        return entity

    async def get_entity(self, entity_id: EntityId) -> Entity | None:
        return self._entities.get(entity_id)

    async def get_entities(self, ids: Sequence[EntityId]) -> Mapping[EntityId, Entity]:
        return {
            entity_id: self._entities[entity_id]
            for entity_id in ids
            if entity_id in self._entities
        }

    async def edges(
        self,
        entity_id: EntityId,
        *,
        reference_time: float,
        valid_at: float | None = None,
        direction: Literal["in", "out", "both"] = "both",
        exclude_sensitive: bool = False,
        limit: int,
    ) -> Sequence[Fact]:
        filters = FactFilter(valid_at=valid_at, exclude_sensitive=exclude_sensitive)
        matches = (
            fact
            for fact in self._matching_facts(filters, reference_time)
            if isinstance(fact.object, EntityRef)
            and (
                (direction != "in" and fact.subject.entity_id == entity_id)
                or (direction != "out" and fact.object.entity_id == entity_id)
            )
        )
        return tuple(sorted(matches, key=_edge_order)[:limit])

    async def save_digest(self, digest: Digest) -> None:
        self._digests[digest.id] = digest

    def _has_digest_sources(self, digest: Digest) -> bool:
        return digest.space_id == self.space_id and all(
            (message := self._messages.get(message_id)) is not None
            and message.space_id == self.space_id
            and message.session_id == digest.session_id
            for message_id in digest.source_message_ids
        )

    async def get_digest(self, digest_id: DigestId) -> Digest | None:
        digest = self._digests.get(digest_id)
        if digest is None or not self._has_digest_sources(digest):
            return None
        return digest

    async def get_digest_for_group(
        self, session_id: SessionId, source_message_ids: Sequence[MessageId]
    ) -> Digest | None:
        group = tuple(dict.fromkeys(source_message_ids))
        return next(
            (
                digest
                for digest in self._digests.values()
                if digest.session_id == session_id
                and digest.source_message_ids == group
                and self._has_digest_sources(digest)
            ),
            None,
        )

    async def get_latest_digest(self, session_id: SessionId) -> Digest | None:
        positions = {
            message_id: index for index, message_id in enumerate(self._messages)
        }
        digests = (
            digest
            for digest in self._digests.values()
            if digest.session_id == session_id and self._has_digest_sources(digest)
        )
        return min(
            digests,
            key=lambda digest: (
                -max(positions[source] for source in digest.source_message_ids),
                -digest.updated_at,
                digest.id.value,
            ),
            default=None,
        )

    async def save_profile(self, profile: Profile) -> None:
        self._profile = profile

    async def get_profile(self, *, exclude_sensitive: bool = False) -> Profile | None:
        return None if exclude_sensitive else self._profile

    async def delete_profile(self) -> None:
        self._profile = None

    async def upsert_vectors(
        self, items: Sequence[tuple[FactId, Sequence[float]]], *, embedding_space: str
    ) -> None:
        self._vectors.setdefault(embedding_space, {}).update(items)

    async def search_vectors(
        self,
        vector: Sequence[float],
        limit: int,
        *,
        filters: FactFilter,
        reference_time: float,
        embedding_space: str,
        min_score: float | None = None,
    ) -> Sequence[tuple[FactId, float]]:
        vectors = self._vectors.get(embedding_space, {})
        hits: list[tuple[FactId, float]] = []
        for fact in self._matching_facts(filters, reference_time):
            if fact.id in vectors:
                score = sum(
                    (
                        left * right
                        for left, right in zip(vector, vectors[fact.id], strict=True)
                    ),
                    0.0,
                )
                hits.append((fact.id, score))
        return _rank_hits(hits, limit, min_score)

    async def search_lexical(
        self,
        query: str,
        limit: int,
        *,
        filters: FactFilter,
        reference_time: float,
        min_score: float | None = None,
    ) -> Sequence[tuple[FactId, float]]:
        hits = (
            (fact.id, float(fact.text.count(query)))
            for fact in self._matching_facts(filters, reference_time)
            if query in fact.text
        )
        return _rank_hits(hits, limit, min_score)


class FakeStorage:
    """以共享内存记录验证端口的组合调用。

    事务隔离、只读限制和并发语义由实际适配器的集成测试验收。
    """

    def __init__(self) -> None:
        self.sessions: dict[SpaceId, FakeSpaceSession] = {}

    async def create_space(self, space: Space) -> None:
        self.sessions[space.id] = FakeSpaceSession(space)

    @asynccontextmanager
    async def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AsyncIterator[SpaceSession]:
        yield self.sessions[space_id]


class FakeLLM:
    async def complete(self, *, system: str, prompt: str) -> Completion:
        return Completion(prompt)

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        return {"text": prompt}


class FakeEmbedder:
    @property
    def embedding_space(self) -> str:
        return "test-embedding-v1"

    @property
    def dim(self) -> int:
        return 2

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return tuple((1.0, 0.0) for _ in texts)


class FakeReranker:
    async def rerank(
        self, query: str, documents: Sequence[tuple[FactId, str]], limit: int
    ) -> Sequence[FactId]:
        ranked = sorted(documents, key=lambda document: query not in document[1])
        return tuple(dict.fromkeys(fact_id for fact_id, _ in ranked))[:limit]


class FakeClock:
    def now(self) -> float:
        return 100.0


class FakeIdGenerator:
    def new_space_id(self) -> SpaceId:
        return SpaceId("space")

    def new_session_id(self) -> SessionId:
        return SessionId("session")

    def new_message_id(self) -> MessageId:
        return MessageId("message")

    def new_fact_id(self) -> FactId:
        return FactId("fact")

    def new_digest_id(self) -> DigestId:
        return DigestId("digest")
