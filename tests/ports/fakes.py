"""用合成数据演示端口的组合调用。"""

from collections import ChainMap
from collections.abc import (
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from itertools import count
from math import fsum, isfinite, sqrt
from typing import Literal, NamedTuple, Self, overload

from mnemotrace.domain._validation import timestamp
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Source
from mnemotrace.domain.errors import DomainError
from mnemotrace.domain.fact import Fact, validate_fact_update
from mnemotrace.domain.ids import (
    EntityId,
    FactId,
    MessageId,
    SessionId,
    SpaceId,
)
from mnemotrace.domain.message import Message
from mnemotrace.domain.profile import Profile
from mnemotrace.domain.salience import FactSalience
from mnemotrace.domain.space import Space
from mnemotrace.domain.time import utc_datetime
from mnemotrace.domain.values import EntityRef, Predicate
from mnemotrace.domain.vector import scale_by_max_abs
from mnemotrace.ports.inference import Completion
from mnemotrace.ports.queries import FactFilter, FactQuery
from mnemotrace.ports.runtime import Clock
from mnemotrace.ports.storage import (
    CutoffTrigger,
    FactChange,
    GraphEdge,
    GraphEdgeBatch,
    SessionRegistration,
    SessionUnavailable,
    SpaceSession,
    SpaceUnavailable,
    SupportGroups,
    flatten_support_groups,
    validate_cutoff_triggers,
    validate_embedding_dimension,
)


def _matches_time(fact: Fact, filters: FactFilter, reference_time: float) -> bool:
    if (
        filters.overlap_start is not None
        and fact.invalid_at is not None
        and fact.invalid_at <= filters.overlap_start
    ) or (
        filters.overlap_end is not None
        and fact.valid_at is not None
        and fact.valid_at >= filters.overlap_end
    ):
        return False
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


class _CosineVector(NamedTuple):
    """保留缩放后的向量分量及其平方范数。"""

    components: tuple[float, ...]
    squared_norm: float

    @classmethod
    def from_vector(cls, vector: Sequence[float]) -> Self:
        validate_embedding_dimension(len(vector))
        components = tuple(scale_by_max_abs(vector))
        return cls(components, fsum(value * value for value in components))


class FakeSpaceSession:
    """保存测试使用的空间记录和索引项。

    Args:
        space: 此内存会话绑定的空间快照。
        clock: 提供会话登记时刻的时钟。
    """

    def __init__(self, space: Space, clock: Clock) -> None:
        self.space_id = space.id
        self._space = space
        self._clock = clock
        self._erasure_generation = 0
        self._fact_revision = 0
        self._sessions: dict[SessionId, SessionRegistration] = {}
        self._messages: dict[MessageId, Message] = {}
        self._message_positions: dict[MessageId, int] = {}
        self._next_message_position = count(1)
        self._facts: dict[FactId, Fact] = {}
        self._fact_salience: dict[FactId, FactSalience] = {}
        self._entities: dict[EntityId, Entity] = {}
        self._profile: Profile | None = None
        self._groups: dict[FactId, SupportGroups] = {}
        self._changes: list[tuple[FactChange, frozenset[MessageId]]] = []
        self._change_cursors = count(1)
        self._vectors: dict[str, dict[FactId, _CosineVector]] = {}
        self._embedding_dims: dict[str, int] = {}

    async def get_space(self) -> Space:
        """读取会话的空间元数据。

        Returns:
            初始化会话时绑定的空间快照。
        """
        return self._space

    async def get_erasure_token(self) -> str:
        """读取当前空间的擦除标记。

        Returns:
            内存替身为当前空间保存的擦除代际标记。
        """
        return str(self._erasure_generation)

    async def get_fact_revision(self) -> int:
        """读取内存事实及向量版本。"""
        return self._fact_revision

    async def has_session(self, session_id: SessionId) -> bool:
        """查询测试对话的登记。

        Args:
            session_id: 要查询的对话标识。

        Returns:
            对话仍有登记时为真。
        """
        return session_id in self._sessions

    async def get_session(self, session_id: SessionId) -> SessionRegistration | None:
        """读取测试空间中的会话登记。"""
        return self._sessions.get(session_id)

    async def list_sessions(
        self, *, after: SessionRegistration | None, limit: int
    ) -> list[SessionRegistration]:
        """使用与 PostgreSQL 一致的会话排序位置。"""

        def position(item: SessionRegistration) -> tuple[float, str]:
            return -item.created_at.timestamp(), item.session_id.value

        sessions = sorted(self._sessions.values(), key=position)
        if after is not None:
            sessions = [item for item in sessions if position(item) > position(after)]
        return sessions[:limit]

    async def register_session(self, session_id: SessionId) -> SessionRegistration:
        """在测试空间登记对话。

        Args:
            session_id: 后端生成的对话标识。

        Raises:
            ValueError: 标识已登记。
        """
        if session_id in self._sessions:
            raise ValueError("对话标识已存在")
        session = SessionRegistration(session_id, utc_datetime(self._clock.now()))
        self._sessions[session_id] = session
        return session

    async def append_messages(self, messages: Sequence[Message]) -> None:
        """追加一批消息并保留已存在记录的首次收录时刻。

        Args:
            messages: 待追加或重放的消息。

        Raises:
            SessionUnavailable: 任一原文的对话不存在。
            ValueError: 同一标识的消息字段冲突。
        """
        pending: dict[MessageId, Message] = {}
        for message in messages:
            if message.space_id != self.space_id:
                raise ValueError("消息不属于绑定空间")
            if message.session_id not in self._sessions:
                raise SessionUnavailable("对话不可用")
            existing = self._messages.get(message.id)
            if existing is None:
                existing = pending.setdefault(message.id, message)
            if existing != replace(message, recorded_at=existing.recorded_at):
                raise ValueError("同 ID 的消息字段冲突")
        self._messages.update(pending)
        self._message_positions.update(
            (message_id, next(self._next_message_position)) for message_id in pending
        )

    async def last_messages(
        self, session_id: SessionId, limit: int
    ) -> Sequence[Message]:
        """读取指定会话的原文尾部。

        Args:
            session_id: 要读取的会话标识。
            limit: 最大返回条数，须为正整数。

        Returns:
            按内存追加顺序排列的最近原文。
        """
        messages = tuple(
            message
            for message in self._messages.values()
            if message.session_id == session_id
        )
        return messages[-limit:]

    async def get_session_summary(self, session_id: SessionId) -> str | None:
        """内存测试替身默认没有已生成的会话摘要。"""
        return None

    async def list_session_messages(
        self,
        session_id: SessionId,
        *,
        after: int | None,
        limit: int,
        through: int | None = None,
    ) -> Sequence[tuple[int, Message]]:
        """使用原文首次追加时的排序位置分页。"""
        return tuple(
            (self._message_positions[message.id], message)
            for message in self._messages.values()
            if message.session_id == session_id
            and self._message_positions[message.id] > (after or 0)
            and (through is None or self._message_positions[message.id] <= through)
        )[:limit]

    async def get_messages(
        self, message_ids: Sequence[MessageId]
    ) -> Mapping[MessageId, Message]:
        """批量查找原文。

        Args:
            message_ids: 要读取的原文标识。

        Returns:
            以原文标识为键的已有消息。
        """
        return {
            message_id: self._messages[message_id]
            for message_id in message_ids
            if message_id in self._messages
        }

    async def get_fact_support_groups(
        self, fact_ids: Sequence[FactId]
    ) -> Mapping[FactId, SupportGroups]:
        """读取现有事实的证据组。

        Args:
            fact_ids: 待查事实标识。

        Returns:
            以现有事实标识为键的证据组映射。
        """
        return {
            fact_id: self._groups[fact_id]
            for fact_id in fact_ids
            if fact_id in self._facts
        }

    async def save_facts(
        self,
        facts: Sequence[Fact],
        *,
        changed_at: float,
        support_groups: Mapping[FactId, SupportGroups] | None = None,
        cutoff_triggers: Mapping[FactId, tuple[CutoffTrigger, ...]] | None = None,
    ) -> tuple[FactChange, ...]:
        """保存事实快照并记录实际变化。

        Args:
            facts: 待保存的事实快照。
            changed_at: 写入变更记录的 UTC epoch 秒。
            support_groups: 需要替换的完整证据组。
            cutoff_triggers: 按裁决顺序排列的截止触发项。

        Returns:
            按游标升序排列的实际变更。

        Raises:
            ValueError: 事实超出绑定空间、引用缺失或更新违反事实约束。
        """
        changed_at = timestamp(changed_at, "changed_at", DomainError)
        pending = {fact.id: fact for fact in facts}
        groups_by_id = support_groups or {}
        triggers_by_id = cutoff_triggers or {}
        if (
            len(pending) != len(facts)
            or (groups_by_id.keys() | triggers_by_id.keys()) - pending.keys()
        ):
            raise ValueError("证据组及截止触发信息须属于本批唯一的事实")
        causes = ChainMap(pending, self._facts)
        changes = []
        for fact in facts:
            before = self._facts.get(fact.id)
            if fact.space_id != self.space_id:
                raise ValueError("事实的 space_id 必须属于绑定空间")
            if before is not None:
                validate_fact_update(before, fact)
            elif fact.source is Source.MANUAL and fact.provenance:
                raise ValueError("新建手工事实的 provenance 必须为空")
            entities = {fact.subject.entity_id}
            if isinstance(fact.object, EntityRef):
                entities.add(fact.object.entity_id)
            if (
                entities - self._entities.keys()
                or set(fact.provenance) - self._messages.keys()
            ):
                raise ValueError("事实引用的实体和来源须在绑定空间内存在")
            old_groups = self._groups.get(fact.id)
            groups = groups_by_id.get(fact.id)
            if groups is None:
                if before is not None and before.provenance != fact.provenance:
                    raise ValueError("改变已有事实的来源须提供完整证据组")
                groups = (
                    old_groups
                    if old_groups is not None
                    else ((fact.provenance,) if fact.provenance else ())
                )
            if (
                any(not group for group in groups)
                or flatten_support_groups(groups) != fact.provenance
            ):
                raise ValueError("证据组须非空且展开后与事实来源一致")
            triggers = triggers_by_id.get(fact.id, ())
            validate_cutoff_triggers(
                before, fact, triggers, causes, existing_causes=self._facts
            )
            if before == fact and groups == old_groups:
                continue
            changes.append((before, fact, groups, triggers))

        saved = []
        for before, fact, groups, triggers in changes:
            self._facts[fact.id] = fact
            self._groups[fact.id] = groups
            sources = frozenset(fact.provenance)
            if before is not None:
                sources = sources.union(before.provenance)
            for trigger in triggers:
                sources = sources.union(trigger.source_message_ids)
            change = FactChange(
                next(self._change_cursors),
                changed_at,
                before,
                fact,
                triggers,
            )
            self._changes.append((change, sources))
            saved.append(change)
        if self._profile is not None:
            self._profile = self._profile.without_facts(
                {
                    change.after.id
                    for change in saved
                    if change.before is not None
                    and not change.before.sensitive
                    and change.after.sensitive
                }
            )
        if saved:
            self._fact_revision += 1
        return tuple(saved)

    async def save_salience(self, states: Mapping[FactId, FactSalience]) -> None:
        """批量更新已存在事实的显著度。

        Args:
            states: 以事实标识为键的显著度状态。

        Raises:
            ValueError: 有事实标识在内存会话中缺失。
        """
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
            and (fact.subject, fact.predicate) != filters.exclude_slot
            and (not labels or not labels.isdisjoint(fact.labels))
            and (filters.source is None or fact.source == filters.source)
            and (not provenance or not provenance.isdisjoint(fact.provenance))
            and (not filters.exclude_sensitive or not fact.sensitive)
            and _matches_time(fact, filters, reference_time)
        )

    async def get_fact(
        self, fact_id: FactId, *, exclude_sensitive: bool = False
    ) -> Fact | None:
        """按标识查找可见事实。

        Args:
            fact_id: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            可见的事实快照，缺失或被过滤时为 None。
        """
        fact = self._facts.get(fact_id)
        if fact is None or (exclude_sensitive and fact.sensitive):
            return None
        return fact

    async def get_facts(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, Fact]:
        """批量查找可见事实。

        Args:
            ids: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的可见快照。
        """
        return {
            fact_id: fact
            for fact_id in ids
            if (fact := self._facts.get(fact_id)) is not None
            and (not exclude_sensitive or not fact.sensitive)
        }

    async def get_relation_edges(
        self, fact_ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, GraphEdge]:
        """按候选标识投影已保存的可见关系边。

        Args:
            fact_ids: 候选事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的可见关系边。
        """
        facts = await self.get_facts(fact_ids, exclude_sensitive=exclude_sensitive)
        return {
            fact.id: GraphEdge(fact.id, fact.subject.entity_id, fact.object.entity_id)
            for fact in facts.values()
            if isinstance(fact.object, EntityRef)
        }

    async def get_salience(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, FactSalience]:
        """批量查找可见事实的强化状态。

        Args:
            ids: 要查询显著度的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的显著度映射，尚未强化时使用领域基线。
        """
        facts = await self.get_facts(ids, exclude_sensitive=exclude_sensitive)
        default = FactSalience()
        return {fact_id: self._fact_salience.get(fact_id, default) for fact_id in facts}

    async def query_facts(
        self, spec: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        """在内存记录中查询一页事实。

        Args:
            spec: 筛选条件、分页位置及条数上限。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。

        Returns:
            按收录时刻倒序排列的一页事实，同刻按事实标识升序排列。
        """
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
        self,
        fact_id: FactId,
        *,
        after: int | None = None,
        limit: int = 100,
        exclude_sensitive: bool = False,
    ) -> Sequence[FactChange]:
        """从内存账本分页读取修改记录。

        Args:
            fact_id: 要查询的事实标识。
            after: 排除本游标及更新的记录，省略时从最新记录开始。
            limit: 最大返回条数。
            exclude_sensitive: 是否隐藏敏感变更与触发引用。

        Returns:
            按游标倒序排列的修改记录。
        """
        changes = tuple(
            change
            for change, _ in reversed(self._changes)
            if change.after.id == fact_id and (after is None or change.cursor < after)
        )
        if not changes or not exclude_sensitive:
            return changes[:limit]
        if self._facts[fact_id].sensitive:
            return ()
        return tuple(
            replace(
                change,
                cutoff_triggers=tuple(
                    trigger
                    for trigger in change.cutoff_triggers
                    if not self._facts[trigger.fact_id].sensitive
                ),
            )
            for change in changes
            if not change.after.sensitive
            and (change.before is None or not change.before.sensitive)
        )[:limit]

    async def delete_facts(self, fact_ids: Sequence[FactId]) -> None:
        """从内存索引和记录中删除事实及其引用。

        Args:
            fact_ids: 要删除的事实标识。
        """
        deleted = set(fact_ids).intersection(self._facts)
        if deleted:
            self._fact_revision += 1
        for fact_id in deleted:
            del self._facts[fact_id]
            del self._groups[fact_id]
            self._fact_salience.pop(fact_id, None)
        for vectors in self._vectors.values():
            for fact_id in deleted:
                vectors.pop(fact_id, None)
        self._changes = [
            (change, sources)
            for change, sources in self._changes
            if change.after.id not in deleted
            and all(
                trigger.fact_id not in deleted for trigger in change.cutoff_triggers
            )
        ]
        if self._profile is not None:
            self._profile = self._profile.without_facts(deleted)

    async def delete_session_data(self, session_id: SessionId) -> frozenset[FactId]:
        """删除会话原文及其派生数据，并在删去原文时推进擦除代际。

        Args:
            session_id: 待删除的会话标识。

        Returns:
            已删除的事实标识。
        """
        messages = {
            message.id
            for message in self._messages.values()
            if message.session_id == session_id
        }
        deleted: set[FactId] = set()
        for fact_id, groups in self._groups.items():
            remaining = tuple(group for group in groups if messages.isdisjoint(group))
            if remaining == groups:
                continue
            fact = self._facts[fact_id]
            if not remaining and fact.source is Source.EXTRACTED:
                deleted.add(fact_id)
                continue
            self._groups[fact_id] = remaining
            self._facts[fact_id] = replace(
                fact, provenance=flatten_support_groups(remaining)
            )
        await self.delete_facts(tuple(deleted))
        self._changes = [
            (change, sources)
            for change, sources in self._changes
            if messages.isdisjoint(sources)
        ]
        for message_id in messages:
            del self._messages[message_id]
            del self._message_positions[message_id]
        self._sessions.pop(session_id, None)
        if messages:
            self._erasure_generation += 1
            self._fact_revision += 1
        return frozenset(deleted)

    async def purge_space(self) -> None:
        """清空空间内容并推进擦除代际。"""
        self._fact_revision += 1
        self._messages.clear()
        self._message_positions.clear()
        self._sessions.clear()
        self._facts.clear()
        self._groups.clear()
        self._fact_salience.clear()
        self._entities.clear()
        self._profile = None
        self._changes.clear()
        self._vectors.clear()
        self._erasure_generation += 1

    async def upsert_entity(self, entity: Entity) -> Entity:
        """按实体标识设置内存快照。

        Args:
            entity: 待保存的实体。

        Returns:
            写入的实体快照。
        """
        self._entities[entity.id] = entity
        return entity

    async def get_entities(self, ids: Sequence[EntityId]) -> Mapping[EntityId, Entity]:
        """批量查找实体。

        Args:
            ids: 要读取的实体标识。

        Returns:
            以实体标识为键的已有实体。
        """
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
        """在内存中查找一个实体的关系事实。

        Args:
            entity_id: 关系的起始实体标识。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。
            valid_at: 显式有效时点。
            direction: 匹配主体、客体或任一端的方向。
            exclude_sensitive: 是否排除敏感事实。
            limit: 最大返回条数。

        Returns:
            按有效起点倒序排列的关系事实。未知起点排在最后，同刻按标识升序。
        """
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

    async def adjacent_edges(
        self,
        entity_ids: Sequence[EntityId],
        *,
        filters: FactFilter,
        reference_time: float,
        limit: int,
        exclude_fact_ids: Sequence[FactId] = (),
    ) -> GraphEdgeBatch:
        """从内存记录批量查找邻接边。

        Args:
            entity_ids: 可命中任一端的实体标识。
            filters: 边的事实筛选条件。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。
            limit: 最大返回边数。
            exclude_fact_ids: 截取前要排除的事实标识。

        Returns:
            按事实标识排序的邻接边及截断标记。
        """
        endpoints, excluded = set(entity_ids), set(exclude_fact_ids)
        matches = (
            GraphEdge(fact.id, fact.subject.entity_id, fact.object.entity_id)
            for fact in self._matching_facts(filters, reference_time)
            if isinstance(fact.object, EntityRef)
            and fact.id not in excluded
            and (
                fact.subject.entity_id in endpoints
                or fact.object.entity_id in endpoints
            )
        )
        edges = sorted(matches, key=lambda edge: edge.fact_id.value)
        return GraphEdgeBatch(tuple(edges[:limit]), len(edges) > limit)

    async def outgoing_edges(
        self,
        subject_ids: Sequence[EntityId],
        *,
        filters: FactFilter,
        reference_time: float,
        limit: int,
        predicates: Sequence[Predicate] = (),
    ) -> Sequence[GraphEdge]:
        """按主体输入顺序批量查找符合条件的出边。

        Args:
            subject_ids: 按遍历顺序排列的主体标识。
            filters: 每条边必须满足的事实筛选条件。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。
            limit: 整批最多返回的边数。
            predicates: 可选的精确谓词集合。

        Returns:
            按主体输入顺序、再按事实标识升序排列的唯一关系边。
        """
        positions = {item: pos for pos, item in enumerate(dict.fromkeys(subject_ids))}
        allowed = set(predicates)
        matches = (
            GraphEdge(fact.id, fact.subject.entity_id, fact.object.entity_id)
            for fact in self._matching_facts(filters, reference_time)
            if isinstance(fact.object, EntityRef)
            and fact.subject.entity_id in positions
            and (not allowed or fact.predicate in allowed)
        )
        return tuple(
            sorted(
                matches,
                key=lambda edge: (positions[edge.subject_id], edge.fact_id.value),
            )[:limit]
        )

    async def save_profile(self, profile: Profile) -> None:
        """保存同一空间内时间单调的画像快照。

        Args:
            profile: 待保存的画像快照。

        Raises:
            ValueError: 画像超出绑定空间、来源事实缺失或更新时间倒退。
        """
        if (
            profile.space_id != self.space_id
            or set(profile.source_fact_ids) - self._facts.keys()
        ):
            raise ValueError("画像及其来源事实须属于绑定空间")
        if self._profile is not None and profile.updated_at < self._profile.updated_at:
            raise ValueError("画像更新时间不能倒退")
        self._profile = profile

    async def get_profile(self) -> Profile | None:
        """读取内存中已发布的画像快照。"""
        return self._profile

    async def delete_profile(self) -> None:
        """清除内存中的当前画像。"""
        self._profile = None

    async def upsert_vectors(
        self, items: Sequence[tuple[FactId, Sequence[float]]], *, embedding_space: str
    ) -> None:
        """校验并缩放向量，再将向量和平方范数整批写入内存索引。

        Args:
            items: 事实标识及对应的合成向量。
            embedding_space: 选择内存索引的嵌入空间标识。

        Raises:
            ValueError: 标识为空、事实缺失、向量无效或维度不一致。
        """
        if not items:
            return
        if not embedding_space.strip():
            raise ValueError("embedding_space 须为非空标识")
        vectors = [
            (fact_id, _CosineVector.from_vector(vector)) for fact_id, vector in items
        ]
        dim = self._embedding_dims.get(embedding_space, len(vectors[0][1].components))
        if any(len(vector.components) != dim for _, vector in vectors):
            raise ValueError("写入向量与嵌入空间登记的维度不一致")
        if any(fact_id not in self._facts for fact_id, _ in vectors):
            raise ValueError("向量对应的事实须在绑定空间内存在")
        self._embedding_dims[embedding_space] = dim
        self._vectors.setdefault(embedding_space, {}).update(vectors)
        self._fact_revision += 1

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
        """用余弦相似度对同嵌入空间的合成向量排序。

        Args:
            vector: 查询向量。
            limit: 最大返回条数。
            filters: 候选事实须满足的条件。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。
            embedding_space: 要查询的内存索引标识。
            min_score: 包含下界的最低余弦分数，省略时不限。

        Returns:
            按分数倒序、事实标识升序排列的标识和分数。

        Raises:
            ValueError: 阈值、查询向量或嵌入空间标识无效，或向量维度不一致。
        """
        if min_score is not None and not isfinite(min_score):
            raise ValueError("min_score 须为有限数值")
        query = _CosineVector.from_vector(vector)
        if not embedding_space.strip():
            raise ValueError("embedding_space 须为非空标识")
        dim = self._embedding_dims.get(embedding_space)
        if dim is None:
            return ()
        if len(query.components) != dim:
            raise ValueError("查询向量与嵌入空间登记的维度不一致")
        vectors = self._vectors.get(embedding_space, {})
        hits: list[tuple[FactId, float]] = []
        for fact in self._matching_facts(filters, reference_time):
            candidate = vectors.get(fact.id)
            if candidate is None:
                continue
            dot = fsum(
                left * right
                for left, right in zip(
                    query.components, candidate.components, strict=True
                )
            )
            # 先乘平方范数再开方，避免分别归一化时的舍入使边界得分偏低。
            score = dot / sqrt(query.squared_norm * candidate.squared_norm)
            hits.append((fact.id, min(1.0, max(-1.0, score))))
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
        """按查询子串在正文中出现的次数排序。

        Args:
            query: 要在内存正文查找的子串。
            limit: 最大返回条数。
            filters: 候选事实须满足的条件。
            reference_time: 判断事实状态使用的默认 UTC epoch 秒。
            min_score: 包含下界的最低得分，省略时不限。

        Returns:
            按分数倒序、事实标识升序排列的标识和分数。
        """
        hits = (
            (fact.id, float(fact.text.count(query)))
            for fact in self._matching_facts(filters, reference_time)
            if query in fact.text
        )
        return _rank_hits(hits, limit, min_score)


