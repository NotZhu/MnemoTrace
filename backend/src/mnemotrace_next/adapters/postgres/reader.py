"""在绑定空间的同一事务连接中读取原文、事实和派生记录。"""

from collections.abc import Mapping, Sequence
from typing import Literal

from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, args_row, kwargs_row

from ...domain.digest import Digest
from ...domain.entity import Entity
from ...domain.fact import Fact
from ...domain.ids import DigestId, EntityId, FactId, MessageId, SessionId, SpaceId
from ...domain.message import Message
from ...domain.profile import Profile
from ...domain.salience import FactSalience
from ...domain.space import Space
from ...domain.values import EntityRef, Predicate
from ...ports.queries import FactFilter, FactQuery
from ...ports.storage import FactChange, GraphEdgeBatch
from . import search
from .fact_filters import FactConditions, fact_conditions
from .facts import SELECT_FACTS
from .rows import (
    change_from_row,
    digest_from_row,
    entity_from_row,
    fact_from_row,
    graph_edge_from_row,
    message_from_row,
    profile_from_row,
    salience_from_row,
    space_from_row,
)

_MESSAGES = sql.SQL("""
    SELECT message_id, space_id, session_id, role, content, event_time, recorded_at
    FROM mnemotrace_next.messages WHERE space_id = %s
    """)

_AVAILABLE_DIGESTS = sql.SQL("""
    SELECT d.digest_id, d.space_id, d.session_id, d.text, d.source_message_ids,
           d.created_at, d.updated_at
    FROM mnemotrace_next.digests AS d
    WHERE d.space_id = %(space_id)s
      AND NOT EXISTS (
          SELECT 1 FROM unnest(d.source_message_ids) AS source(message_id)
          WHERE NOT EXISTS (
              SELECT 1 FROM mnemotrace_next.messages AS m
              WHERE m.space_id = d.space_id AND m.session_id = d.session_id
                AND m.message_id = source.message_id
          )
      )
    """)

_EDGE_DIRECTIONS = {
    "in": "f.object_entity_id = %(endpoint)s",
    "out": "f.subject_id = %(endpoint)s",
    "both": "(f.subject_id = %(endpoint)s OR f.object_entity_id = %(endpoint)s)",
}

# 先按端点筛选，避免为满足排序而沿空间主键扫过大量非邻接事实。
_ADJACENT_EDGES = sql.SQL("""
    WITH adjacent AS MATERIALIZED (
        SELECT f.fact_id, f.subject_id, f.object_entity_id
        FROM mnemotrace_next.facts AS f
        WHERE {} AND f.object_entity_id IS NOT NULL
    )
    SELECT fact_id, subject_id, object_entity_id FROM adjacent
    ORDER BY fact_id ASC LIMIT %(fetch_limit)s
    """)


