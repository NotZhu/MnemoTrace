from concurrent.futures import ThreadPoolExecutor

import pytest

from mnemotrace.postgres_memory import PostgresMemory
from mnemotrace.store.postgres import Accounts, Database, Spaces
from mnemotrace.store.postgres.dao import PostgresDAO
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.errors import SchemaMismatch
from mnemotrace.store.postgres import migrate as migrations
from mnemotrace.store.postgres.writes import allocate_sequence


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
        return [(text + (';SELECT 1/0' if i == 1 else ''), digest)
                for i, (text, digest) in enumerate(scripts)]
    with database_factory(initialize=False) as bundle:
        monkeypatch.setattr(migrations, '_scripts', broken)
        with pytest.raises(Exception):
            migrations.migrate(bundle.migration)
        with psycopg.connect(bundle.bootstrap.dsn) as conn:
            assert conn.execute("SELECT to_regnamespace('mnemotrace')").fetchone() == (None,)
        monkeypatch.setattr(migrations, '_scripts', original)
        migrations.migrate(bundle.migration)


@pytest.mark.parametrize('damage', [
    'DROP TABLE mnemotrace.source_summaries',
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


@pytest.mark.parametrize('previous_count', [1, 2, 3])
def test_upgrade_applies_only_pending_migrations(database_factory, monkeypatch, previous_count):
    all_migrations = migrations.MIGRATIONS
    with database_factory(initialize=False) as bundle:
        monkeypatch.setattr(migrations, 'MIGRATIONS', all_migrations[:previous_count])
        migrations.migrate(bundle.migration)
        monkeypatch.setattr(migrations, 'MIGRATIONS', all_migrations)
        with pytest.raises(SchemaMismatch):
            Database(bundle.runtime).open()
        migrations.migrate(bundle.migration)
        with Database(bundle.runtime):
            pass


def test_upgrade_makes_existing_queued_extract_discoverable(database_factory, monkeypatch):
    with database_factory(initialize=False) as bundle:
        with monkeypatch.context() as phase3:
            phase3.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:3])
            migrations.migrate(bundle.migration)
            with Database(bundle.admin, purpose='admin') as admin, Database(bundle.runtime) as runtime:
                user = Accounts(admin).create_user()
                spaces = Spaces(runtime)
                space = spaces.create(user, 'Upgrade test')
                context = spaces.authorize(user, space.space_id)
                source = PostgresMemory(runtime, context).remember_raw('Synthetic queued source', 'chat')
                with PostgresDAO(runtime, context)._transaction() as sql:
                    sequence = allocate_sequence(sql, 'chat')
                    stats = dict.fromkeys(('facts_added', 'duplicates', 'invalidated', 'working_added',
                                           'model_calls', 'windows_completed'), 0)
                    sql.insert('operations', dict(operation_id='phase3-queued', kind='extract', session_id='chat',
                               sequence=sequence, state='pending', phase='queued', attempts=0, embed_attempts=0,
                               space_generation=0, session_generation=0, stats=stats, stored_raw=True,
                               extraction_saved=False, core_committed=False))
                    sql.insert('operation_sources', dict(operation_id='phase3-queued', source_id=source.id,
                               content_version=1, input_start=0, input_end=len(source.content), input_order=0))
        migrations.migrate(bundle.migration)
        with Database(bundle.discovery, purpose='discovery') as discovery:
            assert discovery.discover_processing() == ((context, 'phase3-queued'),)
