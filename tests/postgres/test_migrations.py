from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemotrace.store.postgres import Database
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.errors import SchemaMismatch
from mnemotrace.store.postgres import migrate as migrations


def test_concurrent_and_repeat_initialization(database_factory):
    with database_factory(initialize=False) as bundle:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(migrations.migrate, [bundle.migration] * 2))
        migrations.migrate(bundle.migration)
        with Database(bundle.runtime):
            pass


def test_failed_initialization_rolls_back_all_tables(database_factory, monkeypatch):
    psycopg, _ = driver()
    original = migrations._baseline
    def broken(prefix):
        text, checksum = original(prefix)
        return text + ";SELECT 1/0", checksum

    with database_factory(initialize=False) as bundle:
        monkeypatch.setattr(migrations, '_baseline', broken)
        with pytest.raises(Exception):
            migrations.migrate(bundle.migration)
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            assert conn.execute("SELECT to_regnamespace('mnemotrace')").fetchone() == (None,)
        monkeypatch.setattr(migrations, '_baseline', original)
        migrations.migrate(bundle.migration)


@pytest.mark.parametrize('damage', [
    'DROP TABLE mnemotrace.source_digests',
    "UPDATE mnemotrace.schema_baseline SET checksum='wrong'",
    'DELETE FROM mnemotrace.schema_baseline',
    'DROP TABLE mnemotrace.schema_baseline',
    'ALTER TABLE mnemotrace.sources DROP CONSTRAINT sources_pkey CASCADE',
    'ALTER TABLE mnemotrace.sources DISABLE ROW LEVEL SECURITY',
    'DROP INDEX mnemotrace.sources_session_time',
    'REVOKE UPDATE(status) ON mnemotrace.users FROM {runtime}',
])
def test_damaged_schema_is_rejected_not_repaired(database_factory, damage):
    psycopg, _ = driver()
    with database_factory() as bundle:
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            conn.execute(damage.replace('{runtime}', migrations._roles(bundle.runtime.role_prefix)['runtime']))
        with pytest.raises(SchemaMismatch):
            Database(bundle.runtime).open()
        with pytest.raises(SchemaMismatch):
            migrations.migrate(bundle.migration)


def test_unversioned_existing_schema_is_rejected(database_factory):
    psycopg, _ = driver()
    with database_factory(initialize=False) as bundle:
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            conn.execute('CREATE SCHEMA mnemotrace')
            conn.execute('CREATE TABLE mnemotrace.previous_data(id text)')
        with pytest.raises(SchemaMismatch):
            migrations.migrate(bundle.migration)


def test_old_baseline_is_rejected_without_modifying_it(database_factory):
    psycopg, _ = driver()
    with database_factory(initialize=False) as bundle:
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            conn.execute('CREATE SCHEMA mnemotrace AUTHORIZATION '
                         + migrations._roles(bundle.runtime.role_prefix)['migration'])
            conn.execute('CREATE TABLE mnemotrace.schema_migrations(version int)')
            conn.execute('INSERT INTO mnemotrace.schema_migrations VALUES (6)')
        with pytest.raises(SchemaMismatch):
            migrations.migrate(bundle.migration)
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            assert conn.execute('SELECT version FROM mnemotrace.schema_migrations').fetchall() == [(6,)]
