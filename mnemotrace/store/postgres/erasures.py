"""Scope explicit erasure and clean up database references in the same commit."""
from collections import defaultdict

from ..contracts import RecordRef
from .errors import ErasureDependencies
from .records import ENTITY, FACT, SOURCE, WORKING, SCOPE, TABLES, TARGET_COLUMNS


def erase(sql, graph, refs):
    groups = defaultdict(set)
    for ref in refs:
        groups[ref.kind].add(ref.record_id)
    sources = sorted(groups[SOURCE])
    dependencies = []
    if sources:
        for kind, table, column in (
            (FACT, 'fact_sources', 'fact_id'), (WORKING, 'working_sources', 'working_id'),
        ):
            rows = sql.run(f"""SELECT {column}, bool_and(source_id = ANY(%s))
                FROM mnemotrace.{table} WHERE {SCOPE}
                AND {column} IN (SELECT {column} FROM mnemotrace.{table} WHERE {SCOPE}
                                  AND source_id = ANY(%s))
                GROUP BY {column} ORDER BY {column}""",
                (sources, *sql.scope, *sql.scope, sources)).fetchall()
            for record_id, wholly_derived in rows:
                if wholly_derived:
                    groups[kind].add(record_id)
                elif record_id not in groups[kind]:
                    dependencies.append(RecordRef(sql.context, kind, record_id))
    if dependencies:
        raise ErasureDependencies(dependencies)

    # Removing a version does not remove its surviving successors. Drop the erased link.
    fact_ids = sorted(groups[FACT])
    if fact_ids:
        sql.run(f'UPDATE mnemotrace.facts SET supersedes = NULL, record_version = record_version + 1 '
                f'WHERE {SCOPE} AND supersedes = ANY(%s) AND NOT (id = ANY(%s))',
                (*sql.scope, fact_ids, fact_ids))
        graph.remove_fact_edges(fact_ids)
    entities = sorted(groups[ENTITY])
    if entities:
        rows = sql.run(f'SELECT id FROM mnemotrace.graph_relations WHERE {SCOPE}'
                       ' AND (subject_id = ANY(%s) OR object_id = ANY(%s))',
                       (*sql.scope, entities, entities)).fetchall()
        if rows:
            from .records import RELATION
            missing = [RecordRef(sql.context, RELATION, row[0]) for row in rows
                       if row[0] not in groups[RELATION]]
            if missing:
                raise ErasureDependencies(missing)
    if sources:
        sql.delete('operations', f'operation_id IN (SELECT operation_id FROM mnemotrace.operation_sources '
                   f'WHERE {SCOPE} AND source_id = ANY(%s))', (*sql.scope, sources))
    for kind, column in TARGET_COLUMNS.items():
        if groups[kind]:
            sql.delete('operations', f'operation_id IN (SELECT operation_id FROM mnemotrace.operation_targets '
                       f'WHERE {SCOPE} AND {column} = ANY(%s))', (*sql.scope, sorted(groups[kind])))
    if sources or fact_ids or groups[WORKING]:
        sql.delete('record_commit_receipts',
                   'source_ids && %s OR fact_ids && %s OR working_ids && %s',
                   (sources, fact_ids, sorted(groups[WORKING])))
        # Task receipts contain ID references even when the input comes from a different source.
        sql.delete('operation_receipts', 'fact_ids && %s OR working_ids && %s',
                   (fact_ids, sorted(groups[WORKING])))
    count = 0
    for kind, ids in groups.items():
        if ids:
            count += sql.delete(TABLES[kind], 'id = ANY(%s)', (sorted(ids),)).rowcount
    return count


def clear_session(sql, session_id):
    sql.ensure_session(session_id)
    sql.run(f'UPDATE mnemotrace.sessions SET session_generation = session_generation + 1 '
            f'WHERE {SCOPE} AND session_id = %s', (*sql.scope, session_id))
    sql.delete('operation_products', f'operation_id IN (SELECT operation_id FROM mnemotrace.operations '
               f'WHERE {SCOPE} AND session_id = %s)', (*sql.scope, session_id))
    sql.run(f"""UPDATE mnemotrace.operations
        SET state = 'cancelled', error_code = 'session_invalidated', lease_token = NULL,
            lease_until = NULL, next_attempt_at = NULL, extraction_saved = false, phase = 'queued'
        WHERE {SCOPE} AND session_id = %s AND state <> 'completed'""",
        (*sql.scope, session_id))
    ids = [row[0] for row in sql.run(
        f'SELECT id FROM mnemotrace.working_memories WHERE {SCOPE} AND session_id = %s',
        (*sql.scope, session_id)).fetchall()]
    if ids:
        sql.delete('record_commit_receipts', 'working_ids && %s', (ids,))
        sql.delete('operation_receipts', 'working_ids && %s', (ids,))
        sql.delete('operations', 'session_id IS NULL AND operation_id IN '
                   f'(SELECT operation_id FROM mnemotrace.operation_targets WHERE {SCOPE} AND working_id = ANY(%s))',
                   (*sql.scope, ids))
        sql.delete('working_memories', 'session_id = %s', (session_id,))
    return len(ids)


def clear_space(sql):
    # Sources and work items cascade from sessions; facts and entities are space-owned.
    for table in ('record_commit_receipts', 'operations', 'facts', 'sessions', 'entities', 'import_receipts'):
        sql.delete(table)
    sql.run(f'UPDATE mnemotrace.memory_spaces SET space_generation = space_generation + 1 WHERE {SCOPE}',
            sql.scope)
