"""Mapping between scoped domain records and the PostgreSQL schema."""
from dataclasses import fields
from datetime import datetime, timezone
import json

from ...types import Conflict, Entity, Episode, Fact, Relation, WorkingMemory
from ...util import stems, tokenize
from ..contracts import RecordKind, RecordRef, StoredRecord
from .database import identifier
from .errors import DataCorruption

SOURCE, FACT, WORKING, ENTITY, RELATION, CONFLICT = RecordKind
MODELS = dict(zip(RecordKind, (Episode, Fact, WorkingMemory, Entity, Relation, Conflict), strict=True))
TABLES = dict(zip(RecordKind, (
    'sources', 'facts', 'working_memories', 'entities', 'graph_relations', 'conflicts',
), strict=True))
TARGET_COLUMNS = {SOURCE: 'source_id', FACT: 'fact_id', WORKING: 'working_id', ENTITY: 'entity_id'}
TIMES = frozenset(('event_time', 'ingested_at', 'created_at', 'valid_at', 'invalid_at',
                   'expired_at', 'last_access', 'expires_at', 'detected_at'))
SCOPE = 'user_id = %s AND space_id = %s'
ROW_SCOPE = 'r.user_id = %s AND r.space_id = %s'
JSON_COLUMNS = {'metadata', 'coverage', 'summary_coverage', 'settings', 'stats',
                'input', 'result', 'source_versions'}


def anchor_terms(name, aliases):
    return sorted({term for label in (name, *aliases) for term in (*stems(label), *tokenize(label))})


def timestamp(value):
    try:
        return None if value is None else datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError('Timestamp is outside the supported range') from exc


def source_summary_join(known_at=None):
    join = """ LEFT JOIN mnemotrace.source_summaries s
        ON (s.user_id,s.space_id,s.source_id) = (r.user_id,r.space_id,r.id)"""
    if known_at is None:
        return join, ()
    return join + ' AND s.summary_written_at <= %s', (timestamp(known_at),)


def domain(record: StoredRecord):
    return MODELS[record.ref.kind](**json.loads(record.payload_json))


