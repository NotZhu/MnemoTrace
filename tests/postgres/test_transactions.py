from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
import time

import pytest

from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.errors import (
    DatabaseError, DatabaseUnavailable, IdentityRequired,
)
from mnemotrace.store.postgres.migrate import _roles


def test_pool_reuse_clears_identity_after_commit_and_rollback(runtime, spaces, user, accounts):
    other = accounts.create_user()
    try:
        first = spaces.create(user, 'First')
        second = spaces.create(other, 'Second')
        with runtime.user_transaction(user) as conn:
            backend = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
            assert conn.execute('SELECT space_id FROM mnemotrace.memory_spaces').fetchall() == [(first.space_id,)]
        with runtime._transaction() as conn:
            assert conn.execute('SELECT pg_backend_pid()').fetchone()[0] == backend
            assert conn.execute('SELECT space_id FROM mnemotrace.memory_spaces').fetchall() == []
        with pytest.raises(ValueError), runtime.user_transaction(user) as conn:
            conn.execute('UPDATE mnemotrace.memory_spaces SET name=%s', ('Rolled back',))
            raise ValueError('Synthetic rollback')
        assert spaces.list(user)[0].name == 'First'
        with runtime._transaction() as conn:
            assert conn.execute("SELECT nullif(current_setting('mnemotrace.user_id',true),'')").fetchone() == (None,)
        with runtime.user_transaction(other, read_only=True) as conn:
            assert conn.execute('SELECT space_id FROM mnemotrace.memory_spaces').fetchall() == [(second.space_id,)]
        with pytest.raises(IdentityRequired), runtime.user_transaction(''):
            pass
    finally:
        accounts.delete_user(other)


def test_rls_rejects_insert_update_delete_of_other_user(runtime, spaces, user, accounts):
    other = accounts.create_user()
    try:
        space = spaces.create(other, 'Other')
        with runtime.user_transaction(user) as conn:
            assert conn.execute('UPDATE mnemotrace.memory_spaces SET name=%s WHERE space_id=%s',
                                ('Stolen', space.space_id)).rowcount == 0
            assert conn.execute('DELETE FROM mnemotrace.memory_spaces WHERE space_id=%s',
                                (space.space_id,)).rowcount == 0
        with pytest.raises(DatabaseError), runtime.user_transaction(user) as conn:
            conn.execute("INSERT INTO mnemotrace.memory_spaces(space_id,user_id,name,status,settings) "
                         "VALUES ('forged',%s,'Forged','active','{}')", (other,))
        assert spaces.list(other)[0].name == 'Other'
    finally:
        accounts.delete_user(other)


@pytest.mark.parametrize('statement', [
    'SELECT * FROM mnemotrace.api_keys',
    "INSERT INTO mnemotrace.users(user_id,status) VALUES ('forged','active')",
    'TRUNCATE mnemotrace.memory_spaces',
    'CREATE TABLE mnemotrace.forbidden(id text)',
    'ALTER TABLE mnemotrace.sources DISABLE ROW LEVEL SECURITY',
    "UPDATE mnemotrace.users SET status='deleting'",
])
def test_runtime_cannot_administer_database(runtime, user, statement):
    with pytest.raises(DatabaseError), runtime.user_transaction(user) as conn:
        conn.execute(statement)


def test_runtime_cannot_set_role(cluster, runtime, user):
    from psycopg import sql
    for purpose in ('migration', 'admin', 'auth'):
        with pytest.raises(DatabaseError), runtime.user_transaction(user) as conn:
            conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(_roles(cluster.runtime.role_prefix)[purpose])))


def test_repeatable_read_keeps_multitable_snapshot(cluster, runtime, spaces, user):
    space = spaces.create(user, 'Before')
    with Database(cluster.runtime) as writer:
        with runtime.space_transaction(user, space.space_id, read_only=True) as (conn, _):
            assert conn.execute('SHOW transaction_isolation').fetchone() == ('repeatable read',)
            assert conn.execute('SHOW transaction_read_only').fetchone() == ('on',)
            assert conn.execute('SELECT core_revision FROM mnemotrace.memory_spaces WHERE space_id=%s',
                                (space.space_id,)).fetchone() == (0,)
            with writer.space_transaction(user, space.space_id) as (other, _):
                other.execute('UPDATE mnemotrace.memory_spaces SET name=%s,core_revision=1 WHERE space_id=%s',
                              ('After', space.space_id))
                other.execute('INSERT INTO mnemotrace.sessions(user_id,space_id,session_id) VALUES (%s,%s,%s)',
                              (user, space.space_id, 'Synthetic thread'))
            assert conn.execute('SELECT name,core_revision FROM mnemotrace.memory_spaces WHERE space_id=%s',
                                (space.space_id,)).fetchone() == ('Before', 0)
            assert conn.execute('SELECT session_id FROM mnemotrace.sessions WHERE space_id=%s',
                                (space.space_id,)).fetchall() == []
    assert spaces.list(user)[0].name == 'After'


