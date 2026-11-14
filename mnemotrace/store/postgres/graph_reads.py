"""图读取器，用于从数据库中检索图数据。"""
from dataclasses import replace

from ...retrieve.lexical import lexical_terms
from ...util import name_key
from .reads import PUBLIC_NAME_EVIDENCE
from .records import ENTITY, domain


class RetrievalGraph:
    def __init__(self, view, query):
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
        names = (self.view.public_entity_names([record.ref.record_id for record in records])
                 if self.query.facts.exclude_sensitive else {})
        for record in records:
            entity = domain(record)
            if self.query.facts.exclude_sensitive:
                entity = replace(entity, name=names.get(entity.id, ''))
            self.entities[entity.id] = entity

    def _load_ids(self, ids):
        missing = set(ids) - self.entities.keys()
        if missing:
            self._remember(self.sql.select(ENTITY, ('r.id = ANY(%s)',), (sorted(missing),)))

    def get_entity(self, user_id, name):
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
        key = (entity_id, direction)
        if key not in self._adjacent:
            limit = min(self._edge_budget, self.query.facts.limit)
            edges = self._edges((key[0],), direction, limit)
            self._edge_budget -= len(edges)
            self._adjacent[key] = edges
        return self._adjacent[key]

    def limitations(self):
        return ('graph_candidate_budget',) if self.truncated else ()
