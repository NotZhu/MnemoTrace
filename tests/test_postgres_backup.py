"""Opt-in PostgreSQL 18 logical backup drill with the real client utilities."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from mnemotrace import Config
from tests.embedder_fixtures import product_embedder
from mnemotrace.memory import Memory
from mnemotrace.store.postgres import Accounts, Database, Spaces
from mnemotrace.store.postgres.database import driver
from mnemotrace.store.postgres.errors import DatabaseConfigurationError, SchemaMismatch, SpaceNotFound
from mnemotrace.store.postgres import migrate as migrations
from mnemotrace.store.postgres.migrate import bootstrap_roles, check_restore_target
from mnemotrace.worker import run_worker
from tests.postgres.conftest import disposable_database_factory
from tests.postgres.test_process_recovery import _accept, _model


@contextmanager
def restore_target(source):
    psycopg, _ = driver()
    from psycopg import sql

    name = 'mt_restore_' + uuid4().hex
    with psycopg.connect(source.bootstrap.dsn, autocommit=True) as root:
        root.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
        ownership = root.execute('SELECT oid FROM pg_database WHERE datname = %s', (name,)).fetchone()
        try:
            yield replace(source, **{
                purpose: replace(config, dsn=psycopg.conninfo.make_conninfo(config.dsn, dbname=name))
                for purpose, config in vars(source).items()
            })
        finally:
            if root.execute('SELECT oid FROM pg_database WHERE datname = %s', (name,)).fetchone() != ownership:
                raise RuntimeError('Refusing to clean an unowned restore database')
            root.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def pg(bin_dir, executable, config, *args):
    psycopg, _ = driver()
    settings = psycopg.conninfo.conninfo_to_dict(config.dsn)
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    for key, suffix in (('host', 'HOST'), ('port', 'PORT'), ('dbname', 'DATABASE'),
                        ('user', 'USER'), ('password', 'PASSWORD')):
        if key in settings:
            env['PG' + suffix] = settings[key]
    if executable == 'pg_restore':
        args = ('--dbname', settings['dbname'], *args)
    subprocess.run([str(bin_dir / executable), *map(str, args)],
                   env=env, check=True, timeout=60)


@pytest.fixture
def backup_environment():
    dsn = os.environ.get('MNEMOTRACE_TEST_POSTGRES_DSN')
    bin_dir = os.environ.get('MNEMOTRACE_TEST_POSTGRES_BIN')
    if not dsn or not bin_dir:
        pytest.skip('Set MNEMOTRACE_TEST_POSTGRES_DSN and MNEMOTRACE_TEST_POSTGRES_BIN for the backup drill')
    return dsn, Path(bin_dir).resolve()


def test_full_logical_backup_and_worker_resume(tmp_path, backup_environment):
    dsn, bin_dir = backup_environment
    archive = tmp_path / 'deployment.dump'
    with disposable_database_factory(dsn)() as source:
        with Database(source.admin, purpose='admin') as admin, Database(source.runtime) as runtime:
            accounts, spaces = Accounts(admin), Spaces(runtime)
            user_id = accounts.create_user()
            key = accounts.issue_key(user_id).value
            other = accounts.create_user()
            other_space = spaces.create(other, 'Isolated')
            space = spaces.create(user_id, 'Restored')
            scope = spaces.authorize(user_id, space.space_id)
            calls = []
            memory = Memory(runtime, scope, llm=_model(calls), embedder=product_embedder(), embedding_model='test-vectors-v1')
            complete = _accept(memory, 'first', 'Ada works at Synthetic Lab')
            assert memory.flush_processing('chat')['ok']
            assert memory.operation(complete).core_committed
            pending = _accept(memory, 'next', 'Grace works at Synthetic Studio', session='next')
            assert memory._processing.run_one(pending, force=True)
            assert memory.operation(pending).phase == 'embed'
            assert len(calls) >= 2
            prior_fact, = memory.history('Ada works', 'works_at')
            prior_edge, = memory.graph_relations(prior_fact.id)
            from tests.test_profile_cache import model as profile_model
            memory._processing.llm = profile_model()
            memory.add_fact('user', 'likes', 'Synthetic restored tea', sensitive=False, text='user likes Synthetic restored tea')
            generated = memory.refresh_profile()
            assert memory._processing.flush(operation_ids=(generated.operation_id,), timeout=3)['ok']
            prior_profile = memory.profile()
            unfinished_profile = memory.refresh_profile()
            assert memory._processing.run_one(unfinished_profile.operation_id)
            with memory.dao.read_snapshot() as view:
                revision = view.snapshot.core_revision

        pg(bin_dir, 'pg_dump', source.bootstrap, '-Fc', '--file', archive)
        with pytest.raises(DatabaseConfigurationError, match='empty database'):
            check_restore_target(source.bootstrap)
        with restore_target(source) as target:
            check_restore_target(target.bootstrap)
            bootstrap_roles(target.bootstrap)
            pg(bin_dir, 'pg_restore', target.bootstrap, '--single-transaction',
               '--exit-on-error', archive)
            restored_calls = []
            llm = _model(restored_calls)
            with (Database(target.runtime) as runtime,
                  Database(target.discovery, purpose='discovery') as discovery):
                spaces = Spaces(runtime)
                assert spaces.authenticate(key).user_id == user_id
                assert spaces.authorize(user_id, space.space_id) == scope
                with pytest.raises(SpaceNotFound):
                    spaces.authorize(user_id, other_space.space_id)
                memory = Memory(runtime, scope, llm=llm, embedder=product_embedder(), embedding_model='test-vectors-v1')
                assert memory.get_episode(prior_fact.provenance[0]).content == 'Ada works at Synthetic Lab'
                assert memory.graph_relations(prior_fact.id) == (prior_edge,)
                assert memory.operation(complete).core_committed
                assert memory.operation(pending).phase == 'embed'
                assert memory.profile()['profile'] == prior_profile['profile']
                resumed_profile = Memory(runtime, scope, llm=profile_model())
                assert resumed_profile._processing.run_one(unfinished_profile.operation_id)
                assert not resumed_profile._processing.llm.calls
                assert memory.operation(unfinished_profile.operation_id).core_committed
                with memory.dao.read_snapshot() as view:
                    assert view.snapshot.core_revision == revision
                assert prior_fact.id in {fact.id for fact in memory.search('Ada works Synthetic Lab').facts}
                for _ in range(6):
                    run_worker(discovery, runtime, Config(), llm, product_embedder(), 'test-vectors-v1', once=True)
                assert memory.operation(pending).core_committed
                assert 'extract' not in restored_calls
                assert len(memory.history('Grace works', 'works_at')) == 1


def test_restored_prior_graph_schema_is_rejected(tmp_path, backup_environment, monkeypatch):
    dsn, bin_dir = backup_environment
    archive = tmp_path / 'previous-graph.dump'
    current_scripts = migrations._scripts

    def prior_scripts(prefix):
        scripts = current_scripts(prefix)
        initial = scripts[0][0].replace(
            '    normalized_name mnemotrace.identifier NOT NULL,',
            '    normalized_name mnemotrace.identifier NOT NULL,\n    aliases text[] NOT NULL,',
        ).replace('CHECK (format_version = 6)', 'CHECK (format_version = 4)')
        return [(initial, sha256(initial.encode()).hexdigest()), *scripts[1:]]

    with disposable_database_factory(dsn)(initialize=False) as source:
        with monkeypatch.context() as previous:
            previous.setattr(migrations, '_scripts', prior_scripts)
            migrations.migrate(source.migration)
        pg(bin_dir, 'pg_dump', source.bootstrap, '-Fc', '--file', archive)
        with restore_target(source) as target:
            check_restore_target(target.bootstrap)
            bootstrap_roles(target.bootstrap)
            pg(bin_dir, 'pg_restore', target.bootstrap, '--single-transaction', '--exit-on-error', archive)
            with pytest.raises(SchemaMismatch, match='migration history is incompatible'):
                Database(target.runtime).open()