def test_pool_timeout_is_sanitized(cluster, user):
    config = replace(cluster.runtime, pool_timeout=0.1)
    with Database(config) as database:
        with database.user_transaction(user):
            with pytest.raises(DatabaseUnavailable) as caught:
                with database.user_transaction(user):
                    pass
            assert config.dsn not in str(caught.value)


def test_dead_connection_is_replaced(cluster, runtime, user):
    psycopg, _ = driver()
    with runtime.user_transaction(user) as conn:
        backend = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
    with psycopg.connect(cluster.bootstrap.dsn) as admin:
        admin.execute('SELECT pg_terminate_backend(%s)', (backend,))
    with runtime.user_transaction(user) as conn:
        assert conn.execute('SELECT pg_backend_pid()').fetchone()[0] != backend


def test_account_delete_waits_for_inflight_space_creation(cluster, accounts, user, runtime):
    entered, proceed = Event(), Event()
    def create():
        with runtime.user_transaction(user) as conn:
            entered.set()
            assert proceed.wait(10)
            conn.execute("INSERT INTO mnemotrace.memory_spaces(space_id,user_id,name,status,settings) "
                         "VALUES ('concurrent-space',%s,'Synthetic','active','{}')", (user,))
    with ThreadPoolExecutor(max_workers=2) as executor:
        creating = executor.submit(create)
        assert entered.wait(10)
        with accounts.database.admin_transaction() as conn:
            deleting_pid = conn.execute('SELECT pg_backend_pid()').fetchone()[0]
        deleting = executor.submit(accounts.delete_user, user)
        try:
            psycopg, _ = driver()
            with psycopg.connect(cluster.bootstrap.dsn, autocommit=True) as root:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    waiting = root.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',
                                           (deleting_pid,)).fetchone()
                    if waiting == ('Lock',):
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail('Account deletion did not wait for the inflight user transaction')
            assert not deleting.done()
        finally:
            proceed.set()
        creating.result(timeout=10)
        assert deleting.result(timeout=10)
    with runtime._transaction() as conn:
        conn.execute("SELECT set_config('mnemotrace.user_id',%s,true)", (user,))
        assert conn.execute('SELECT * FROM mnemotrace.memory_spaces').fetchall() == []


def test_different_users_can_write_while_another_user_transaction_is_open(cluster, runtime, user, accounts):
    from mnemotrace.store.postgres import Spaces
    from psycopg.conninfo import make_conninfo
    other = accounts.create_user()
    config = replace(cluster.runtime, dsn=make_conninfo(cluster.runtime.dsn, options='-c statement_timeout=1000'))
    try:
        with Database(config) as writer, runtime.user_transaction(user):
            assert Spaces(writer).create(other, 'Concurrent other user').user_id == other
    finally:
        accounts.delete_user(other)


def test_invalid_foreign_user_write_without_identity(runtime):
    with pytest.raises(DatabaseError), runtime._transaction() as conn:
        conn.execute("INSERT INTO mnemotrace.memory_spaces(space_id,user_id,name,status,settings) "
                     "VALUES ('forged','unknown','Synthetic','active','{}')")


def test_unavailable_database_and_open_failure_are_sanitized(cluster, monkeypatch):
    import socket
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import ConnectionPool, PoolTimeout
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        unavailable = replace(cluster.runtime, dsn=make_conninfo(
            cluster.runtime.dsn, host='127.0.0.1', port=reserved.getsockname()[1]), connect_timeout=1)
        with pytest.raises(DatabaseUnavailable) as caught:
            Database(unavailable).open()
        assert unavailable.dsn not in str(caught.value)
    attempted = []
    def interrupted(pool, **_):
        attempted.append(pool)
        raise PoolTimeout('Synthetic interrupted pool initialization')
    monkeypatch.setattr(ConnectionPool, 'open', interrupted)
    with pytest.raises(DatabaseUnavailable):
        Database(cluster.runtime).open()
    assert len(attempted) == 1 and attempted[0].closed


def test_disconnect_during_schema_check_is_unavailable_not_corruption(cluster):
    from mnemotrace.store.postgres.database import database_errors
    from mnemotrace.store.postgres.migrate import check_schema
    psycopg, _ = driver()
    with psycopg.connect(cluster.runtime.dsn) as conn:
        with psycopg.connect(cluster.bootstrap.dsn) as root:
            root.execute('SELECT pg_terminate_backend(%s)', (conn.info.backend_pid,))
        with pytest.raises(DatabaseUnavailable), database_errors():
            check_schema(conn, cluster.runtime.role_prefix)
