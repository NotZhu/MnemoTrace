from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from mnemotrace.types import Fact
from mnemotrace.store.postgres.errors import DatabaseConstraintError


def fact(conn, user, space, record_id='fact-1', **changes):
    from psycopg import sql
    from psycopg.types.json import Jsonb

    fields = asdict(Fact('Synthetic user', 'likes', 'tea', id=record_id))
    for key in ('embedding', 'provenance', 'user_id'):
        fields.pop(key)
    fields.update(user_id=user, space_id=space, content_version=1, record_version=1, **changes)
    for key in ('valid_at', 'invalid_at', 'created_at', 'expired_at', 'last_access'):
        if fields[key] is not None:
            fields[key] = datetime.fromtimestamp(fields[key], timezone.utc)
    fields["validity_revisions"] = Jsonb(fields["validity_revisions"])
    conn.execute(sql.SQL('INSERT INTO mnemotrace.facts ({}) VALUES ({})').format(
        sql.SQL(',').join(map(sql.Identifier, fields)),
        sql.SQL(',').join(sql.Placeholder() for _ in fields)), tuple(fields.values()))


def source(conn, user, space, record_id='source-1', message_id=None):
    conn.execute('INSERT INTO mnemotrace.sessions(user_id,space_id,session_id) VALUES (%s,%s,%s) '
                 'ON CONFLICT DO NOTHING', (user, space, 'thread'))
    conn.execute(
        'INSERT INTO mnemotrace.sources(user_id,space_id,id,content,session_id,role,ingested_at,'
        'source_kind,scope,message_id,sequence,mode,message_fingerprint,metadata,record_version,content_version) '
        "VALUES (%s,%s,%s,'Synthetic input','thread','user',now(),'dialogue','auto',%s,0,'live',%s,'{}',1,1)",
        (user, space, record_id, message_id, 'a' * 64 if message_id else None))


def test_foreign_keys_reject_cross_space_and_orphan_history(runtime, spaces, user):
    a, b = spaces.create(user, 'A'), spaces.create(user, 'B')
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        fact(conn, user, a.space_id)
        source(conn, user, a.space_id)
    with runtime.space_transaction(user, b.space_id) as (conn, _):
        fact(conn, user, b.space_id)
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, b.space_id) as (conn, _):
        conn.execute('INSERT INTO mnemotrace.fact_sources VALUES (%s,%s,%s,%s)',
                     (user, b.space_id, 'fact-1', 'source-1'))
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, b.space_id) as (conn, _):
        fact(conn, user, b.space_id, 'orphan', supersedes='missing')


def test_duplicate_message_identifier_is_rejected(runtime, spaces, user):
    a = spaces.create(user, 'A')
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        source(conn, user, a.space_id, message_id='message-1')
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, a.space_id) as (conn, _):
        source(conn, user, a.space_id, 'other-source', message_id='message-1')


@pytest.mark.parametrize('values', [
    ('fact-1', None, 2, '[1,2,3]', 'Fact'),
    ('fact-1', 'source-1', 3, '[1,2,3]', 'Fact'),
    ('missing', None, 3, '[1,2,3]', 'Fact'),
    ('fact-1', None, 3, '[1,2,3]', 'Episode'),
    (None, None, 3, '[1,2,3]', 'Fact'),
])
def test_embedding_target_and_dimension_constraints(runtime, spaces, user, values):
    a = spaces.create(user, 'A')
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        fact(conn, user, a.space_id)
        source(conn, user, a.space_id)
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, a.space_id) as (conn, _):
        conn.execute('INSERT INTO mnemotrace.embeddings(user_id,space_id,fact_id,source_id,dimension,vector,'
                     'target_kind,field,model,content_version) VALUES (%s,%s,%s,%s,%s,%s,%s,\'text\',\'synthetic\',1)',
                     (user, a.space_id, *values))


def test_vector_and_lexical_unique_targets(runtime, spaces, user):
    a = spaces.create(user, 'A')
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        fact(conn, user, a.space_id)
        conn.execute('INSERT INTO mnemotrace.embeddings(user_id,space_id,fact_id,dimension,vector,target_kind,'
                     "field,model,content_version) VALUES (%s,%s,'fact-1',3,'[1,2,3]','Fact','text','synthetic',1)",
                     (user, a.space_id))
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, a.space_id) as (conn, _):
        conn.execute('INSERT INTO mnemotrace.embeddings SELECT * FROM mnemotrace.embeddings WHERE space_id=%s',
                     (a.space_id,))


def test_account_deletion_cleans_sources_facts_derived_data(accounts, runtime, spaces, user):
    a = spaces.create(user, 'A')
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        fact(conn, user, a.space_id)
        fact(conn, user, a.space_id, 'fact-2', supersedes='fact-1')
        source(conn, user, a.space_id)
        conn.execute('INSERT INTO mnemotrace.fact_sources VALUES (%s,%s,%s,%s)',
                     (user, a.space_id, 'fact-1', 'source-1'))
        conn.execute('INSERT INTO mnemotrace.embeddings(user_id,space_id,fact_id,dimension,vector,target_kind,'
                     "field,model,content_version) VALUES (%s,%s,'fact-1',3,'[1,2,3]','Fact','text','synthetic',1)",
                     (user, a.space_id))
    assert accounts.delete_user(user)
    with runtime._transaction() as conn:
        conn.execute("SELECT set_config('mnemotrace.user_id',%s,true)", (user,))
        for table in ('sources', 'facts', 'embeddings', 'fact_sources'):
            from psycopg import sql
            assert conn.execute(sql.SQL('SELECT count(*) FROM mnemotrace.{}').format(
                sql.Identifier(table))).fetchone() == (0,)


def test_task_references_and_required_stats(runtime, spaces, user):
    from psycopg.types.json import Jsonb
    stats = dict.fromkeys(('facts_added','duplicates','invalidated','working_added','model_calls','windows_completed'), 0)
    a, b = spaces.create(user, 'A'), spaces.create(user, 'B')
    def operation(conn, space, counters):
        conn.execute('INSERT INTO mnemotrace.operations(user_id,space_id,operation_id,kind,state,phase,attempts,'
                     'embed_attempts,space_generation,stats,stored_raw,extraction_saved,core_committed) '
                     "VALUES (%s,%s,'operation-1','extract','pending','queued',0,0,0,%s,true,false,false)",
                     (user, space, Jsonb(counters)))
    with runtime.space_transaction(user, a.space_id) as (conn, _):
        source(conn, user, a.space_id)
        operation(conn, a.space_id, stats)
    with runtime.space_transaction(user, b.space_id) as (conn, _):
        operation(conn, b.space_id, stats)
    for invalid in ({}, {**stats, 'model_calls': -1}, {**stats, 'model_calls': True},
                    {**stats, 'model_calls': None}, {**stats, 'model_calls': 1.5}):
        with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, a.space_id) as (conn, _):
            conn.execute('UPDATE mnemotrace.operations SET stats=%s WHERE space_id=%s', (Jsonb(invalid), a.space_id))
    with pytest.raises(DatabaseConstraintError), runtime.space_transaction(user, b.space_id) as (conn, _):
        conn.execute('INSERT INTO mnemotrace.operation_sources(user_id,space_id,operation_id,source_id,'
                     "content_version,input_start,input_end,input_order) VALUES (%s,%s,'operation-1','source-1',1,0,1,0)",
                     (user, b.space_id))
