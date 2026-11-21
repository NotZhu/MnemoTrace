"""只在专用测试实例上创建并清理当前测试库。"""

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from secrets import token_urlsafe
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from mnemotrace_next.adapters.postgres.migrate import apply_migrations

type DatabaseFactory = Callable[[], AbstractContextManager[str]]


@dataclass(frozen=True)
class PgDatabase:
    """记录一次真库验证的迁移和运行连接串。"""

    admin: str = field(repr=False)
    runtime: str = field(repr=False)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    """为真库测试读取显式配置的专用实例。

    Returns:
        专用测试实例的管理连接串。
    """
    dsn = os.environ.get("MNEMOTRACE_NEXT_TEST_POSTGRES_DSN")
    if dsn:
        return dsn
    message = "请设置 MNEMOTRACE_NEXT_TEST_POSTGRES_DSN 指向可丢弃的测试实例"
    if os.environ.get("MNEMOTRACE_NEXT_REQUIRE_POSTGRES") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def database_factory(admin_dsn: str) -> DatabaseFactory:
    """提供在专用实例上创建和清理临时库的工厂。

    Args:
        admin_dsn: 具有建库权限的测试实例连接串。

    Returns:
        返回临时数据库上下文管理器的工厂。实例清理范围限定在本次创建的库。
    """

    @contextmanager
    def empty() -> Iterator[str]:
        database = f"mnemo_next_test_{uuid4().hex}"
        identifier = sql.Identifier(database)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                    "LOCALE_PROVIDER libc LOCALE 'C'"
                ).format(identifier)
            )
            try:
                yield make_conninfo(admin_dsn, dbname=database)
            finally:
                conn.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier)
                )

    return empty


@pytest.fixture(scope="session")
def database(admin_dsn: str, database_factory: DatabaseFactory) -> Iterator[PgDatabase]:
    """迁移临时库并为测试运行角色配置最小权限。

    Args:
        admin_dsn: 具有建库和创建角色权限的测试实例连接串。
        database_factory: 创建临时库的工厂。

    Yields:
        临时数据库的迁移账号和运行账号连接信息。
    """
    role = f"mnemo_next_run_{uuid4().hex}"
    password = token_urlsafe(24)
    identifier = sql.Identifier(role)
    with psycopg.connect(admin_dsn, autocommit=True) as cluster:
        cluster.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                identifier, sql.Literal(password)
            )
        )
        try:
            with database_factory() as dsn:
                apply_migrations(dsn)
                with psycopg.connect(dsn) as conn:
                    for grant in (
                        "USAGE ON SCHEMA mnemotrace_next",
                        "SELECT ON mnemotrace_next.schema_migrations",
                        """
                        SELECT (key_id, owner_id, secret_hash, revoked_at)
                            ON mnemotrace_next.api_keys
                        """,
                        "SELECT, INSERT, UPDATE (purge_generation, name) ON mnemotrace_next.spaces",
                        """
                        SELECT, INSERT, DELETE ON
                            mnemotrace_next.messages, mnemotrace_next.fact_provenance,
                            mnemotrace_next.sessions, mnemotrace_next.fact_changes
                        """,
                        """
                        SELECT, INSERT ON
                            mnemotrace_next.embedding_spaces, mnemotrace_next.fact_change_triggers
                        """,
                        """
                        SELECT, INSERT, UPDATE, DELETE ON
                            mnemotrace_next.entities, mnemotrace_next.facts,
                            mnemotrace_next.digests, mnemotrace_next.profiles,
                            mnemotrace_next.fact_salience, mnemotrace_next.fact_vectors
                        """,
                        "USAGE ON ALL SEQUENCES IN SCHEMA mnemotrace_next",
                    ):
                        conn.execute(
                            sql.SQL("GRANT {} TO {}").format(sql.SQL(grant), identifier)
                        )
                yield PgDatabase(dsn, make_conninfo(dsn, user=role, password=password))
        finally:
            cluster.execute(sql.SQL("DROP ROLE {}").format(identifier))


def pytest_sessionfinish(session: pytest.Session) -> None:
    """在必跑真库作业中把跳过真库测试视为失败。

    Args:
        session: 提供测试结果及退出状态的 pytest 会话。
    """
    if os.environ.get("MNEMOTRACE_NEXT_REQUIRE_POSTGRES") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and any(
        report.nodeid.startswith("tests/postgres/")
        for report in reporter.stats.get("skipped", ())
    ):
        reporter.write_line("必跑真库测试出现跳过")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