class FakeStorage:
    """以共享内存记录验证端口的组合调用。

    事务隔离、只读限制和并发语义由实际适配器的集成测试验收。

    Args:
        clock: 提供会话登记时刻的时钟。省略时使用独立的 FakeClock。
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = FakeClock() if clock is None else clock
        self.sessions: dict[SpaceId, FakeSpaceSession] = {}

    async def create_space(self, space: Space) -> None:
        """为测试空间初始化一个内存会话。

        Args:
            space: 要注册的空间快照。

        Raises:
            ValueError: 空间标识已存在。
        """
        if space.id in self.sessions:
            raise ValueError("空间标识已存在")
        self.sessions[space.id] = FakeSpaceSession(space, self._clock)

    @asynccontextmanager
    async def session(
        self, space_id: SpaceId, *, write: bool = False
    ) -> AsyncIterator[SpaceSession]:
        """打开测试空间的共享内存会话。

        Args:
            space_id: 已注册的测试空间标识。
            write: 调用方请求的写模式。内存替身在两种模式下共享会话。

        Yields:
            绑定到目标空间的内存会话。

        Raises:
            SpaceUnavailable: 指定空间尚未注册。
        """
        session = self.sessions.get(space_id)
        if session is None:
            raise SpaceUnavailable("空间不可访问")
        yield session


class FakeLLM:
    """用提示文本构造确定性的补全结果。"""

    def structured_prompt_fits(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> bool:
        """默认允许所有合成提示，测试可覆盖此方法模拟预算。"""
        return True

    async def complete(self, *, system: str, prompt: str) -> Completion:
        """回显提示作为文本补全。

        Args:
            system: 兼容端口签名的系统指令，仅用于调用方组装。
            prompt: 要回显的提示文本。

        Returns:
            正文为提示原文、结束原因为 None 的补全。
        """
        return Completion(prompt)

    @overload
    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object]: ...

    @overload
    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T],
        enforce_context_budget: bool = False,
    ) -> T: ...

    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T] | None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object] | T:
        """返回固定的测试对象，或经应用解析器转换后的结果。

        Args:
            system: 调用方传入的系统指令。
            prompt: 要回显的提示文本。
            schema: 调用方请求的结构。替身固定使用 text 字段。
            parse: 可选的应用解析器。
            enforce_context_budget: 兼容端口签名，替身允许合成请求。

        Returns:
            固定的测试对象或解析结果。
        """
        response = await self._structured_response(
            system=system, prompt=prompt, schema=schema
        )
        return response if parse is None else parse(response)

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        return {"text": prompt}


class FakeEmbedder:
    """为每条输入提供同一个合成向量。"""

    @property
    def embedding_space(self) -> str:
        """读取合成向量空间的身份。

        Returns:
            测试使用的固定嵌入空间标识。
        """
        return "test-embedding-v1"

    @property
    def dim(self) -> int:
        """读取合成向量的维度。

        Returns:
            固定的二维分量数。
        """
        return 2

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """为输入序列生成固定响应。

        Args:
            texts: 需要对应合成向量的文本序列。

        Returns:
            与输入一一对应的二维向量，分量固定为 (1.0, 0.0)。
        """
        return tuple((1.0, 0.0) for _ in texts)


class FakeReranker:
    """用子串命中提供确定性的重排。"""

    async def rerank(
        self, query: str, documents: Sequence[tuple[FactId, str]]
    ) -> Sequence[tuple[FactId, float]]:
        """将查询子串命中映射为相关性分数。

        Args:
            query: 要查找的子串。
            documents: 合成的事实标识和正文。

        Returns:
            各事实标识及对应的命中分数。
        """
        return tuple((fact_id, float(query in text)) for fact_id, text in documents)


@dataclass
class FakeClock:
    """为测试提供可显式设定的 UTC epoch 时钟。"""

    at: float = 100.0

    def now(self) -> float:
        """读取当前设定的 UTC epoch 秒数。"""
        return self.at


class FakeIdGenerator:
    """按类型为同一测试场景依次分配可复现的唯一标识。"""

    def __init__(self) -> None:
        self._spaces = count()
        self._sessions = count()
        self._messages = count()
        self._facts = count()

    def new_space_id(self) -> SpaceId:
        """为测试空间分配下一个标识。"""
        return SpaceId(f"space-{next(self._spaces)}")

    def new_session_id(self) -> SessionId:
        """为测试会话分配下一个标识。"""
        return SessionId(f"session-{next(self._sessions)}")

    def new_message_id(self) -> MessageId:
        """为测试原文分配下一个标识。"""
        return MessageId(f"incoming-{next(self._messages)}")

    def new_fact_id(self) -> FactId:
        """为测试事实分配下一个标识。"""
        return FactId(f"new-{next(self._facts)}")