def unpack(context, kind, row):
    values = {}
    try:
        if (kind == SOURCE and row['summary_content_version'] is not None
                and row['summary_content_version'] != row['content_version']):
            raise ValueError('Source summary refers to an obsolete content version')
        for field in fields(MODELS[kind]):
            key = field.name
            value = row[key]
            if key in TIMES and value is not None:
                value = value.timestamp()
            values[key] = value
        return StoredRecord(
            RecordRef(context, kind, row['id']), row.get('content_version', 1),
            json.dumps(values, ensure_ascii=False, allow_nan=False),
            row.get('agent_id'), row.get('record_version', row.get('projection_version', 1)),
            row.get('summary_version'),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DataCorruption('Invalid stored domain record') from exc


def columns(record, values):
    kind = record.ref.kind
    excluded = {'id', 'user_id', 'embedding'}
    if kind == SOURCE:
        excluded.update(('summary', 'summary_embedding', 'coverage', 'summary_coverage', 'consolidated'))
    if kind == FACT:
        excluded.add('provenance')
    data = {key: timestamp(value) if key in TIMES else value
            for key, value in values.items() if key not in excluded}
    if kind == RELATION:
        data['projection_version'] = record.record_version
    else:
        data['record_version'] = record.record_version
        if kind != CONFLICT:
            data['content_version'] = record.content_version
    if kind == SOURCE:
        data['agent_id'] = record.agent_id
    if kind == ENTITY:
        data['normalized_name'] = values['name'].lower()
        data['authoritative'] = True
        data['anchor_terms'] = anchor_terms(values['name'], values['aliases'])
    if kind == FACT:
        data['subject_key'] = values['subject'].lower()
        data['predicate_key'] = values['predicate'].lower()
        data['subject_match_key'] = values['subject'].strip().lower()
    return data


class ScopedSQL:
    """Owns one connection for the duration of the caller's transaction."""

    def __init__(self, conn, context):
        self._conn = conn
        self.context = context
        self.scope = (context.user_id, context.space_id)

    @property
    def conn(self):
        if self._conn is None:
            raise RuntimeError('The database transaction has ended')
        return self._conn

    def close(self):
        self._conn = None

    def rows(self, query, params=()):
        from psycopg.rows import dict_row
        with self.conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(query, params).fetchall()

    def run(self, query, params=(), *, prepare=None):
        return self.conn.execute(query, params, prepare=prepare)

    def select(self, kind, conditions=(), params=(), *, limit=None, summary_known_at=None):
        extra, joins = '', ''
        join_params = ()
        if kind in TARGET_COLUMNS:
            extra += ', NULL AS embedding'
        if kind == FACT:
            extra += """, ARRAY(SELECT p.source_id::text FROM mnemotrace.fact_sources p
                WHERE (p.user_id,p.space_id,p.fact_id) = (r.user_id,r.space_id,r.id)
                ORDER BY p.source_order NULLS LAST, p.source_id) AS provenance"""
        elif kind == SOURCE:
            extra += """, coalesce(s.summary, '') AS summary, s.coverage, s.summary_coverage,
                CASE WHEN s.summary <> '' THEN s.summary_version END AS summary_version,
                coalesce(s.processing_state = 'completed', false) AS consolidated,
                s.content_version AS summary_content_version, NULL AS summary_embedding"""
            joins, join_params = source_summary_join(summary_known_at)
        where = ' AND '.join((ROW_SCOPE, *conditions))
        query = f'SELECT r.*{extra} FROM mnemotrace.{TABLES[kind]} r{joins} WHERE {where} ORDER BY r.id'
        if limit is not None:
            query += ' LIMIT %s'
            params = (*params, limit)
        return tuple(unpack(self.context, kind, row)
                     for row in self.rows(query, (*join_params, *self.scope, *params)))

    def get(self, kind, record_id):
        identifier(record_id)
        return next(iter(self.select(kind, ('r.id = %s',), (record_id,))), None)

    def insert(self, table, data, *, suffix=''):
        from psycopg.types.json import Jsonb
        names = ('user_id', 'space_id', *data)
        values = (*self.scope, *(Jsonb(v) if k in JSON_COLUMNS and v is not None else v
                                 for k, v in data.items()))
        placeholders = ', '.join(['%s'] * len(values))
        return self.run(f'INSERT INTO mnemotrace.{table} ({", ".join(names)}) '
                        f'VALUES ({placeholders}) {suffix}', values)

    def update(self, table, data, condition, params=()):
        from psycopg.types.json import Jsonb
        values = tuple(Jsonb(v) if k in JSON_COLUMNS and v is not None else v
                       for k, v in data.items())
        assignments = ', '.join(f'{key} = %s' for key in data)
        return self.run(f'UPDATE mnemotrace.{table} SET {assignments} WHERE {SCOPE} AND ({condition})',
                        (*values, *self.scope, *params))

    def delete(self, table, condition='true', params=()):
        return self.run(f'DELETE FROM mnemotrace.{table} WHERE {SCOPE} AND ({condition})',
                        (*self.scope, *params))

    def version(self, ref, *, summary=False):
        if summary:
            row = self.run(f'SELECT summary_version FROM mnemotrace.source_summaries WHERE {SCOPE}'
                           " AND source_id = %s AND summary <> ''", (*self.scope, ref.record_id)).fetchone()
        else:
            row = self.run(f'SELECT content_version FROM mnemotrace.{TABLES[ref.kind]} WHERE {SCOPE}'
                           ' AND id = %s', (*self.scope, ref.record_id)).fetchone()
        return row[0] if row else None

    def ensure_session(self, session_id):
        self.insert('sessions', {'session_id': session_id}, suffix='ON CONFLICT DO NOTHING')

    def write_record(self, record, *, insert):
        values = json.loads(record.payload_json)
        data = columns(record, values)
        table, record_id = TABLES[record.ref.kind], record.ref.record_id
        if insert:
            self.insert(table, {'id': record_id, **data})
        else:
            self.update(table, data, 'id = %s', (record_id,))
        return values
