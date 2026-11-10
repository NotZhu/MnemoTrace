"""Account/key administration and user-scoped spaces, separate from HTTP routing."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import secrets
from uuid import uuid4

from ..contracts import SpaceContext, SpaceSettings
from .database import Database, identifier
from .errors import (
    AccountNotFound, AuthenticationError, DataCorruption, DatabaseConfigurationError, KeyNotFound,
)


def _id(prefix):
    return prefix + '_' + uuid4().hex


@dataclass(frozen=True)
class Identity:
    user_id: str
    key_id: str


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    user_id: str
    name: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class IssuedKey:
    info: KeyInfo
    value: str = field(repr=False)


@dataclass(frozen=True)
class Space:
    space_id: str
    user_id: str
    name: str
    status: str
    core_revision: int
    space_generation: int
    record_version: int
    settings: SpaceSettings


def _space(row):
    try:
        values = dict(row[-1])
        if set(values) != set(asdict(SpaceSettings())):
            raise ValueError('Missing or unknown space settings')
        for key in ('track', 'mute'):
            if not isinstance(values[key], list):
                raise ValueError('Expected JSON array')
            values[key] = tuple(values[key])
        settings = SpaceSettings(**values)
        if any(type(value) is not int or value < 0 for value in row[4:7]) or row[6] < 1:
            raise ValueError('Invalid space version')
        return Space(*row[:-1], settings)
    except (ValueError, TypeError, KeyError) as exc:
        raise DataCorruption('Stored space settings are invalid') from exc


class Accounts:
    def __init__(self, database: Database):
        if database.purpose != 'admin':
            raise DatabaseConfigurationError('Accounts require administration credentials')
        self.database = database

    @staticmethod
    def _lock_user(conn, user_id):
        identifier(user_id)
        row = conn.execute(
            'SELECT status FROM mnemotrace.users WHERE user_id=%s FOR UPDATE', (user_id,),
        ).fetchone()
        if row is None or row[0] != 'active':
            raise AccountNotFound('Account unavailable')

    def create_user(self) -> str:
        user_id = _id('user')
        with self.database.admin_transaction() as conn:
            conn.execute("INSERT INTO mnemotrace.users(user_id,status) VALUES (%s,'active')", (user_id,))
        return user_id

    def delete_user(self, user_id: str) -> bool:
        identifier(user_id)
        with self.database.admin_transaction() as conn:
            row = conn.execute(
                'SELECT user_id FROM mnemotrace.users WHERE user_id=%s FOR UPDATE', (user_id,),
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE mnemotrace.users SET status='deleting' WHERE user_id=%s", (user_id,))
            conn.execute('DELETE FROM mnemotrace.users WHERE user_id=%s', (user_id,))
        return True

    @staticmethod
    def _issue(conn, user_id, name):
        if not isinstance(name, str) or len(name) > 128 or '\x00' in name:
            raise ValueError('Key name must be at most 128 characters')
        value = 'mt_' + secrets.token_urlsafe(32)
        row = conn.execute(
            'INSERT INTO mnemotrace.api_keys(key_id,user_id,key_hash,name) VALUES (%s,%s,%s,%s) '
            'RETURNING key_id,user_id,name,created_at,revoked_at',
            (_id('key'), user_id, hashlib.sha256(value.encode()).hexdigest(), name),
        ).fetchone()
        return IssuedKey(KeyInfo(*row), value)

    def issue_key(self, user_id: str, name: str = '') -> IssuedKey:
        with self.database.admin_transaction() as conn:
            self._lock_user(conn, user_id)
            return self._issue(conn, user_id, name)

    def list_keys(self, user_id: str, *, after_id: str | None = None,
                  limit: int = 100) -> tuple[KeyInfo, ...]:
        _page(after_id, limit)
        with self.database.admin_transaction() as conn:
            self._lock_user(conn, user_id)
            return tuple(KeyInfo(*row) for row in conn.execute(
                'SELECT key_id,user_id,name,created_at,revoked_at FROM mnemotrace.api_keys '
                'WHERE user_id=%s AND (%s::text IS NULL OR key_id>%s) ORDER BY key_id LIMIT %s',
                (user_id, after_id, after_id, limit)).fetchall())

    @staticmethod
    def _revoke(conn, user_id, key_id):
        identifier(key_id)
        row = conn.execute(
            'UPDATE mnemotrace.api_keys SET revoked_at=coalesce(revoked_at,clock_timestamp()) '
            'WHERE user_id=%s AND key_id=%s RETURNING key_id', (user_id, key_id),
        ).fetchone()
        if row is None:
            raise KeyNotFound('Key unavailable')

    def revoke_key(self, user_id: str, key_id: str) -> None:
        with self.database.admin_transaction() as conn:
            self._lock_user(conn, user_id)
            self._revoke(conn, user_id, key_id)

    def rotate_key(self, user_id: str, key_id: str, name: str = '') -> IssuedKey:
        with self.database.admin_transaction() as conn:
            self._lock_user(conn, user_id)
            row = conn.execute(
                'SELECT revoked_at FROM mnemotrace.api_keys WHERE user_id=%s AND key_id=%s',
                (user_id, key_id),
            ).fetchone()
            if row is None or row[0] is not None:
                raise KeyNotFound('Active key unavailable')
            self._revoke(conn, user_id, key_id)
            return self._issue(conn, user_id, name)


def _page(after_id, limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('Page size must be between 1 and 1000')
    if after_id is not None:
        identifier(after_id)


def _validate_space_name(name):
    if (not isinstance(name, str) or not name.strip() or not 1 <= len(name) <= 128
            or '\x00' in name):
        raise ValueError('Space name must contain 1 to 128 characters and not be blank')


class Spaces:
    def __init__(self, database: Database):
        if database.purpose != 'runtime':
            raise DatabaseConfigurationError('Spaces require runtime credentials')
        self.database = database

    def authenticate(self, key: str) -> Identity:
        if not isinstance(key, str) or not 1 <= len(key) <= 512 or any(
                ord(c) < 33 or ord(c) > 126 for c in key):
            raise AuthenticationError('Invalid API key')
        with self.database._transaction() as conn:
            row = conn.execute('SELECT user_id,key_id FROM mnemotrace.authenticate(%s)',
                               (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise AuthenticationError('Invalid API key')
            return Identity(*row)

    def create(self, user_id: str, name: str) -> Space:
        _validate_space_name(name)
        from psycopg.types.json import Jsonb
        with self.database.user_transaction(user_id) as conn:
            row = conn.execute(
                "INSERT INTO mnemotrace.memory_spaces(space_id,user_id,name,status,settings) "
                "VALUES (%s,%s,%s,'active',%s) RETURNING "
                'space_id,user_id,name,status,core_revision,space_generation,record_version,settings',
                (_id('space'), user_id, name, Jsonb(asdict(SpaceSettings()))),
            ).fetchone()
            return _space(row)

    def list(self, user_id: str, *, after_id: str | None = None,
             limit: int = 100) -> tuple[Space, ...]:
        _page(after_id, limit)
        with self.database.user_transaction(user_id, read_only=True) as conn:
            return tuple(_space(row) for row in conn.execute(
                'SELECT space_id,user_id,name,status,core_revision,space_generation,record_version,settings '
                "FROM mnemotrace.memory_spaces WHERE user_id=%s AND status='active' "
                'AND (%s::text IS NULL OR space_id>%s) ORDER BY space_id LIMIT %s',
                (user_id, after_id, after_id, limit),
            ).fetchall())

    def authorize(self, user_id: str, space_id: str) -> SpaceContext:
        with self.database.space_transaction(user_id, space_id, read_only=True) as (_, context):
            return context

    def rename(self, user_id: str, space_id: str, name: str) -> Space:
        _validate_space_name(name)
        with self.database.space_transaction(user_id, space_id) as (conn, _):
            row = conn.execute(
                'UPDATE mnemotrace.memory_spaces SET name = %s, record_version = record_version + 1 '
                'WHERE user_id = %s AND space_id = %s RETURNING '
                'space_id,user_id,name,status,core_revision,space_generation,record_version,settings',
                (name, user_id, space_id),
            ).fetchone()
        return _space(row)
