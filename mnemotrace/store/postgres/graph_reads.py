"""图读取器，用于从数据库中检索图数据。"""
from dataclasses import replace

from ...retrieve.lexical import lexical_terms
from ...util import name_key
from .reads import PUBLIC_NAME_EVIDENCE
from .records import ENTITY, domain


class RetrievalGraph:
    """按查询范围和候选预算读取 PostgreSQL 实体图。"""
    def __init__(self, view, query):
        """从查询中定位候选实体并初始化图边预算。

        Args:
            view: 当前空间的读取快照。
            query: 查询文本及其事实筛选条件。
        """
        self.view = view
        self.query = query
        self.sql = view._sql
        self.entities = {}
        self._by_name = {}
        self._adjacent = {}
        self._edge_budget = 4 * query.facts.limit
        self.truncated = False
        terms = sorted(set(lexical_terms(query.text, query=True, exact=True)))
        conditions = ['r.anchor_terms && %s']
        if query.text.strip() != '我':
            conditions.append("r.normalized_name <> '我'")
        if query.facts.exclude_sensitive:
            conditions.append(PUBLIC_NAME_EVIDENCE)
        records = self.sql.select(ENTITY, conditions, (terms,), limit=query.facts.limit + 1) if terms else ()
        self.truncated = len(records) > query.facts.limit
        self._remember(records[:query.facts.limit])

    def _remember(self, records):
        """缓存已载入的实体，并按脱敏设置替换展示名称。

        Args:
            records: 已载入的实体记录。
        """
        names = (self.view.public_entity_names([record.ref.record_id for record in records])
                 if self.query.facts.exclude_sensitive else {})
        for record in records:
            entity = domain(record)
            if self.query.facts.exclude_sensitive:
                entity = replace(entity, name=names.get(entity.id, ''))
            self.entities[entity.id] = entity

    def _load_ids(self, ids):
        """补载尚未放入请求缓存的实体记录。

        Args:
            ids: 待补载的实体 ID。
        """
        missing = set(ids) - self.entities.keys()
        if missing:
            self._remember(self.sql.select(ENTITY, ('r.id = ANY(%s)',), (sorted(missing),)))

    def get_entity(self, user_id, name):
        """在当前快照中按名称查找实体。

        Args:
            user_id: 目标用户 ID，须与快照一致。
            name: 实体名称。

        Returns:
            查询可见的实体。无匹配时返回 None。
        """
        if user_id != self.view.snapshot.context.user_id:
            return None
        key = name_key(name)
        if key not in self._by_name:
            record = self.view.entity(key, share_safe=self.query.facts.exclude_sensitive)
            if record is not None:
                self._remember((record,))
            self._by_name[key] = self.entities[record.ref.record_id] if record else None
        return self._by_name[key]

    def _edges(self, ids, direction, limit, predicates=()):
        """读取指定方向的图边并补载两端实体。

        Args:
            ids: 起始实体 ID。
            direction: 图边方向。
            limit: 返回图边数量上限。
            predicates: 可选的谓词过滤条件。

        Returns:
            按遍历方向调整后的关系记录。
        """
        records = self.view.graph_edges(ids, self.query.facts, direction=direction,
                                        predicates=predicates, limit=limit + 1)
        self.truncated |= len(records) > limit
        relations = []
        for edge in map(domain, records[:limit]):
            if (direction == 'out' and edge.subject_id not in ids or
                    direction == 'in' and edge.object_id not in ids):
                edge = replace(edge, subject_id=edge.object_id, object_id=edge.subject_id)
            relations.append(edge)
        self._load_ids(entity_id for rel in relations for entity_id in (rel.subject_id, rel.object_id))
        return tuple(relations)

    def neighbors(self, entity_id, as_of=None, direction='out'):
        # All time filtering is performed against the associated fact in SQL.
        """在请求的剩余预算内读取实体邻接边。

        Args:
            entity_id: 起始实体 ID。
            as_of: 适配图接口的时间参数。实际时间筛选使用初始化时的查询。
            direction: 图边方向。

        Returns:
            满足当前查询的邻接关系。
        """
        key = (entity_id, direction)
        if key not in self._adjacent:
            limit = min(self._edge_budget, self.query.facts.limit)
            edges = self._edges((key[0],), direction, limit)
            self._edge_budget -= len(edges)
            self._adjacent[key] = edges
        return self._adjacent[key]

    def limitations(self):
        """返回图候选预算耗尽时的覆盖限制。"""
        return ('graph_candidate_budget',) if self.truncated else ()
