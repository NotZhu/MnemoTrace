"""验证迁移账本、并发安装和故障回滚。"""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from mnemotrace_next.adapters.postgres import PostgresStorage, migrate
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import FactId, MessageId, SessionId
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.ports.queries import FactFilter
from mnemotrace_next.ports.storage import CutoffTrigger

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

        fact_key = (scope.id.value, item.id.value)
        set_access = """
            UPDATE mnemotrace_next.fact_salience SET last_access = %s
            WHERE space_id = %s AND fact_id = %s
            """
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO mnemotrace_next.spaces (space_id, owner_id, name, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, scope.owner_id, scope.name, scope.created_at),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.entities (space_id, entity_id, name_key, name) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, subject.id.value, subject.name_key, subject.name),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.facts
                    (space_id, fact_id, subject_id, predicate, object_value, text,
                     recorded_at, labels, sensitive, source, lexemes)
                VALUES (%s, %s, %s, '喜欢', '茶', '喜欢茶', 10, '{}', false, 'manual', ''::tsvector)
                """,
                (*fact_key, subject.id.value),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.fact_salience (space_id, fact_id, salience, last_access) "
                "VALUES (%s, %s, %s, %s)",
                (*fact_key, state.salience, state.last_access),
            )
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


def test_session_evidence_and_trigger_upgrades_from_002(
    database_factory: DatabaseFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依次升级会话、证据和触发列表，保留旧来源和变更游标。"""
    with database_factory() as dsn:
        initial, salience, evidence, triggers, entity_kind = migrate._migrations()
        with monkeypatch.context() as legacy:
            legacy.setattr(migrate, "_migrations", lambda: (initial, salience))
            assert migrate.apply_migrations(dsn) == (initial.version, salience.version)
        scope = space()
        subject = Entity(scope.id, "用户")
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "INSERT INTO mnemotrace_next.spaces (space_id, owner_id, name, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, scope.owner_id, scope.name, scope.created_at),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.entities (space_id, entity_id, name_key, name) "
                "VALUES (%s, %s, %s, %s)",
                (scope.id.value, subject.id.value, subject.name_key, subject.name),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.messages
                    (space_id, message_id, session_id, role, content, recorded_at)
                VALUES (%s, 'first', 'first-session', 'user', 'legacy', 10),
                       (%s, 'second', 'second-session', 'user', 'legacy', 11)
                """,
                (scope.id.value, scope.id.value),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.facts
                    (space_id, fact_id, subject_id, predicate, object_value, text,
                     valid_at, recorded_at, labels, sensitive, source, lexemes)
                SELECT %s, id, %s, '偏好', 'tea', 'legacy tea', starts, 10, '{}', false, source,
                       to_tsvector('pg_catalog.simple', 'legacy tea')
                FROM (VALUES ('old', 'manual', 10), ('current', 'extracted', 11))
                    AS inputs(id, source, starts)
                """,
                (scope.id.value, subject.id.value),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.fact_provenance (space_id, fact_id, position, message_id)
                VALUES (%s, 'current', 0, 'first'), (%s, 'current', 1, 'second')
                """,
                (scope.id.value, scope.id.value),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.fact_supersedes VALUES (%s, 'current', 0, 'old')",
                (scope.id.value,),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.embedding_spaces VALUES (%s, 'legacy', 1)",
                (scope.id.value,),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.fact_vectors (space_id, embedding_space, fact_id, dim, embedding)
                VALUES (%s, 'legacy', 'current', 1, '[1]')
                """,
                (scope.id.value,),
            )
            conn.execute(
                """
                INSERT INTO mnemotrace_next.fact_changes
                    (space_id, fact_id, changed_at, before, after, source_message_ids)
                SELECT space_id, fact_id, 20, snapshot, snapshot, ARRAY['first','second']
                FROM (
                    SELECT space_id, fact_id, to_jsonb(f) - 'lexemes' ||
                        '{"provenance":["first","second"],"supersedes":["old"]}'::jsonb AS snapshot
                    FROM mnemotrace_next.facts AS f WHERE space_id=%s AND fact_id='current'
                ) AS previous
                """,
                (scope.id.value,),
            )
            conn.execute(
                "INSERT INTO mnemotrace_next.digests VALUES (%s, 'orphan', 'digest-only', "
                "'旧摘要', ARRAY['first'], 1, 10, 10)",
                (scope.id.value,),
            )
        with monkeypatch.context() as single_trigger:
            single_trigger.setattr(
                migrate, "_migrations", lambda: (initial, salience, evidence)
            )
            assert migrate.apply_migrations(dsn) == (evidence.version,)
        with psycopg.connect(dsn) as conn:
            cutoff = conn.execute(
                """
                WITH previous AS MATERIALIZED (
                    SELECT space_id, fact_id, to_jsonb(f) - 'lexemes' ||
                        '{"provenance":[]}'::jsonb AS snapshot
                    FROM mnemotrace_next.facts AS f
                    WHERE space_id = %s AND fact_id = 'old'
                ), closed AS (
                    UPDATE mnemotrace_next.facts AS f SET invalid_at = 11, expired_at = 20
                    FROM previous AS p
                    WHERE (f.space_id, f.fact_id) = (p.space_id, p.fact_id)
                    RETURNING f.space_id, f.fact_id, p.snapshot AS before,
                        p.snapshot || '{"invalid_at":11,"expired_at":20}'::jsonb AS after
                )
                INSERT INTO mnemotrace_next.fact_changes
                    (space_id, fact_id, changed_at, before, after, source_message_ids,
                     trigger_fact_id)
                SELECT space_id, fact_id, 20, before, after, ARRAY['first','second'], 'current'
                FROM closed RETURNING cursor, source_message_ids
                """,
                (scope.id.value,),
            ).fetchone()
            assert cutoff is not None
        with monkeypatch.context() as before_entity_kind:
            before_entity_kind.setattr(
                migrate, "_migrations", lambda: (initial, salience, evidence, triggers)
            )
            assert migrate.apply_migrations(dsn) == (triggers.version,)
        assert migrate.apply_migrations(dsn) == (entity_kind.version,)
        assert migrate.apply_migrations(dsn) == ()

        async def verify() -> None:
            async with pool(dsn) as connections:
                storage = PostgresStorage(connections, owner_id=scope.owner_id)
                current = FactId("current")
                first, second = MessageId("first"), MessageId("second")
                async with storage.session(scope.id, write=True) as writer:
                    user = Entity.space_user(scope.id)
                    assert user.id != subject.id
                    assert await writer.upsert_entity(user) == user
                    assert await writer.upsert_entity(user) == user
                    assert await writer.get_entities((subject.id, user.id)) == {
                        subject.id: subject,
                        user.id: user,
                    }
                    stored = await writer.get_fact(current)
                    assert stored is not None and stored.subject.entity_id == subject.id
                    assert await writer.has_session(SessionId("first-session"))
                    assert await writer.has_session(SessionId("second-session"))
                    assert await writer.has_session(SessionId("digest-only"))
                    assert await writer.get_fact_support_groups((current,)) == {
                        current: ((first, second),),
                    }
                    (change,) = await writer.list_fact_changes(current)
                    assert (
                        change.before == change.after == await writer.get_fact(current)
                    )
                    assert change.cutoff_triggers == ()
                    (old_change,) = await writer.list_fact_changes(FactId("old"))
                    assert old_change.cursor == cutoff[0]
                    assert old_change.cutoff_triggers == (
                        CutoffTrigger(current, 11.0, None),
                    )
                    assert await writer.search_lexical(
                        "legacy", 3, filters=FactFilter(), reference_time=20.0
                    )
                    assert await writer.search_vectors(
                        (1.0,),
                        1,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space="legacy",
                    ) == ((current, 1.0),)
                    assert await writer.delete_session_data(
                        SessionId("first-session")
                    ) == {current}
                    assert set(await writer.get_messages((first, second))) == {second}
                    old_fact = await writer.get_fact(FactId("old"))
                    assert old_fact is not None and old_fact.invalid_at == 11.0
                    assert await writer.list_fact_changes(old_fact.id) == ()

        with psycopg.connect(dsn) as conn:
            assert conn.execute(
                "SELECT to_regclass('mnemotrace_next.fact_supersedes')"
            ).fetchone() == (None,)
            assert (
                conn.execute(
                    """
                SELECT cursor, source_message_ids FROM mnemotrace_next.fact_changes
                WHERE space_id = %s AND fact_id = 'old'
                """,
                    (scope.id.value,),
                ).fetchone()
                == cutoff
            )
        run(verify())
