from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from mnemotrace.store.postgres import Accounts, Database
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
    original = migrations._scripts
    def broken(prefix):
        scripts = original(prefix)
        return [(text + (';SELECT 1/0' if i == 1 else ''), content_hash)
                for i, (text, content_hash) in enumerate(scripts)]
    with database_factory(initialize=False) as bundle:
        monkeypatch.setattr(migrations, '_scripts', broken)
        with pytest.raises(Exception):
            migrations.migrate(bundle.migration)
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            assert conn.execute("SELECT to_regnamespace('mnemotrace')").fetchone() == (None,)
        monkeypatch.setattr(migrations, '_scripts', original)
        migrations.migrate(bundle.migration)


@pytest.mark.parametrize('damage', [
    'DROP TABLE mnemotrace.source_digests',
    "UPDATE mnemotrace.schema_migrations SET checksum='wrong' WHERE version=1",
    'UPDATE mnemotrace.schema_migrations SET version=99 WHERE version=2',
    'DROP TABLE mnemotrace.schema_migrations',
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


def test_pre_removal_database_is_rejected_without_modifying_it(database_factory, monkeypatch):
    psycopg, _ = driver()
    current_scripts = migrations._scripts

    def old_scripts(prefix):
        scripts = current_scripts(prefix)
        initial, _ = scripts[0]
        initial = initial.replace('    valid_at timestamptz,', (
            "    assertion_kind text NOT NULL CHECK (assertion_kind IN "
            "('unspecified','user_statement','assistant_suggestion','user_confirmation','assistant_report')),\n"
            '    valid_at timestamptz,'
        )).replace('CHECK (format_version = 8)', 'CHECK (format_version = 4)')
        return [(initial, sha256(initial.encode()).hexdigest()), *scripts[1:]]

    with database_factory(initialize=False) as bundle:
        with monkeypatch.context() as old:
            old.setattr(migrations, '_scripts', old_scripts)
            migrations.migrate(bundle.migration)
            with Database(bundle.admin, purpose='admin') as admin:
                user = Accounts(admin).create_user()
        with pytest.raises(SchemaMismatch, match='migration history is incompatible'):
            Database(bundle.runtime).open()
        with pytest.raises(SchemaMismatch, match='migration history is incompatible'):
            migrations.migrate(bundle.migration)
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            assert conn.execute('SELECT user_id FROM mnemotrace.users').fetchall() == [(user,)]
            assert conn.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'mnemotrace' AND table_name = 'facts' AND column_name = 'assertion_kind'
                """).fetchone() == ('assertion_kind',)
