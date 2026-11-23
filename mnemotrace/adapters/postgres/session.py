"""实现空间行锁保护下的写入和清理。"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
from typing import cast

from psycopg import DataError, sql
from psycopg.rows import args_row
from psycopg.types.json import Jsonb

from ...application.profile import is_profile_evidence
from ...domain._validation import timestamp
from ...domain.entity import Entity
from ...domain.enums import Source
from ...domain.errors import DomainError
from ...domain.fact import Fact
from ...domain.ids import FactId, MessageId, SessionId, SpaceId
from ...domain.message import Message
from ...domain.profile import Profile
from ...domain.salience import FactSalience
from ...ports.storage import (
    CutoffTrigger,
    FactChange,
    SessionRegistration,
    SessionUnavailable,
    SupportGroups,
    flatten_support_groups,
    validate_cutoff_triggers,
)
from . import search
from .facts import FactWrite, validate_fact_update, write_fact_batch
from .profile_schedule import schedule_auto_refresh
from .reader import PostgresReader
from .rows import entity_from_row, profile_to_json
from .session_summary_schedule import schedule_summaries


@dataclass(frozen=True)
class _StoredFactSupport:
    groups: SupportGroups
    next_group: int
    next_position: int


class PostgresSession(PostgresReader):
    """提供同一空间事务中的读取和写入。"""

    def _check_scope(self, space_id: SpaceId) -> None:
        if space_id != self.space_id:
            raise ValueError("记录不属于会话绑定的空间")

    async def register_session(self, session_id: SessionId) -> SessionRegistration:
        """在绑定空间登记由后端生成的对话标识。

        Args:
            session_id: 待登记的对话标识。
        Returns:
            新登记及数据库记录的创建时间。

        Raises:
            ValueError: 绑定空间内已有同一标识。
        """
        cursor = await self._conn.execute(
            """
            INSERT INTO mnemotrace.sessions (space_id, session_id)
            VALUES (%s, %s) ON CONFLICT DO NOTHING
            RETURNING created_at
            """,
            (self.space_id.value, session_id.value),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("对话标识已存在")
        return SessionRegistration(session_id, row[0])

    async def append_messages(self, messages: Sequence[Message]) -> None:
        """按输入顺序追加新原文，保留重放消息的首次收录位置。

        Args:
            messages: 待追加或重放的消息。

        Raises:
            SessionUnavailable: 任一原文的对话在绑定空间内不可用。
            ValueError: 消息超出绑定空间，或重放消息与首次收录的字段冲突。
        """
        conn = self._conn
        if not messages:
            return
        for message in messages:
            self._check_scope(message.space_id)
        session_ids = {message.session_id.value for message in messages}
        async with conn.cursor(row_factory=args_row(str)) as cursor:
            await cursor.execute(
                """
                SELECT session_id FROM mnemotrace.sessions
                WHERE space_id = %s AND session_id = ANY(%s::text[])
                """,
                (self.space_id.value, list(session_ids)),
            )
            if set(await cursor.fetchall()) != session_ids:
                raise SessionUnavailable("对话不可用")
        existing = await self.get_messages(tuple(message.id for message in messages))
        pending: dict[MessageId, Message] = {}
        for message in messages:
            first = existing.get(message.id)
            if first is None:
                first = pending.setdefault(message.id, message)
            if first != replace(message, recorded_at=first.recorded_at):
                raise ValueError("同 ID 的原文与首次收录的字段冲突")
        if pending:
            async with conn.transaction(), conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO mnemotrace.messages
                        (space_id, message_id, session_id, role, content, event_time, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            self.space_id.value,
                            message.id.value,
                            message.session_id.value,
                            message.role.value,
                            message.content,
                            message.event_time,
                            message.recorded_at,
                        )
                        for message in pending.values()
                    ],
                )
                if self._session_summary_enabled:
                    await schedule_summaries(
                        conn,
                        self.space_id,
                        {message.session_id for message in pending.values()},
                    )

    async def upsert_entity(self, entity: Entity) -> Entity:
        """按空间内的身份类型和规范名称创建或更新实体。

        Args:
            entity: 待保存的实体。

        Returns:
            写入后的实体快照。

        Raises:
            ValueError: 实体超出绑定空间。
        """
        self._check_scope(entity.space_id)
        async with self._conn.cursor(row_factory=args_row(entity_from_row)) as cursor:
            await cursor.execute(
                """
                INSERT INTO mnemotrace.entities (space_id, entity_id, name_key, name, type, kind)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (space_id, kind, name_key) DO UPDATE
                SET name = EXCLUDED.name, type = EXCLUDED.type
                RETURNING space_id, name, type, kind
                """,
                (
                    self.space_id.value,
                    entity.id.value,
                    entity.name_key,
                    entity.name,
                    entity.type,
                    entity.kind.value,
                ),
            )
            saved = await cursor.fetchone()
            assert saved is not None
            return saved

    async def _require_facts(self, ids: Collection[FactId]) -> None:
        if not ids:
            return
        async with self._conn.cursor(row_factory=args_row(FactId)) as cursor:
            await cursor.execute(
                """
                SELECT fact_id FROM mnemotrace.facts
                WHERE space_id = %s AND fact_id = ANY(%s::text[])
                """,
                (self.space_id.value, [fact_id.value for fact_id in ids]),
            )
            if set(await cursor.fetchall()) != set(ids):
                raise ValueError("引用的事实须在绑定空间内已存在")

    async def get_fact_support_groups(
        self, fact_ids: Sequence[FactId]
    ) -> Mapping[FactId, SupportGroups]:
        """按写入顺序读取绑定空间中事实的证据组。

        Args:
            fact_ids: 待查找的事实标识。

        Returns:
            已存在事实到证据组的映射。
        """
        return {
            fact_id: support.groups
            for fact_id, support in (await self._read_fact_support(fact_ids)).items()
        }

    async def _read_fact_support(
        self, fact_ids: Sequence[FactId]
    ) -> dict[FactId, _StoredFactSupport]:
        cursor = await self._conn.execute(
            """
            SELECT f.fact_id, p.support_group, p.position, p.message_id
            FROM mnemotrace.facts AS f
            LEFT JOIN mnemotrace.fact_provenance AS p USING (space_id, fact_id)
            WHERE f.space_id = %s AND f.fact_id = ANY(%s::text[])
            ORDER BY f.fact_id, p.support_group, p.position
            """,
            (self.space_id.value, [fact_id.value for fact_id in fact_ids]),
        )
        stored: dict[FactId, _StoredFactSupport] = {}
        for fact_id, rows in groupby(await cursor.fetchall(), key=lambda row: row[0]):
            groups: list[tuple[MessageId, ...]] = []
            next_group = 0
            next_position = 0
            for number, members in groupby(rows, key=lambda row: row[1]):
                if number is None:
                    continue
                messages = []
                for _, _, position, message_id in members:
                    messages.append(MessageId(message_id))
                    next_position = max(next_position, position + 1)
                groups.append(tuple(messages))
                next_group = number + 1
            stored[FactId(fact_id)] = _StoredFactSupport(
                tuple(groups), next_group, next_position
            )
        return stored

    async def save_facts(
        self,
        facts: Sequence[Fact],
        *,
        changed_at: float,
        support_groups: Mapping[FactId, SupportGroups] | None = None,
        cutoff_triggers: Mapping[FactId, tuple[CutoffTrigger, ...]] | None = None,
    ) -> tuple[FactChange, ...]:
        """保存一批事实及其变更记录，为新事实生成词法索引。

        Args:
            facts: 待保存的事实快照，每个标识只能出现一次。
            changed_at: 写入变更记录的 UTC epoch 秒。
            support_groups: 指定事实变更后的完整证据组。
            cutoff_triggers: 按裁决顺序记录设置或提前截止的触发事实及来源。

        Returns:
            本次实际写入的变更，按游标升序排列。

        Raises:
            ValueError: 审计时间无效、标识重复、事实越界、引用缺失或更新违反约束。
        """
        changed_at = timestamp(changed_at, "changed_at", DomainError)
        conn = self._conn
        groups_by_id = support_groups or {}
        triggers_by_id = cutoff_triggers or {}
        pending = {fact.id: fact for fact in facts}
        if len(pending) != len(facts):
            raise ValueError("同批每个事实 ID 只接受一份快照")
        if (groups_by_id.keys() | triggers_by_id.keys()) - pending.keys():
            raise ValueError("证据组及截止触发信息须属于本批事实")
        if not facts:
            return ()
        for fact in facts:
            self._check_scope(fact.space_id)
        existing = await self.get_facts(
            tuple(
                pending.keys()
                | {
                    trigger.fact_id
                    for triggers in triggers_by_id.values()
                    for trigger in triggers
                }
            )
        )
        old_groups = (
            await self._read_fact_support(tuple(groups_by_id)) if groups_by_id else {}
        )
        causes = {**existing, **pending}
        changes: list[FactWrite] = []
        for fact in facts:
            before = existing.get(fact.id)
            groups = groups_by_id.get(fact.id)
            if groups is not None and (
                any(not group for group in groups)
                or flatten_support_groups(groups) != fact.provenance
            ):
                raise ValueError("证据组须非空且展开后与事实来源一致")
            if before is None:
                if fact.source is Source.MANUAL and fact.provenance:
                    raise ValueError("新建手工事实的 provenance 必须为空")
                if groups is None:
                    groups = (fact.provenance,) if fact.provenance else ()
            elif groups is None and before.provenance != fact.provenance:
                raise ValueError("改变已有事实的来源须提供完整证据组")
            append_at = None
            stored = old_groups.get(fact.id)
            if stored is not None and groups is not None:
                previous = stored.groups
                if groups == previous:
                    groups = None
                elif groups[: len(previous)] == previous:
                    groups = groups[len(previous) :]
                    append_at = (stored.next_group, stored.next_position)
            triggers = triggers_by_id.get(fact.id, ())
            validate_cutoff_triggers(
                before, fact, triggers, causes, existing_causes=existing
            )
            if before == fact and groups is None:
                continue
            if before is not None:
                validate_fact_update(before, fact)
            changes.append(FactWrite(before, fact, groups, triggers, append_at))
        invalidated = {
            change.after.id
            for change in changes
            if change.before is not None
            and not change.before.same_except_provenance(change.after)
        }
        async with conn.transaction():
            saved = await write_fact_batch(conn, changes, changed_at)
            await self._prune_profile(invalidated)
            await self._track_profile_changes(saved, invalidated)
        return saved

    async def _track_profile_changes(
        self, changes: Sequence[FactChange], invalidated: set[FactId]
    ) -> None:
        subject = Entity.space_user(self.space_id)
        candidates = [
            change
            for change in changes
            if change.after.subject.entity_id == subject.id
            and change.after.source is Source.EXTRACTED
            and (
                not change.after.sensitive
                or (change.before is not None and not change.before.sensitive)
            )
            and (
                change.before is None
                or change.after.id in invalidated
                or change.after.provenance[: len(change.before.provenance)]
                != change.before.provenance
            )
        ]
        if not candidates:
            return
        auto_at = await self._advance_profile_evidence_revision()
        if auto_at is not None and any(
            (change.before is None or change.after.id in invalidated)
            and (
                is_profile_evidence(change.after, subject, auto_at)
                or (
                    change.before is not None
                    and is_profile_evidence(change.before, subject, auto_at)
                )
            )
            for change in candidates
        ):
            await schedule_auto_refresh(self._conn, self.space_id)

    async def _advance_profile_evidence_revision(self) -> float | None:
        cursor = await self._conn.execute(
            """
            UPDATE mnemotrace.spaces
            SET profile_evidence_revision = profile_evidence_revision + 1
            WHERE space_id = %s
            RETURNING CASE WHEN profile_auto_refresh
                THEN EXTRACT(EPOCH FROM statement_timestamp())::double precision END
            """,
            (self.space_id.value,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return cast(float | None, row[0])

    async def save_salience(self, states: Mapping[FactId, FactSalience]) -> None:
        """批量替换事实的访问强化状态。

        Args:
            states: 以事实标识为键的显著度状态。

        Raises:
            ValueError: 有事实标识在绑定空间内缺失，整批拒绝。
        """
        conn = self._conn
        if not states:
            return
        await self._require_facts(states.keys())
        async with conn.transaction(), conn.cursor() as cursor:
            await cursor.executemany(
                """
                INSERT INTO mnemotrace.fact_salience
                    (space_id, fact_id, salience, last_access)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (space_id, fact_id) DO UPDATE
                SET salience = EXCLUDED.salience, last_access = EXCLUDED.last_access
                """,
                [
                    (
                        self.space_id.value,
                        fact_id.value,
                        state.salience,
                        state.last_access,
                    )
                    for fact_id, state in states.items()
                ],
            )

    async def save_profile(self, profile: Profile) -> None:
        """保存绑定空间的完整画像，保证更新时间单调。

        Args:
            profile: 待保存的画像快照。

        Raises:
            ValueError: 画像超出绑定空间、来源事实缺失或更新时间倒退。
        """
        await self._store_profile(profile, require_monotonic=True)

    async def _store_profile(
        self, profile: Profile, *, require_monotonic: bool
    ) -> None:
        self._check_scope(profile.space_id)
        await self._require_facts(profile.source_fact_ids)
        saved = await self._conn.execute(
            """
            INSERT INTO mnemotrace.profiles AS current_profile (space_id, data, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (space_id) DO UPDATE
            SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
            WHERE NOT %s OR current_profile.updated_at <= EXCLUDED.updated_at
            """,
            (
                self.space_id.value,
                Jsonb(profile_to_json(profile)),
                profile.updated_at,
                require_monotonic,
            ),
        )
        if saved.rowcount == 0:
            raise ValueError("画像更新时间不能倒退")

    async def delete_profile(self) -> None:
        """幂等撤下绑定空间的画像。"""
        await self._conn.execute(
            "DELETE FROM mnemotrace.profiles WHERE space_id = %s",
            (self.space_id.value,),
        )

    async def _prune_profile(
        self, unavailable: set[FactId] | frozenset[FactId]
    ) -> None:
        if not unavailable:
            return
        profile = await self.get_profile()
        if profile is not None:
            retained = profile.without_facts(unavailable)
            if retained is None:
                await self.delete_profile()
            elif retained is not profile:
                await self.save_profile(retained)

    async def upsert_vectors(
        self, items: Sequence[tuple[FactId, Sequence[float]]], *, embedding_space: str
    ) -> None:
        """登记嵌入空间并批量写入或替换向量。

        Args:
            items: 事实标识和非零有限向量组成的批次。
            embedding_space: 与查询一致的嵌入空间标识。

        Raises:
            ValueError: 标识为空、事实缺失，或向量无效、维度不一致或超限。
        """
        conn = self._conn
        if not items:
            return
        vectors = [(fact_id, search.cosine_vector(vector)) for fact_id, vector in items]
        dim = len(vectors[0][1])
        registered = await search.embedding_dim(conn, self.space_id, embedding_space)
        if registered not in (None, dim) or any(
            len(vector) != dim for _, vector in vectors
        ):
            raise ValueError("写入向量与嵌入空间登记的维度不一致")
        by_id = dict(vectors)
        await self._require_facts(by_id.keys())
        try:
            async with conn.transaction(), conn.cursor() as cursor:
                if registered is None:
                    await cursor.execute(
                        """
                        INSERT INTO mnemotrace.embedding_spaces (space_id, embedding_space, dim)
                        VALUES (%s, %s, %s)
                        """,
                        (self.space_id.value, embedding_space, dim),
                    )
                await cursor.executemany(
                    """
                    INSERT INTO mnemotrace.fact_vectors
                        (space_id, embedding_space, fact_id, dim, embedding)
                    VALUES (%s, %s, %s, %s, %s::float8[]::public.vector)
                    ON CONFLICT (space_id, embedding_space, fact_id) DO UPDATE
                    SET embedding = EXCLUDED.embedding
                    """,
                    [
                        (
                            self.space_id.value,
                            embedding_space,
                            fact_id.value,
                            dim,
                            vector,
                        )
                        for fact_id, vector in by_id.items()
                    ],
                )
        except DataError:
            raise ValueError("向量超出 PostgreSQL 支持的范围") from None

    async def delete_facts(self, fact_ids: Sequence[FactId]) -> None:
        """幂等清理事实、索引、变更记录和依赖画像。

        Args:
            fact_ids: 绑定空间内待删除的事实标识。
        """
        async with self._conn.transaction():
            await self._delete_facts(fact_ids)

    async def _delete_facts(self, fact_ids: Sequence[FactId]) -> frozenset[FactId]:
        if not fact_ids:
            return frozenset()
        params = (self.space_id.value, [fact_id.value for fact_id in fact_ids])
        # 触发项依附于完整审计。先删主记录，避免留下缺少部分原因的历史。
        await self._conn.execute(
            """
            DELETE FROM mnemotrace.fact_changes AS change
            USING mnemotrace.fact_change_triggers AS trigger
            WHERE trigger.space_id = %s AND trigger.trigger_fact_id = ANY(%s::text[])
              AND (change.space_id, change.fact_id, change.cursor)
                = (trigger.space_id, trigger.fact_id, trigger.cursor)
            """,
            params,
        )
        async with self._conn.cursor() as cursor:
            await cursor.execute(
                """
                DELETE FROM mnemotrace.facts
                WHERE space_id = %s AND fact_id = ANY(%s::text[])
                RETURNING fact_id, subject_id = %s AND source = %s AND NOT sensitive,
                    (valid_at IS NULL OR valid_at <= EXTRACT(EPOCH FROM statement_timestamp()))
                    AND (invalid_at IS NULL OR invalid_at > EXTRACT(EPOCH FROM statement_timestamp()))
                """,
                (
                    *params,
                    Entity.space_user(self.space_id).id.value,
                    Source.EXTRACTED.value,
                ),
            )
            rows = await cursor.fetchall()
        deleted = frozenset(FactId(fact_id) for fact_id, _, _ in rows)
        await self._prune_profile(deleted)
        if any(evidence for _, evidence, _ in rows):
            auto_at = await self._advance_profile_evidence_revision()
            if auto_at is not None and any(
                evidence and current for _, evidence, current in rows
            ):
                await schedule_auto_refresh(self._conn, self.space_id)
        return deleted

    async def _erase_session_support(self, sources: Sequence[str]) -> frozenset[FactId]:
        if not sources:
            return frozenset()
        params = (self.space_id.value, list(sources))
        async with self._conn.cursor(row_factory=args_row(FactId)) as cursor:
            await cursor.execute(
                """
                DELETE FROM mnemotrace.fact_provenance AS member
                USING mnemotrace.fact_provenance AS erased
                WHERE erased.space_id = %s AND erased.message_id = ANY(%s::text[])
                  AND (member.space_id, member.fact_id, member.support_group)
                    = (erased.space_id, erased.fact_id, erased.support_group)
                RETURNING member.fact_id
                """,
                params,
            )
            affected = tuple(dict.fromkeys(await cursor.fetchall()))
        async with self._conn.cursor(row_factory=args_row(FactId)) as cursor:
            await cursor.execute(
                """
                SELECT f.fact_id FROM mnemotrace.facts AS f
                WHERE f.space_id = %s AND f.fact_id = ANY(%s::text[]) AND f.source = %s
                  AND NOT EXISTS (
                      SELECT 1 FROM mnemotrace.fact_provenance AS p
                      WHERE p.space_id = f.space_id AND p.fact_id = f.fact_id
                  )
                """,
                (
                    self.space_id.value,
                    [fact_id.value for fact_id in affected],
                    Source.EXTRACTED.value,
                ),
            )
            unsupported = await cursor.fetchall()
        await self._conn.execute(
            """
            DELETE FROM mnemotrace.fact_changes
            WHERE space_id = %s AND source_message_ids && %s::text[]
            """,
            params,
        )
        return await self._delete_facts(unsupported)

    async def delete_session_data(self, session_id: SessionId) -> frozenset[FactId]:
        """在同一事务内撤销对话并撤下失去完整证据的抽取事实。

        Args:
            session_id: 绑定空间内待清理的会话标识。

        Returns:
            本次删除的事实标识。
        """
        conn = self._conn
        scope = (self.space_id.value, session_id.value)
        async with conn.transaction():
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT message_id FROM mnemotrace.messages
                    WHERE space_id = %s AND session_id = %s
                    """,
                    scope,
                )
                sources = [row[0] for row in await cursor.fetchall()]
            deleted = await self._erase_session_support(sources)
            removed_messages = await conn.execute(
                """
                DELETE FROM mnemotrace.messages WHERE space_id = %s AND session_id = %s
                """,
                scope,
            )
            await conn.execute(
                "DELETE FROM mnemotrace.sessions WHERE space_id = %s AND session_id = %s",
                scope,
            )
            if removed_messages.rowcount > 0:
                await self._advance_erasure_generation()
        return deleted

    async def purge_space(self, *, key_hash: bytes | None = None) -> None:
        """清空内容并推进代际，可按幂等键记录清空结果。"""
        async with self._conn.transaction():
            if key_hash is not None:
                cursor = await self._conn.execute(
                    """
                    INSERT INTO mnemotrace.space_purge_keys (space_id, key_hash)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """,
                    (self.space_id.value, key_hash),
                )
                if cursor.rowcount == 0:
                    return
            await self._clear_space_contents()
            await self._advance_erasure_generation()

    async def delete_space(self) -> None:
        """在调用方持有的空间写事务内移除空间及其完整内容。"""
        await self._clear_space_contents()
        await self._conn.execute(
            "DELETE FROM mnemotrace.embedding_spaces WHERE space_id = %s",
            (self.space_id.value,),
        )
        await self._conn.execute(
            "DELETE FROM mnemotrace.spaces WHERE space_id = %s",
            (self.space_id.value,),
        )

    async def _clear_space_contents(self) -> None:
        for table in (
            "profile_refreshes",
            "profiles",
            "fact_changes",
            "facts",
            "entities",
            "messages",
            "sessions",
        ):
            await self._conn.execute(
                sql.SQL("DELETE FROM mnemotrace.{} WHERE space_id = %s").format(
                    sql.Identifier(table)
                ),
                (self.space_id.value,),
            )

    async def _advance_erasure_generation(self) -> None:
        await self._conn.execute(
            """
            UPDATE mnemotrace.spaces
            SET purge_generation = purge_generation + 1 WHERE space_id = %s
            """,
            (self.space_id.value,),
        )
