"""本体工作集。用于从数据库中读取本体数据。"""
from contextlib import contextmanager

from .errors import VersionConflict
from ...util import name_key
from ...retrieve.lexical import bm25_query_terms
from .records import ENTITY, ENTITY_TYPE, RELATION_TYPE, domain


class OntologyWorkset:
    def __init__(self, dao, snapshot):
        """绑定本体解析使用的空间快照。

        Args:
            dao: 已绑定到目标空间的数据访问对象。
            snapshot: 解析依赖的空间版本。
        """
        self.dao = dao
        self.snapshot = snapshot
        self._known = {}

    @contextmanager
    def _read(self):
        """读取与解析开始时版本一致的空间。

        Yields:
            当前快照内可用的 SQL 接口。

        Raises:
            VersionConflict: 解析期间空间版本已变化。
        """
        with self.dao.read_snapshot() as view:
            if (view.snapshot.core_revision, view.snapshot.space_generation) != (
                self.snapshot.core_revision, self.snapshot.space_generation,
            ):
                raise VersionConflict('Ontology changed during ingestion')
            yield view._sql

    def types(self, view=None):
        """读取当前空间已保存的实体类型。

        Args:
            view: 可复用的读取视图。省略时打开新的版本校验快照。

        Returns:
            类型代码到类型记录的映射。
        """
        if view is not None:
            return {row.ref.record_id: domain(row) for row in view._sql.select(ENTITY_TYPE)}
        with self._read() as sql:
            return {row.ref.record_id: domain(row) for row in sql.select(ENTITY_TYPE)}

    def entities(self, names):
        """按规范化名称查找空间中的实体。

        Args:
            names: 要匹配的实体名称键。

        Returns:
            名称键到实体记录的映射。
        """
        if not names:
            return {}
        with self._read() as sql:
            return {name_key(entity.name): entity for entity in map(domain, sql.select(
                ENTITY, ('r.normalized_name = ANY(%s)',), (sorted(names),)))}

    def relation_named(self, predicate):
        """按谓词读取并缓存关系类型。

        Args:
            predicate: 要匹配的谓词。

        Returns:
            领域定义与存储记录。未找到时返回 None。
        """
        if predicate not in self._known:
            with self._read() as sql:
                rows = sql.select(RELATION_TYPE, ('r.predicate = %s',), (predicate,), limit=1)
                self._known[predicate] = (domain(rows[0]), rows[0]) if rows else None
        return self._known[predicate]

    def relations(self, meaning, left, right, limit):
        """检索与端点类型或含义有关的关系类型。

        Args:
            meaning: 待解析关系的含义。
            left: 主体实体类型。
            right: 对象实体类型。
            limit: 每路关系候选的数量上限。

        Returns:
            关系类型 ID 到领域定义和存储记录的映射。
        """
        from psycopg.types.json import Jsonb

        signatures = [[left, right]] + ([[right, left]] if right is not None and right != left else [])
        fits = '(' + ' OR '.join('r.endpoint_types @> %s' for _ in signatures) + ')'
        terms = sorted(bm25_query_terms(meaning))
        overlap = """(SELECT count(*) FROM unnest(%s::text[]) AS search(term)
            WHERE strpos(lower(replace(r.predicate, '_', ' ') || ' ' || r.meaning), search.term) > 0)"""
        with self._read() as sql:
            endpoints = tuple(Jsonb([pair]) for pair in signatures)
            typed = sql.select(RELATION_TYPE, (fits,), (*endpoints, terms),
                               order_by=f'{overlap} DESC, r.id', limit=limit)
            related = sql.select(RELATION_TYPE, (f'NOT {fits}', f'{overlap} > 0'), (*endpoints, terms, terms),
                                 order_by=f'{overlap} DESC, r.id', limit=limit) if terms else ()
            return {row.ref.record_id: (domain(row), row) for row in (*typed, *related)}
