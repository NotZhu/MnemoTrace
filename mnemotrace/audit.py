"""Compare the authorized graph projection with its source facts."""
from collections import Counter

from .consolidate.graph_builder import projects_fact
from .store.postgres.records import ENTITY, FACT, RELATION_TYPE, domain, timestamp


_FINDINGS = {
    'missing_edge': ('Eligible fact has no graph edge.', 'Rebuild the graph projection.'),
    'edge_mismatch': ('Graph edge does not match its fact.', 'Rebuild the graph projection.'),
    'orphan_entity': ('Graph node has no supporting edge.', 'Rebuild and check the graph projection.'),
}


def graph_audit(view, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Expected an audit limit from 1 to 1000')
    counts = Counter()
    buckets = {kind: [] for kind in _FINDINGS}
    cursor, checked = None, 0
    definitions = {}

    def add(kind, **refs):
        counts[kind] += 1
        if len(buckets[kind]) < limit:
            why, action = _FINDINGS[kind]
            buckets[kind].append(dict(kind=kind, why=why, action=action, **refs))

    while records := view.records(FACT, after_id=cursor):
        cursor = records[-1].ref.record_id
        facts = [fact for record in records if not (fact := domain(record)).sensitive]
        checked += len(facts)
        if not facts:
            continue
        edges = view.audit_edges([fact.id for fact in facts])
        for fact in facts:
            edge = edges.get(fact.id)
            if fact.relation_id and fact.relation_id not in definitions:
                definitions[fact.relation_id] = domain(view.get(RELATION_TYPE, fact.relation_id))
            eligible = projects_fact(fact, definitions.get(fact.relation_id))
            if edge is None:
                if eligible:
                    add('missing_edge', fact_id=fact.id)
            elif not eligible or (
                edge['subject_id'], edge['predicate'], edge['object_id'], edge['valid_at'], edge['invalid_at']
            ) != (
                fact.subject_id, fact.predicate, fact.object_id,
                timestamp(fact.valid_at), timestamp(fact.invalid_at),
            ):
                add('edge_mismatch', fact_id=fact.id, edge_id=edge['id'])

    orphans, total_orphans = view.orphan_entities(limit)
    for row in orphans:
        add('orphan_entity', entity_id=row['id'])
    if total_orphans:
        counts['orphan_entity'] = total_orphans
    findings = [row for bucket in buckets.values() for row in bucket][:limit]
    total = counts.total()
    return dict(space_id=view.snapshot.context.space_id,
                checked=dict(facts=checked, entities=view.counts()[ENTITY]),
                total_findings=total, by_kind=dict(counts), findings=findings,
                truncated=total > len(findings))
