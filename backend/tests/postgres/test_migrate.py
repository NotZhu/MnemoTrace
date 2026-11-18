"""验证迁移账本、并发安装和故障回滚。"""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from mnemotrace_next.adapters.postgres import PostgresStorage, migrate
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.salience import FactSalience

from .conftest import DatabaseFactory
from .sample import fact, pool, run, space

pytestmark = pytest.mark.postgres


def test_migration_concurrency_repeat_and_checksum(
    database_factory: DatabaseFactory,
) -> None:
    """并发安装只有一个执行者，再次安装保留原有数据。"""
    versions = tuple(migration.version for migration in migrate._migrations())
    with database_factory() as dsn:
        assert migrate.migration_status(dsn) == dict.fromkeys(versions, False)
        with ThreadPoolExecutor(max_workers=2) as executor:
            applied = list(executor.map(migrate.apply_migrations, (dsn, dsn)))
        assert sorted(applied) == [(), versions]
        assert migrate.migration_status(dsn) == dict.fromkeys(versions, True)
        with psycopg.connect(dsn) as conn:
            conn.execute("""
                INSERT INTO mnemotrace_next.spaces (space_id, owner_id, name, created_at)
                VALUES ('a', 'owner', '空间', 1)
                """)
        assert migrate.apply_migrations(dsn) == ()
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT name FROM mnemotrace_next.spaces"
            ).fetchone() == ("空间",)
            conn.execute(
                "UPDATE mnemotrace_next.schema_migrations SET checksum = 'changed'"
            )
        with pytest.raises(migrate.MigrationError, match="校验和不匹配"):
            migrate.apply_migrations(dsn)


def test_migration_upgrade_and_failure_are_atomic(
    database_factory: DatabaseFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """新增列兼容旧数据，后续失败回滚 DDL 和版本账本。"""
    with database_factory() as dsn:
        migrate.apply_migrations(dsn)
        versions = migrate._migrations()
        addition = migrate._Migration(
            "test_add_column.sql",
            "ALTER TABLE mnemotrace_next.spaces ADD COLUMN note text",
        )
        failure = migrate._Migration(
            "test_fails.sql",
            "CREATE TABLE mnemotrace_next.incomplete (id int); SELECT 1 / 0",
        )
        with psycopg.connect(dsn) as conn:
            conn.execute("""
                INSERT INTO mnemotrace_next.spaces (space_id, owner_id, name, created_at)
                VALUES ('a', 'owner', '空间', 1)
                """)
        monkeypatch.setattr(
            migrate, "_migrations", lambda: (*versions, addition, failure)
        )
        with pytest.raises(migrate.MigrationError, match=failure.version):
            migrate.apply_migrations(dsn)
        assert migrate.migration_status(dsn) == {
            **dict.fromkeys((migration.version for migration in versions), True),
            addition.version: False,
            failure.version: False,
        }
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT to_regclass('mnemotrace_next.incomplete')"
            ).fetchone() == (None,)
        monkeypatch.setattr(migrate, "_migrations", lambda: (*versions, addition))
        assert migrate.apply_migrations(dsn) == (addition.version,)
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT name, note FROM mnemotrace_next.spaces"
            ).fetchone() == ("空间", None)


def test_salience_access_constraint_on_upgrade(
    database_factory: DatabaseFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """升级检查既有显著度，并约束升级后的直接 SQL 写入。"""
    with database_factory() as dsn:
        initial = migrate._migrations()[0]
        version = "002_fact_salience_last_access.sql"
        with monkeypatch.context() as only_initial:
            only_initial.setattr(migrate, "_migrations", lambda: (initial,))
            assert migrate.apply_migrations(dsn) == (initial.version,)

        scope = space()
        subject = Entity(scope.id, "甲")
        item = fact(subject, "保留")
        state = FactSalience().reinforce(20.0)

        async def seed() -> None:
            async with pool(dsn) as connections:
                storage = PostgresStorage(connections, owner_id=scope.owner_id)
                await storage.create_space(scope)
                async with storage.session(scope.id, write=True) as writer:
                    await writer.upsert_entity(subject)
                    await writer.save_facts((item,), changed_at=15.0)
                    await writer.save_salience({item.id: state})

        run(seed())
        fact_key = (scope.id.value, item.id.value)
        set_access = """
            UPDATE mnemotrace_next.fact_salience SET last_access = %s
            WHERE space_id = %s AND fact_id = %s
            """
        with psycopg.connect(dsn) as conn:
            conn.execute(set_access, (None, *fact_key))
        with pytest.raises(migrate.MigrationError, match="SQLSTATE 23514"):
            migrate.apply_migrations(dsn)
        assert migrate.migration_status(dsn)[version] is False

        with psycopg.connect(dsn) as conn:
            conn.execute(set_access, (state.last_access, *fact_key))
        assert version in migrate.apply_migrations(dsn)
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                """
                SELECT salience, last_access FROM mnemotrace_next.fact_salience
                WHERE space_id = %s AND fact_id = %s
                """,
                fact_key,
            ).fetchone() == (state.salience, state.last_access)
            with (
                pytest.raises(psycopg.errors.CheckViolation) as rejected,
                conn.transaction(),
            ):
                conn.execute(set_access, (None, *fact_key))
            assert (
                rejected.value.diag.constraint_name
                == "fact_salience_last_access_required"
            )
