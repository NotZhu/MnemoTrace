from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import pytest

from mnemotrace.store.contracts import StoredRecord
from mnemotrace.store.postgres import Accounts, Database, Spaces
from mnemotrace.store.postgres.dao import PostgresDAO
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.errors import SchemaMismatch
from mnemotrace.store.postgres import migrate as migrations
from mnemotrace.store.postgres.writes import allocate_sequence
from mnemotrace.types import Episode


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


def test_pre_removal_database_is_rejected_without_modifying_it(database_factory, monkeypatch):
    psycopg, _ = driver()
    current_scripts = migrations._scripts

    def old_scripts(prefix):
        scripts = current_scripts(prefix)
        initial, _ = scripts[0]
        initial = initial.replace('    source_roles text[]', (
            "    assertion_kind text NOT NULL CHECK (assertion_kind IN "
            "('unspecified','user_statement','assistant_suggestion','user_confirmation','assistant_report')),\n"
            '    source_roles text[]'
        )).replace('CHECK (format_version = 2)', 'CHECK (format_version = 1)')
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


@pytest.mark.parametrize('previous_count', [1, 2, 3, 4, 5, 6])
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
                with PostgresDAO(runtime, context)._transaction() as sql:
                    source = Episode('Synthetic queued source', user, 'chat', scope='raw')
                    sql.ensure_session('chat')
                    sql.write_record(StoredRecord.from_record(context, source), insert=True)
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


def test_upgrade_regenerates_saved_pre_review_extraction(database_factory, monkeypatch):
    from mnemotrace.memory import Memory
    from mnemotrace.llm import FakeLLM
    from tests.test_fact_adjudication import model_response

    with database_factory(initialize=False) as bundle:
        with monkeypatch.context() as phase6:
            phase6.setattr(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:6])
            migrations.migrate(bundle.migration)
            with Database(bundle.admin, purpose='admin') as admin, Database(bundle.runtime) as runtime:
                user = Accounts(admin).create_user()
                spaces = Spaces(runtime)
                space = spaces.create(user, 'Review migration test')
                context = spaces.authorize(user, space.space_id)
                mem = Memory(runtime, context, llm=FakeLLM(handler=model_response))
                accepted = mem.remember('Oslo', 'review', event_time=20, processing='buffered')
                operation, = accepted['operation_ids']
                assert mem._processing.run_one(operation, force=True)
                assert mem._processing.tasks.operation(operation).extraction_saved
                with mem.dao._transaction() as sql:
                    sql.run("UPDATE mnemotrace.operation_products SET result = result #- %s::text[] "
                            "WHERE user_id = %s AND space_id = %s AND operation_id = %s",
                            (['facts', '0', 'sensitive'], *sql.scope, operation))
        migrations.migrate(bundle.migration)
        with Database(bundle.runtime) as runtime:
            llm = FakeLLM(handler=model_response)
            mem = Memory(runtime, context, llm=llm)
            assert not mem._processing.tasks.operation(operation).extraction_saved
            assert mem.flush_processing('review')['status'] == 'completed'
            fact, = mem.facts()
            assert fact.object == 'Oslo' and not fact.review_pending
            assert len(llm.calls) == 1
