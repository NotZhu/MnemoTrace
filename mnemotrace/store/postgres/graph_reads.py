"""Request-local graph access for the existing graph retrieval algorithms."""
from ...retrieve.lexical import lexical_terms
from .records import ENTITY, SCOPE, domain


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
        records = self.sql.select(ENTITY, ('r.anchor_terms && %s',),
                                  (terms,), limit=query.facts.limit + 1) if terms else ()
        self.truncated = len(records) > query.facts.limit
        self._remember(records[:query.facts.limit])

    def _remember(self, records):
        for record in records:
            entity = domain(record)
            self.entities[entity.id] = entity

    def _load_ids(self, ids):
        missing = set(ids) - self.entities.keys()
        if missing:
            self._remember(self.sql.select(ENTITY, ('r.id = ANY(%s)',), (sorted(missing),)))

    def find_entities(self, name, *, limit=None):
        records = self.sql.select(ENTITY, ('r.normalized_name = %s',),
                                  (name.lower(),), limit=self.query.facts.limit if limit is None else limit)
        self._remember(records)
        return tuple(self.entities[record.ref.record_id] for record in records)

    def get_entity(self, user_id, name):
        if user_id != self.view.snapshot.context.user_id:
            return None
        key = name.lower()
        if key not in self._by_name:
            records = self.find_entities(key, limit=2)
            self._by_name[key] = records[0] if len(records) == 1 else None
        return self._by_name[key]

    def _edges(self, ids, direction, limit, predicates=()):
        records = self.view.graph_edges(ids, self.query.facts, direction=direction,
                                        predicates=predicates, limit=limit + 1)
        self.truncated |= len(records) > limit
        relations = tuple(map(domain, records[:limit]))
        self._load_ids(entity_id for rel in relations for entity_id in (rel.subject_id, rel.object_id))
        return relations

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
        flags = ['graph_candidate_budget'] if self.truncated else []
        if self.sql.run(f'SELECT EXISTS(SELECT 1 FROM mnemotrace.entities WHERE {SCOPE} AND anchor_terms IS NULL)',
                        self.sql.scope).fetchone()[0]:
            flags.append('graph_backfill_pending')
        return tuple(flags)
