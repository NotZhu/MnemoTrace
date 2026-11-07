"""Real, disposable databases. Only objects carrying our ownership marker are removed."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import secrets
from uuid import uuid4

import pytest

from mnemotrace.store.postgres import Accounts, Database, DatabaseConfig, Spaces
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.migrate import bootstrap_roles, migrate, _roles


def pytest_sessionfinish(session):
    if os.environ.get('MNEMOTRACE_REQUIRE_POSTGRES') == '1':
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if reporter is not None and any(report.nodeid.startswith('tests/postgres/')
                                        for report in reporter.stats.get('skipped', ())):
            reporter.write_line('Required Postgres verification contains skipped tests')
            session.exitstatus = 1


@dataclass
class TestDatabase:
    bootstrap: DatabaseConfig
    migration: DatabaseConfig
    runtime: DatabaseConfig
    admin: DatabaseConfig
    discovery: DatabaseConfig


def disposable_database_factory(dsn, record_testsuite_property=lambda *_: None):
    psycopg, _ = driver()
    from psycopg import sql

    @contextmanager
    def create(*, initialize=True):
        token = uuid4().hex[:16]
        dbname, prefix = 'mt_phase2_fixture_' + token, 'mt_' + token
        marker = 'mnemotrace:storage-phase2:' + token
        roles = _roles(prefix)
        logins = {kind: prefix + '_' + kind + '_login'
                  for kind in ('runtime', 'admin', 'migration', 'discovery')}
        owned_roles = []
        with psycopg.connect(dsn, autocommit=True) as root:
            record_testsuite_property('postgres_version', root.execute('SHOW server_version').fetchone()[0])
            root.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
            root.execute(sql.SQL('COMMENT ON DATABASE {} IS {}').format(
                sql.Identifier(dbname), sql.Literal(marker)))
            db_dsn = psycopg.conninfo.make_conninfo(dsn, dbname=dbname)
            config = DatabaseConfig(db_dsn, role_prefix=prefix, pool_max=1)
            try:
                bootstrap_roles(config)
                with psycopg.connect(db_dsn) as check:
                    record_testsuite_property('pgvector_version', check.execute(
                        "SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0])
                for role in roles.values():
                    root.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(
                        sql.Identifier(role), sql.Literal(marker)))
                    owned_roles.append(role)
                configs = {}
                for purpose, login in logins.items():
                    password = secrets.token_urlsafe(24)
                    root.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(
                        sql.Identifier(login), sql.Literal(password)))
                    root.execute(sql.SQL('COMMENT ON ROLE {} IS {}').format(
                        sql.Identifier(login), sql.Literal(marker)))
                    owned_roles.append(login)
                    root.execute(sql.SQL('GRANT {} TO {}').format(
                        sql.Identifier(roles[purpose]), sql.Identifier(login)))
                    configs[purpose] = DatabaseConfig(
                        psycopg.conninfo.make_conninfo(db_dsn, user=login, password=password),
                        role_prefix=prefix, pool_max=1)
                bundle = TestDatabase(config, **configs)
                if initialize:
                    migrate(bundle.migration)
                yield bundle
            finally:
                comment = root.execute(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
                    (dbname,)).fetchone()
                if comment != (marker,):
                    raise RuntimeError('Refusing to clean an unowned test database')
                root.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(dbname)))
                # Memberships disappear on DROP ROLE; no DROP OWNED in unrelated databases.
                for role in reversed(owned_roles):
                    comment = root.execute(
                        "SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",
                        (role,)).fetchone()
                    if comment != (marker,):
                        raise RuntimeError('Refusing to clean an unowned test role')
                    root.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    return create


@pytest.fixture(scope='session')
def database_factory(record_testsuite_property):
    dsn = os.environ.get('MNEMOTRACE_TEST_POSTGRES_DSN')
    if not dsn:
        if os.environ.get('MNEMOTRACE_REQUIRE_POSTGRES') == '1':
            pytest.fail('Real Postgres is required but MNEMOTRACE_TEST_POSTGRES_DSN is missing')
        pytest.skip('Set MNEMOTRACE_TEST_POSTGRES_DSN to a disposable-database DBA connection')
    return disposable_database_factory(dsn, record_testsuite_property)


@pytest.fixture(scope='session')
def cluster(database_factory):
    with database_factory() as bundle:
        yield bundle


@pytest.fixture
def runtime(cluster):
    with Database(cluster.runtime) as database:
        yield database


@pytest.fixture
def accounts(cluster):
    with Database(cluster.admin, purpose='admin') as database:
        yield Accounts(database)


@pytest.fixture
def user(accounts):
    user_id = accounts.create_user()
    yield user_id
    accounts.delete_user(user_id)


@pytest.fixture
def spaces(runtime):
    return Spaces(runtime)


@pytest.fixture
def product_service(runtime, monkeypatch):
    pytest.importorskip('fastapi')
    from mnemotrace.postgres_service import MemoryService
    from mnemotrace.server import app
    from tests.model_fixtures import RecordedLLM

    service = MemoryService(runtime, llm=RecordedLLM())
    monkeypatch.setattr(app, '_svc', service)
    return service


@pytest.fixture
def product_scope(accounts, spaces):
    users = {}

    def create(label='synthetic'):
        if label not in users:
            user = accounts.create_user()
            space = spaces.create(user, label)
            users[label] = spaces.authorize(user, space.space_id), accounts.issue_key(user).value
        return users[label]

    yield create
    for context, _ in users.values():
        accounts.delete_user(context.user_id)


@pytest.fixture()
def product_client(product_service, product_scope):
    from fastapi.testclient import TestClient
    from urllib.parse import parse_qsl, urlsplit
    from mnemotrace.server import app

    class ScopedClient(TestClient):
        def request(self, method, url, *, headers=None, **kwargs):
            headers = dict(headers or {})
            label = headers.pop('X-Synthetic-Scope', None)
            if label is not None:
                context, key = product_scope(label)
                headers['Authorization'] = f'Bearer {key}'
                if method in {'GET', 'HEAD', 'DELETE'}:
                    query = dict(parse_qsl(urlsplit(url).query))
                    kwargs['params'] = {**query, **(kwargs.get('params') or {}), 'space_id': context.space_id}
                elif urlsplit(url).path == '/v1/chat/completions':
                    body = kwargs['json']
                    kwargs['json'] = {**body, 'memory': {'space_id': context.space_id, **body.get('memory', {})}}
                elif kwargs.get('content') is None and kwargs.get('data') is None:
                    kwargs['json'] = {'space_id': context.space_id, **(kwargs.get('json') or {})}
            return super().request(method, url, headers=headers, **kwargs)

    with ScopedClient(app.app) as http:
        yield http
