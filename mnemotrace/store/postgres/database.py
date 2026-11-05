"""Connection ownership and authorized transactions for the Postgres DAO."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
import re
from threading import RLock

from ..contracts import SpaceContext
from .errors import (
    AccountNotFound, DatabaseConfigurationError, DatabaseConstraintError,
    DatabaseDependencyError, DatabaseError, DatabaseUnavailable, IdentityRequired,
    SpaceNotFound, DataCorruption,
)


@dataclass(frozen=True)
class DatabaseConfig:
    dsn: str = field(repr=False)
    role_prefix: str = 'mnemotrace'
    pool_min: int = 1
    pool_max: int = 10
    connect_timeout: float = 10
    pool_timeout: float = 10
    open_timeout: float = 30

    def __post_init__(self):
        if not isinstance(self.dsn, str) or not self.dsn.strip():
            raise DatabaseConfigurationError('A database connection is required')
        if not isinstance(self.role_prefix, str) or not re.fullmatch(
                r'[a-z][a-z0-9_]{0,31}', self.role_prefix):
            raise DatabaseConfigurationError('Invalid database role prefix')
        if (type(self.pool_min) is not int or type(self.pool_max) is not int
                or not 1 <= self.pool_min <= self.pool_max):
            raise DatabaseConfigurationError('Expected 1 <= pool_min <= pool_max')
        for value in (self.connect_timeout, self.pool_timeout, self.open_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise DatabaseConfigurationError('Database timeouts must be positive finite numbers')

    @classmethod
    def from_env(cls, purpose: str = 'runtime') -> DatabaseConfig:
        variables = {'runtime': 'MNEMOTRACE_DATABASE_URL',
                     'migration': 'MNEMOTRACE_MIGRATION_DATABASE_URL',
                     'bootstrap': 'MNEMOTRACE_BOOTSTRAP_DATABASE_URL',
                     'admin': 'MNEMOTRACE_ADMIN_DATABASE_URL'}
        if purpose not in variables:
            raise DatabaseConfigurationError('Unknown database credential purpose')
        try:
            kwargs = {name: convert(os.environ[env]) for name, env, convert in (
                ('pool_min', 'MNEMOTRACE_DB_POOL_MIN', int),
                ('pool_max', 'MNEMOTRACE_DB_POOL_MAX', int),
                ('connect_timeout', 'MNEMOTRACE_DB_CONNECT_TIMEOUT', float),
                ('pool_timeout', 'MNEMOTRACE_DB_POOL_TIMEOUT', float),
                ('open_timeout', 'MNEMOTRACE_DB_OPEN_TIMEOUT', float),
            ) if env in os.environ}
        except (ValueError, OverflowError) as exc:
            raise DatabaseConfigurationError('Invalid database numeric configuration') from exc
        return cls(os.environ.get(variables[purpose], ''),
                   role_prefix=os.environ.get('MNEMOTRACE_DB_ROLE_PREFIX', 'mnemotrace'), **kwargs)


def driver():
    try:
        import psycopg
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise DatabaseDependencyError(
            'Install mnemotrace-memory[postgres] to use PostgreSQL') from exc
    return psycopg, ConnectionPool


@contextmanager
def database_errors():
    psycopg, _ = driver()
    try:
        yield
    except DatabaseError:
        raise
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        raise DatabaseUnavailable('Database connection unavailable') from exc
    except psycopg.DataError as exc:
        raise DataCorruption('Database data could not be decoded or represented') from exc
    except psycopg.IntegrityError as exc:
        raise DatabaseConstraintError('Database constraint rejected the operation') from exc
    except psycopg.Error as exc:
        raise DatabaseError('Database operation failed') from exc


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or '\x00' in value:
        raise ValueError('Expected a non-empty identifier of at most 512 characters')
    value.encode('utf-8')


class Database:
    """One process owns one explicitly opened pool for a single credential purpose."""

    def __init__(self, config: DatabaseConfig, *, purpose: str = 'runtime'):
        if purpose not in {'runtime', 'admin'}:
            raise DatabaseConfigurationError('Pools are for runtime or account administration')
        self.config = config
        self.purpose = purpose
        self._pool = None
        self._lifecycle_lock = RLock()

    def open(self) -> Database:
        with self._lifecycle_lock:
            return self._open()

    def _open(self) -> Database:
        if self._pool is not None:
            return self
        psycopg, pool_type = driver()
        # Parse separately so malformed DSNs are configuration failures, never echoed.
        try:
            psycopg.conninfo.conninfo_to_dict(self.config.dsn)
        except psycopg.Error as exc:
            raise DatabaseConfigurationError('Invalid database connection configuration') from exc
        pool = pool_type(
            self.config.dsn, open=False, min_size=self.config.pool_min,
            max_size=self.config.pool_max, timeout=self.config.pool_timeout,
            kwargs={'autocommit': True, 'connect_timeout': math.ceil(self.config.connect_timeout)},
            check=pool_type.check_connection,
        )
        try:
            # Pool retries normally log the underlying error, which can include a DSN.
            # A direct probe produces our sanitized failure before any retry worker starts.
            with database_errors(), psycopg.connect(
                    self.config.dsn, connect_timeout=math.ceil(self.config.connect_timeout)) as conn:
                from .migrate import check_schema, check_role
                check_schema(conn, self.config.role_prefix)
                check_role(conn, self.config.role_prefix, self.purpose)
            pool.open(wait=True, timeout=self.config.open_timeout)
        except Exception as exc:
            pool.close()
            from psycopg_pool import PoolTimeout
            if isinstance(exc, PoolTimeout):
                raise DatabaseUnavailable('Database pool initialization timed out') from exc
            raise
        self._pool = pool
        return self

    def close(self) -> None:
        with self._lifecycle_lock:
            pool, self._pool = self._pool, None
            if pool is not None:
                pool.close()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self, *, read_only: bool = False):
        pool = self._pool
        if pool is None:
            raise DatabaseUnavailable('Database pool is not open')
        driver()
        from psycopg_pool import PoolClosed, PoolTimeout
        try:
            with database_errors(), pool.connection() as conn, conn.transaction():
                if read_only:
                    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                yield conn
        except (PoolTimeout, PoolClosed) as exc:
            raise DatabaseUnavailable('Database connection pool unavailable') from exc

    @contextmanager
    def user_transaction(self, user_id: str, *, read_only: bool = False):
        if self.purpose != 'runtime':
            raise DatabaseConfigurationError('User transactions require runtime credentials')
        if not user_id:
            raise IdentityRequired('A trusted user identity is required')
        identifier(user_id)
        with self._transaction(read_only=read_only) as conn:
            conn.execute("SELECT set_config('mnemotrace.user_id', %s, true)", (user_id,))
            suffix = '' if read_only else ' FOR SHARE'
            row = conn.execute(
                "SELECT status FROM mnemotrace.users WHERE user_id = %s" + suffix,
                (user_id,),
            ).fetchone()
            if row is None or row[0] != 'active':
                raise AccountNotFound('Account unavailable')
            yield conn

    @contextmanager
    def space_transaction(self, user_id: str, space_id: str, *, read_only: bool = False):
        identifier(space_id)
        with self.user_transaction(user_id, read_only=read_only) as conn:
            # User first, space second: account deletion cannot race a new write.
            suffix = '' if read_only else ' FOR UPDATE'
            row = conn.execute(
                'SELECT status FROM mnemotrace.memory_spaces '
                'WHERE user_id = %s AND space_id = %s' + suffix, (user_id, space_id),
            ).fetchone()
            if row is None or row[0] != 'active':
                raise SpaceNotFound('Space unavailable')
            yield conn, SpaceContext(user_id, space_id)

    @contextmanager
    def admin_transaction(self):
        if self.purpose != 'admin':
            raise DatabaseConfigurationError('Account administration requires separate credentials')
        with self._transaction() as conn:
            yield conn
