from dataclasses import replace
import hashlib

import pytest

from mnemotrace.store.postgres import Accounts, Database, Spaces
from mnemotrace.store.postgres.errors import (
    AccountNotFound, AuthenticationError, DataCorruption, DatabaseConfigurationError,
    KeyNotFound, SpaceNotFound,
)


def test_key_lifecycle_and_rollback(accounts, user, spaces, monkeypatch):
    first = accounts.issue_key(user, 'first')
    second = accounts.issue_key(user, 'second')
    assert first.value not in repr(first)
    assert spaces.authenticate(first.value).user_id == user
    assert spaces.authenticate(second.value).user_id == user
    assert len(accounts.list_keys(user)) == 2
    original = accounts._issue
    def fail(*_):
        raise ValueError('Synthetic interrupted rotation')
    monkeypatch.setattr(accounts, '_issue', fail)
    with pytest.raises(ValueError):
        accounts.rotate_key(user, first.info.key_id)
    assert spaces.authenticate(first.value).user_id == user
    monkeypatch.setattr(accounts, '_issue', original)
    rotated = accounts.rotate_key(user, first.info.key_id, 'rotated')
    with pytest.raises(AuthenticationError):
        spaces.authenticate(first.value)
    assert spaces.authenticate(rotated.value).key_id == rotated.info.key_id
    accounts.revoke_key(user, second.info.key_id)
    accounts.revoke_key(user, second.info.key_id)
    with pytest.raises(AuthenticationError):
        spaces.authenticate(second.value)
    with pytest.raises(KeyNotFound):
        accounts.rotate_key(user, second.info.key_id)
    with accounts.database.admin_transaction() as conn:
        rows = conn.execute('SELECT key_hash FROM mnemotrace.api_keys WHERE user_id=%s', (user,)).fetchall()
    assert (hashlib.sha256(rotated.value.encode()).hexdigest(),) in rows
    assert not any(rotated.value in str(row) for row in rows)
    assert not any(hasattr(info, 'key_hash') or hasattr(info, 'value') for info in accounts.list_keys(user))


@pytest.mark.parametrize('key', ['', ' ', 'fake-key', 'é', '\ninvalid', None, 'x' * 513])
def test_invalid_authentication(spaces, key):
    with pytest.raises(AuthenticationError):
        spaces.authenticate(key)


def test_empty_spaces_ownership_and_paging(accounts, user, spaces):
    assert spaces.list(user) == ()
    a = spaces.create(user, '相同名称')
    b = spaces.create(user, '相同名称')
    assert a.space_id != b.space_id
    assert a.core_revision == a.space_generation == 0
    page = spaces.list(user, limit=1)
    assert len(page) == 1
    assert len(spaces.list(user, after_id=page[0].space_id, limit=1)) == 1
    other = accounts.create_user()
    try:
        assert spaces.list(other) == ()
        with pytest.raises(SpaceNotFound):
            spaces.authorize(other, a.space_id)
        with pytest.raises(SpaceNotFound):
            spaces.authorize(user, 'unknown')
        assert spaces.authorize(user, a.space_id).user_id == user
    finally:
        accounts.delete_user(other)


@pytest.mark.parametrize('name', ['', '  ', 'x' * 129, None])
def test_invalid_space_names(spaces, user, name):
    with pytest.raises(ValueError):
        spaces.create(user, name)


def test_deleted_account_cannot_authenticate_or_access(accounts, user, spaces):
    key = accounts.issue_key(user)
    spaces.create(user, 'Synthetic space')
    assert accounts.delete_user(user)
    assert not accounts.delete_user(user)
    with pytest.raises(AuthenticationError):
        spaces.authenticate(key.value)
    with pytest.raises(AccountNotFound):
        spaces.list(user)


def test_corrupt_space_settings_are_not_reconstructed(spaces, user, runtime):
    space = spaces.create(user, 'Synthetic')
    with runtime.space_transaction(user, space.space_id) as (conn, _):
        conn.execute("UPDATE mnemotrace.memory_spaces SET settings='{}' WHERE space_id=%s", (space.space_id,))
    with pytest.raises(DataCorruption):
        spaces.list(user)


def test_wrong_credential_purpose_and_elevated_login(cluster, runtime):
    with pytest.raises(DatabaseConfigurationError):
        Accounts(runtime)
    with Database(cluster.admin, purpose='admin') as admin:
        with pytest.raises(DatabaseConfigurationError):
            Spaces(admin)
    with pytest.raises(DatabaseConfigurationError):
        Database(replace(cluster.bootstrap), purpose='runtime').open()


def test_startup_role_cannot_disguise_privileged_login(cluster):
    from psycopg.conninfo import make_conninfo
    from mnemotrace.store.postgres.migrate import _roles
    disguised = replace(cluster.runtime, dsn=make_conninfo(
        cluster.bootstrap.dsn, options='-c role=' + _roles(cluster.runtime.role_prefix)['runtime']))
    with pytest.raises(DatabaseConfigurationError):
        Database(disguised).open()


def test_authentication_is_not_shadowed_by_temporary_objects(runtime, spaces, accounts, user):
    issued = accounts.issue_key(user)
    with runtime._transaction() as conn:
        conn.execute('CREATE TEMP TABLE api_keys(key_hash text, user_id text)')
        conn.execute("INSERT INTO pg_temp.api_keys VALUES (%s, 'forged')",
                     (hashlib.sha256(issued.value.encode()).hexdigest(),))
    assert spaces.authenticate(issued.value).user_id == user


def test_schema_verification_is_independent_of_startup_search_path(cluster, user):
    from psycopg.conninfo import make_conninfo
    config = replace(cluster.runtime, dsn=make_conninfo(
        cluster.runtime.dsn, options='-c search_path=mnemotrace,public'))
    with Database(config) as database:
        assert Spaces(database).list(user) == ()