class PostgresReader:
    """提供单个空间会话中的读取操作。

    会话关闭后继续访问数据库会抛出 RuntimeError。

    Args:
        conn: 会话事务使用的 PostgreSQL 连接。
        space_id: 已通过授权的空间标识。
    """

    def __init__(self, conn: AsyncConnection[TupleRow], space_id: SpaceId) -> None:
        self._connection: AsyncConnection[TupleRow] | None = conn
        self._space_id = space_id

    @property
    def space_id(self) -> SpaceId:
        """标识本会话的空间作用域。

        Returns:
            与会话绑定的空间标识。
        """
        return self._space_id

    @property
    def _conn(self) -> AsyncConnection[TupleRow]:
        if self._connection is None:
            raise RuntimeError("空间会话已关闭")
        return self._connection

    def _close(self) -> None:
        self._connection = None

    async def get_space(self) -> Space:
        """读取当前空间的元数据。

        Returns:
            当前事务可见的空间快照。
        """
        async with self._conn.cursor(row_factory=args_row(space_from_row)) as cursor:
            await cursor.execute(
                """
                SELECT space_id, owner_id, name, created_at
                FROM mnemotrace_next.spaces WHERE space_id = %s
                """,
                (self.space_id.value,),
            )
            space = await cursor.fetchone()
            assert space is not None
            return space

    async def get_erasure_token(self) -> str:
        """读取当前事务可见的擦除标记。

        Returns:
            绑定空间的擦除代际标记。
        """
        async with self._conn.cursor(row_factory=args_row(int)) as cursor:
            await cursor.execute(
                """
                SELECT purge_generation FROM mnemotrace_next.spaces WHERE space_id = %s
                """,
                (self.space_id.value,),
            )
            generation = await cursor.fetchone()
            assert generation is not None
            return str(generation)

    async def has_session(self, session_id: SessionId) -> bool:
        """判断绑定空间的快照是否包含此对话。

        Args:
            session_id: 待检查的对话标识。

        Returns:
            在绑定空间仍有登记时为真。
        """
        cursor = await self._conn.execute(
            "SELECT 1 FROM mnemotrace_next.sessions WHERE space_id = %s AND session_id = %s",
            (self.space_id.value, session_id.value),
        )
        return await cursor.fetchone() is not None

    async def last_messages(
        self, session_id: SessionId, limit: int
    ) -> Sequence[Message]:
        """读取会话末尾的原文。

        Args:
            session_id: 要读取的会话标识。
            limit: 最大返回条数，须为正整数。

        Returns:
            最近追加的原文，按追加位置从旧到新排列。
        """
        statement = _MESSAGES + sql.SQL(
            " AND session_id = %s ORDER BY append_seq DESC LIMIT %s"
        )
        async with self._conn.cursor(row_factory=args_row(message_from_row)) as cursor:
            await cursor.execute(
                statement, (self.space_id.value, session_id.value, limit)
            )
            return tuple(reversed(await cursor.fetchall()))

    async def get_messages(
        self, message_ids: Sequence[MessageId]
    ) -> Mapping[MessageId, Message]:
        """按标识批量读取绑定空间内的原文。

        Args:
            message_ids: 要读取的原文标识。

        Returns:
            以原文标识为键的记录映射，缺失的标识省略。
        """
        statement = _MESSAGES + sql.SQL(" AND message_id = ANY(%s::text[])")
        async with self._conn.cursor(row_factory=args_row(message_from_row)) as cursor:
            await cursor.execute(
                statement, (self.space_id.value, [item.value for item in message_ids])
            )
            return {message.id: message for message in await cursor.fetchall()}

    async def _read_facts(
        self, conditions: FactConditions, tail: sql.SQL
    ) -> list[Fact]:
        statement = SELECT_FACTS + sql.SQL(" WHERE ") + conditions.where() + tail
        async with self._conn.cursor(row_factory=kwargs_row(fact_from_row)) as cursor:
            await cursor.execute(statement, conditions.params)
            return await cursor.fetchall()

    async def get_fact(
        self, fact_id: FactId, *, exclude_sensitive: bool = False
    ) -> Fact | None:
        """按标识读取一条可见事实。

        Args:
            fact_id: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            可见的事实快照。找不到或已被过滤时为 None。
        """
        return (
            await self.get_facts((fact_id,), exclude_sensitive=exclude_sensitive)
        ).get(fact_id)

    async def get_facts(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, Fact]:
        """按标识批量读取可见事实。

        Args:
            ids: 要读取的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的可见事实快照。
        """
        conditions = FactConditions(self.space_id)
        conditions.add(
            "f.fact_id = ANY(%(ids)s::text[])", ids=[item.value for item in ids]
        )
        if exclude_sensitive:
            conditions.add("NOT f.sensitive")
        return {
            fact.id: fact for fact in await self._read_facts(conditions, sql.SQL(""))
        }

    async def facts_in_slot(
        self,
        subject: EntityRef,
        predicate: Predicate,
        *,
        valid_at: float | None = None,
        limit: int,
    ) -> Sequence[Fact]:
        """读取同一主体和谓词的事实。

        Args:
            subject: 槽位的主体。
            predicate: 槽位的谓词。
            valid_at: 要查询的有效时点，省略时读取整个槽位时间线。
            limit: 最大返回条数。

        Returns:
            按收录时刻倒序排列的事实，同刻按事实标识升序排列。
        """
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

    async def query_facts(
        self, spec: FactQuery, *, reference_time: float
    ) -> Sequence[Fact]:
        """按有效时间和键集位置筛选事实。

        Args:
            spec: 事实筛选条件、分页游标和条数上限。
            reference_time: 用于判断事实状态的默认 UTC epoch 秒。

        Returns:
            游标之后的一页事实，按收录时刻倒序排列，同刻按事实标识升序排列。
        """
        conditions = fact_conditions(self.space_id, spec.filters, reference_time)
        if spec.after is not None:
            conditions.add(
                """
                (f.recorded_at < %(cursor_at)s OR
                 (f.recorded_at = %(cursor_at)s AND f.fact_id > %(cursor_id)s))
                """,
                cursor_at=spec.after.recorded_at,
                cursor_id=spec.after.fact_id.value,
            )
        conditions.params["limit"] = spec.limit
        facts = await self._read_facts(
            conditions,
            sql.SQL(" ORDER BY f.recorded_at DESC, f.fact_id ASC LIMIT %(limit)s"),
        )
        return tuple(facts)

    async def list_fact_changes(
        self, fact_id: FactId, *, after: int | None = None, limit: int = 100
    ) -> Sequence[FactChange]:
        """分页读取事实的变更记录。

        Args:
            fact_id: 要查询的事实标识。
            after: 上一页末尾的游标。省略时从最新记录开始。
            limit: 最大返回条数。

        Returns:
            游标倒序排列的记录，含变更前后快照。
        """
        async with self._conn.cursor(row_factory=args_row(change_from_row)) as cursor:
            await cursor.execute(
                """
                SELECT c.cursor, c.changed_at, c.before, c.after, ARRAY(
                    SELECT jsonb_build_object(
                        'fact_id', t.trigger_fact_id,
                        'valid_end', t.valid_end,
                        'source_message_ids', t.source_message_ids
                    )
                    FROM mnemotrace_next.fact_change_triggers AS t
                    WHERE (t.space_id, t.fact_id, t.cursor) = (c.space_id, c.fact_id, c.cursor)
                    ORDER BY t.position
                )
                FROM mnemotrace_next.fact_changes AS c
                WHERE c.space_id = %(space)s AND c.fact_id = %(fact)s
                  AND (%(after)s::bigint IS NULL OR c.cursor < %(after)s)
                ORDER BY c.cursor DESC LIMIT %(limit)s
                """,
                {
                    "space": self.space_id.value,
                    "fact": fact_id.value,
                    "after": after,
                    "limit": limit,
                },
            )
            return tuple(await cursor.fetchall())

    async def get_entity(self, entity_id: EntityId) -> Entity | None:
        """按标识读取单个实体。

        Args:
            entity_id: 要读取的实体标识。

        Returns:
            绑定空间内的实体快照，缺失时为 None。
        """
        return (await self.get_entities((entity_id,))).get(entity_id)

    async def get_entities(self, ids: Sequence[EntityId]) -> Mapping[EntityId, Entity]:
        """按标识批量读取实体。

        Args:
            ids: 要读取的实体标识。

        Returns:
            以实体标识为键的已找到快照。
        """
        async with self._conn.cursor(row_factory=args_row(entity_from_row)) as cursor:
            await cursor.execute(
                """
                SELECT space_id, name, type, kind FROM mnemotrace_next.entities
                WHERE space_id = %s AND entity_id = ANY(%s::text[])
                """,
                (self.space_id.value, [item.value for item in ids]),
            )
            return {entity.id: entity for entity in await cursor.fetchall()}

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
        """读取指定方向和有效时点的一跳实体关系。

        Args:
            entity_id: 关系的起始实体标识。
            reference_time: 未指定有效时点时使用的 UTC epoch 秒。
            valid_at: 显式有效时点，要求事实的起点已知。
            direction: in 匹配客体，out 匹配主体，both 匹配任一端。
            exclude_sensitive: 是否排除敏感事实。
            limit: 最大返回条数。

        Returns:
            按有效起点倒序排列的关系事实。未知起点排在末尾，同值按事实标识升序排列。
        """
        conditions = fact_conditions(
            self.space_id,
            FactFilter(valid_at=valid_at, exclude_sensitive=exclude_sensitive),
            reference_time,
        )
        conditions.add("f.object_entity_id IS NOT NULL")
        conditions.add(_EDGE_DIRECTIONS[direction], endpoint=entity_id.value)
        conditions.params["limit"] = limit
        facts = await self._read_facts(
            conditions,
            sql.SQL(
                " ORDER BY f.valid_at DESC NULLS LAST, f.fact_id ASC LIMIT %(limit)s"
            ),
        )
        return tuple(facts)

    async def adjacent_edges(
        self,
        entity_ids: Sequence[EntityId],
        *,
        filters: FactFilter,
        reference_time: float,
        limit: int,
        exclude_fact_ids: Sequence[FactId] = (),
    ) -> GraphEdgeBatch:
        """批量读取与给定实体相邻的关系边。

        Args:
            entity_ids: 主体或客体可以命中的实体标识。
            filters: 邻接关系对应的事实筛选条件。
            reference_time: 用于判断事实状态的默认 UTC epoch 秒。
            limit: 最大返回边数。
            exclude_fact_ids: 截取结果前须排除的事实标识。

        Returns:
            按事实标识升序排列的唯一关系边及截断标记。
        """
        if not entity_ids:
            return GraphEdgeBatch((), False)

        conditions = fact_conditions(self.space_id, filters, reference_time)
        conditions.add(
            "(f.subject_id = ANY(%(entity_ids)s::text[])"
            " OR f.object_entity_id = ANY(%(entity_ids)s::text[]))",
            entity_ids=[entity_id.value for entity_id in entity_ids],
        )
        conditions.add(
            "NOT (f.fact_id = ANY(%(exclude_ids)s::text[]))",
            exclude_ids=[fact_id.value for fact_id in exclude_fact_ids],
        )
        async with self._conn.cursor(
            row_factory=args_row(graph_edge_from_row)
        ) as cursor:
            await cursor.execute(
                _ADJACENT_EDGES.format(conditions.where()),
                {**conditions.params, "fetch_limit": limit + 1},
            )
            edges = await cursor.fetchall()
        return GraphEdgeBatch(tuple(edges[:limit]), len(edges) > limit)

    async def _digest(self, tail: str, **params: object) -> Digest | None:
        async with self._conn.cursor(row_factory=args_row(digest_from_row)) as cursor:
            await cursor.execute(
                _AVAILABLE_DIGESTS + sql.SQL(tail),
                {"space_id": self.space_id.value, **params},
            )
            return await cursor.fetchone()

    async def get_digest(self, digest_id: DigestId) -> Digest | None:
        """按标识读取摘要。

        Args:
            digest_id: 要读取的摘要标识。

        Returns:
            来源齐全的摘要。摘要或来源缺失时为 None。
        """
        return await self._digest(
            " AND d.digest_id = %(digest)s", digest=digest_id.value
        )

    async def get_digest_for_group(
        self, session_id: SessionId, source_message_ids: Sequence[MessageId]
    ) -> Digest | None:
        """读取有序消息组最新保存的可用摘要。

        Args:
            session_id: 消息组所在的会话。
            source_message_ids: 定义消息组的原文标识，按首次出现顺序去重。

        Returns:
            对应消息组的可用摘要。同刻按摘要标识升序择一，缺失时为 None。
        """
        return await self._digest(
            """
            AND d.session_id = %(session)s AND d.source_message_ids = %(sources)s::text[]
            ORDER BY d.updated_at DESC, d.digest_id ASC LIMIT 1
            """,
            session=session_id.value,
            sources=list(dict.fromkeys(item.value for item in source_message_ids)),
        )

    async def get_latest_digest(self, session_id: SessionId) -> Digest | None:
        """按原文覆盖位置选取最近的可用摘要。

        Args:
            session_id: 要查询的会话标识。

        Returns:
            覆盖原文追加位置最靠后的摘要。缺失时为 None。
        """
        return await self._digest(
            """
            AND d.session_id = %(session)s
            ORDER BY d.last_message_seq DESC, d.updated_at DESC, d.digest_id ASC LIMIT 1
            """,
            session=session_id.value,
        )

    async def get_profile(self, *, exclude_sensitive: bool = False) -> Profile | None:
        """读取绑定空间的画像。

        Args:
            exclude_sensitive: 是否使用省略整份画像的受限读取。

        Returns:
            完整画像快照。缺失或受限时为 None。
        """
        conn = self._conn
        if exclude_sensitive:
            return None
        async with conn.cursor(row_factory=args_row(profile_from_row)) as cursor:
            await cursor.execute(
                """
                SELECT space_id, data, updated_at FROM mnemotrace_next.profiles WHERE space_id = %s
                """,
                (self.space_id.value,),
            )
            return await cursor.fetchone()

    async def get_salience(
        self, ids: Sequence[FactId], *, exclude_sensitive: bool = False
    ) -> Mapping[FactId, FactSalience]:
        """批量读取可见事实的显著度。

        Args:
            ids: 要查询显著度的事实标识。
            exclude_sensitive: 是否排除敏感事实。

        Returns:
            以事实标识为键的显著度状态。可见事实尚未强化时使用领域基线。
        """
        async with self._conn.cursor(row_factory=args_row(salience_from_row)) as cursor:
            await cursor.execute(
                """
                SELECT f.fact_id, s.salience, s.last_access
                FROM mnemotrace_next.facts AS f
                LEFT JOIN mnemotrace_next.fact_salience AS s
                  ON s.space_id = f.space_id AND s.fact_id = f.fact_id
                WHERE f.space_id = %s AND f.fact_id = ANY(%s::text[])
                  AND (NOT %s OR NOT f.sensitive)
                """,
                (
                    self.space_id.value,
                    [fact_id.value for fact_id in ids],
                    exclude_sensitive,
                ),
            )
            return dict(await cursor.fetchall())

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
        """在当前快照中搜索同一嵌入空间的向量。

        Args:
            vector: 用于计算余弦相似性的查询向量。
            limit: 最大返回条数。
            filters: 向量所属事实须满足的筛选条件。
            reference_time: 用于判断事实状态的默认 UTC epoch 秒。
            embedding_space: 与写入时一致的嵌入空间标识。
            min_score: 最低相似度，包含阈值，省略时不限。

        Returns:
            按分数倒序、事实标识升序排列的候选标识和分数。
            嵌入空间尚未登记时返回空序列。

        Raises:
            ValueError: 嵌入空间标识为空，或向量无效、维度超限或与登记维度不符。
        """
        query_vector = search.cosine_vector(vector)
        conn = self._conn
        dim = await search.embedding_dim(conn, self.space_id, embedding_space)
        if dim is None:
            return ()
        if len(query_vector) != dim:
            raise ValueError("查询向量与嵌入空间登记的维度不一致")
        conditions = fact_conditions(self.space_id, filters, reference_time)
        return tuple(
            await search.search_vectors(
                conn,
                conditions,
                query_vector,
                embedding_space,
                min_score,
                limit,
            )
        )

    async def search_lexical(
        self,
        query: str,
        limit: int,
        *,
        filters: FactFilter,
        reference_time: float,
        min_score: float | None = None,
    ) -> Sequence[tuple[FactId, float]]:
        """在当前快照中搜索匹配词项的事实。

        Args:
            query: 用于分词的查询文本。
            limit: 最大返回条数。
            filters: 候选事实须满足的筛选条件。
            reference_time: 用于判断事实状态的默认 UTC epoch 秒。
            min_score: 最低词法得分，包含阈值，省略时不限。

        Returns:
            按分数倒序、事实标识升序排列的候选标识和分数。
        """
        conditions = fact_conditions(self.space_id, filters, reference_time)
        return tuple(
            await search.search_lexical(self._conn, conditions, query, min_score, limit)
        )
