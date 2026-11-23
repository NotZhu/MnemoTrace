"""验证迁移账本、并发安装和故障回滚。"""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from mnemotrace.adapters.postgres import migrate
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.salience import FactSalience

from .conftest import DatabaseFactory
from .sample import fact, space

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
                INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
                VALUES ('a', 'owner', '空间', 1)
                """)
        assert migrate.apply_migrations(dsn) == ()
        with psycopg.connect(dsn) as conn:
            assert conn.execute("SELECT name FROM mnemotrace.spaces").fetchone() == (
                "空间",
            )
            conn.execute("UPDATE mnemotrace.schema_migrations SET checksum = 'changed'")
        with pytest.raises(migrate.MigrationError, match="校验和不匹配"):
            migrate.apply_migrations(dsn)


def test_migration_batch_failure_is_atomic(
    database_factory: DatabaseFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后续迁移失败时回滚整个批次的 DDL 和版本账本。"""
    with database_factory() as dsn:
        migrate.apply_migrations(dsn)
        versions = migrate._migrations()
        addition = migrate._Migration(
            "test_add_column.sql",
            "ALTER TABLE mnemotrace.spaces ADD COLUMN note text",
        )
        failure = migrate._Migration(
            "test_fails.sql",
            "CREATE TABLE mnemotrace.incomplete (id int); SELECT 1 / 0",
        )
        with psycopg.connect(dsn) as conn:
            conn.execute("""
                INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at)
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
            assert (
                conn.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'mnemotrace' AND table_name = 'spaces'
                    AND column_name = 'note'
                """).fetchone()
                is None
            )
            assert conn.execute(
                "SELECT to_regclass('mnemotrace.incomplete')"
            ).fetchone() == (None,)
        monkeypatch.setattr(migrate, "_migrations", lambda: (*versions, addition))
        assert migrate.apply_migrations(dsn) == (addition.version,)
        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT name, note FROM mnemotrace.spaces"
            ).fetchone() == ("空间", None)


def test_salience_above_baseline_requires_last_access(
    database_factory: DatabaseFactory,
) -> None:
    """新库禁止为强化后的显著度清除最后访问时间。"""
    with database_factory() as dsn:
        migrate.apply_migrations(dsn)
        scope = space()
        subject = Entity(scope.id, "甲")
        item = fact(subject, "保留")
        state = FactSalience().reinforce(20.0)

        fact_key = (scope.id.value, item.id.value)
        set_access = """
            UPDATE mnemotrace.fact_salience SET last_access = %s
            WHERE space_id = %s AND fact_id = %s
            """
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO mnemotrace.spaces (space_id, owner_id, name, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, scope.owner_id, scope.name, scope.created_at),
            )
            conn.execute(
                "INSERT INTO mnemotrace.entities (space_id, entity_id, name_key, name) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, subject.id.value, subject.name_key, subject.name),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace.facts
                    (space_id, fact_id, subject_id, predicate, object_value, text,
                     recorded_at, labels, sensitive, source, lexemes)
                VALUES (%s, %s, %s, '喜欢', '茶', '喜欢茶', 10, '{}', false, 'manual', ''::tsvector)
                """,
                (*fact_key, subject.id.value),
            )
            conn.execute(
                "INSERT INTO mnemotrace.fact_salience (space_id, fact_id, salience, last_access) "
                "VALUES (%s, %s, %s, %s)",
                (*fact_key, state.salience, state.last_access),
            )
            with (
                pytest.raises(psycopg.errors.CheckViolation) as rejected,
                conn.transaction(),
            ):
                conn.execute(set_access, (None, *fact_key))
            assert (
                rejected.value.diag.constraint_name
                == "fact_salience_last_access_required"
            )
