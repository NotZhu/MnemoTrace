"""Request-local graph access for the existing graph retrieval algorithms."""
from ...retrieve.hybrid import _EXCLUSION_VALUE_PREDS
from ...retrieve.lexical import lexical_terms
from .projection import GraphProjection
from .records import ENTITY, SCOPE, domain


class RetrievalGraph:
    def __init__(self, view, query):
        self.view = view
        self.query = query
        self.sql = view._sql
        self.projection = GraphProjection(self.sql)
        self.entities = {}
        self._names = {}
        self._adjacent = {}
        self._canonical = {}
        self.excluded = set()
        self._edge_budget = 4 * query.facts.limit
        self._incoming = ()
        self.truncated = False
        terms = sorted(set(lexical_terms(query.text, query=True, exact=True)))
        settings = view.settings()
        self._self = settings.subject_name or 'user'
        aliases = (*settings.subject_aliases, self._self, 'user')
        records = self.sql.select(ENTITY, ('(r.anchor_terms && %s OR r.normalized_name = ANY(%s))',),
                                  (terms, [name.lower() for name in aliases]), limit=query.facts.limit + 1)
        self.truncated = len(records) > query.facts.limit
        self._remember(records[:query.facts.limit])

    def _remember(self, records):
        for record in records:
            entity = domain(record)
            self.entities[entity.id] = entity
            self._names[entity.name.lower()] = entity.id

    def _resolve(self, entity_id):
        if entity_id not in self._canonical:
            self._canonical[entity_id] = self.projection.canonical(entity_id)
        return self._canonical[entity_id]

    def _load_ids(self, ids):
        missing = set(ids) - self.entities.keys()
        if missing:
            self._remember(self.sql.select(ENTITY, ('r.id = ANY(%s)',), (sorted(missing),)))

    def find_entities(self, name):
        records = self.sql.select(ENTITY, ('(r.normalized_name = %s OR EXISTS '
                                  '(SELECT 1 FROM unnest(r.aliases) a WHERE lower(a) = %s))',),
                                  (name.lower(), name.lower()), limit=self.query.facts.limit)
        self._remember(records)
        ids = tuple(dict.fromkeys(self._resolve(record.ref.record_id) for record in records))
        self._load_ids(ids)
        return tuple(self.entities[entity_id] for entity_id in ids)

    def get_entity(self, user_id, name):
        if user_id != self.view.snapshot.context.user_id:
            return None
        # The account ID is not an entity name. Legacy algorithms pass it when
        # looking up a self-reference, which is represented by the domain subject.
        key = self._self.lower() if name == user_id else name.lower()
        if key not in self._names:
            self._names[key] = self._unique_entity_id(key)
        entity_id = self._names[key]
        if entity_id is None:
            return None
        canonical = self._resolve(entity_id)
        self._load_ids((canonical,))
        return self.entities.get(canonical)

    def resolve_mention_alias(self, name):
        # Resolve against the full space even when the candidate budget omitted a matching entity.
        return self.get_entity(self.view.snapshot.context.user_id, name)

    def _unique_entity_id(self, name):
        # Deduplicate merge targets before checking ambiguity, independent of traversal limits.
        rows = self.sql.run(f"""WITH RECURSIVE matches(id, exact) AS (
            SELECT id::text,normalized_name = %s FROM mnemotrace.entities
            WHERE {SCOPE} AND (normalized_name = %s OR EXISTS
                (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = %s))
        ), resolved(id) AS (
            SELECT id FROM matches WHERE exact OR NOT EXISTS (SELECT 1 FROM matches WHERE exact)
            UNION
            SELECT m.survivor_id::text FROM mnemotrace.entity_merges m JOIN resolved r ON m.merged_id = r.id
            WHERE m.user_id = %s AND m.space_id = %s
        )
        SELECT r.id FROM resolved r WHERE NOT EXISTS (
            SELECT 1 FROM mnemotrace.entity_merges m WHERE m.user_id = %s AND m.space_id = %s AND m.merged_id = r.id
        ) LIMIT 2""", (name, *self.sql.scope, name, name, *self.sql.scope, *self.sql.scope)).fetchall()
        return rows[0][0] if len(rows) == 1 else None

    def _edges(self, ids, direction, limit, predicates=(), *, excluded=()):
        records = self.view.graph_edges(ids, self.query.facts, direction=direction,
                                        predicates=predicates, excluded=excluded, limit=limit + 1)
        self.truncated |= len(records) > limit
        relations = tuple(map(domain, records[:limit]))
        self._load_ids(entity_id for rel in relations for entity_id in (rel.subject_id, rel.object_id))
        return relations

    def neighbors(self, entity_id, as_of=None, direction='out'):
        # All time filtering is performed against the associated fact in SQL.
        key = (self._resolve(entity_id), direction)
        if key not in self._adjacent:
            limit = min(self._edge_budget, self.query.facts.limit)
            edges = self._edges((key[0],), direction, limit, excluded=self.excluded)
            self._edge_budget -= len(edges)
            self._adjacent[key] = edges
        return tuple(edge for edge in self._adjacent[key]
                     if edge.subject_id not in self.excluded and edge.object_id not in self.excluded)

    def relations(self):
        return self._incoming

    def exclude(self, entity_ids):
        direct = {self._resolve(entity_id) for entity_id in entity_ids}
        # Resolve merge decisions before following incoming value relationships.
        self._incoming = self._edges(tuple(sorted(direct)), 'in', self.query.facts.limit, _EXCLUSION_VALUE_PREDS)
        self.excluded = direct | {edge.subject_id for edge in self._incoming}
        return self.excluded

    def excluded_names(self):
        """Resolve names of excluded entities for facts that have no projected edge."""
        if not self.excluded:
            return ()
        rows = self.sql.run("""WITH RECURSIVE members(id) AS (
            SELECT unnest(%s::text[])
            UNION
            SELECT m.merged_id FROM mnemotrace.entity_merges m JOIN members x ON x.id = m.survivor_id
            WHERE m.user_id = %s AND m.space_id = %s
        )
        SELECT e.normalized_name,e.aliases FROM mnemotrace.entities e JOIN members x ON x.id = e.id
        WHERE e.user_id = %s AND e.space_id = %s""",
            (sorted(self.excluded), *self.sql.scope, *self.sql.scope)).fetchall()
        names = {name for name, _ in rows}
        account = self.view.snapshot.context.user_id
        for name in {alias.lower() for _, aliases in rows for alias in aliases} - names:
            match = self.get_entity(account, name)
            if match is not None and match.id in self.excluded:
                names.add(name)
        if self._self.lower() in names:
            names.add(account.lower())
        return names

    def limitations(self):
        flags = ['graph_candidate_budget'] if self.truncated else []
        if self.sql.run(f'SELECT EXISTS(SELECT 1 FROM mnemotrace.entities WHERE {SCOPE} AND anchor_terms IS NULL)',
                        self.sql.scope).fetchone()[0]:
            flags.append('graph_backfill_pending')
        return tuple(flags)
