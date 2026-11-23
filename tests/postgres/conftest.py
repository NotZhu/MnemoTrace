"""只在专用测试实例上创建并清理当前测试库。"""

import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from mnemotrace.adapters.postgres.migrate import apply_migrations

type DatabaseFactory = Callable[[], AbstractContextManager[str]]
_GRANT_ROLES = Path(__file__).resolve().parents[2] / "deploy" / "grant-roles.sql"


@dataclass(frozen=True)
class PgDatabase:
    """记录一次真库验证的管理、运行和凭据管理连接串。"""

    admin: str = field(repr=False)
    runtime: str = field(repr=False)
    key_admin: str = field(repr=False)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    """为真库测试读取显式配置的专用实例。

    Returns:
        专用测试实例的管理连接串。
    """
    dsn = os.environ.get("MNEMOTRACE_TEST_POSTGRES_DSN")
    if dsn:
        return dsn
    message = "请设置 MNEMOTRACE_TEST_POSTGRES_DSN 指向可丢弃的测试实例"
    if os.environ.get("MNEMOTRACE_REQUIRE_POSTGRES") == "1":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def database_factory(admin_dsn: str) -> DatabaseFactory:
    """提供在专用实例上创建和清理已安装 pgvector 的临时空库的工厂。

    Args:
        admin_dsn: 具有建库权限的测试实例连接串。

    Returns:
        返回临时数据库上下文管理器的工厂。实例清理范围限定在本次创建的库。
    """

    @contextmanager
    def empty() -> Iterator[str]:
        database = f"mnemo_test_{uuid4().hex}"
        identifier = sql.Identifier(database)
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL(
                    "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' "
                    "LOCALE_PROVIDER libc LOCALE 'C'"
                ).format(identifier)
            )
            try:
                dsn = make_conninfo(admin_dsn, dbname=database)
                with psycopg.connect(dsn) as db:
                    db.execute("CREATE EXTENSION vector WITH SCHEMA public")
                yield dsn
            finally:
                conn.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier)
                )

    return empty


def _grant_roles(dsn: str, runtime: str, key_admin: str) -> None:
    options = conninfo_to_dict(dsn)
    env = os.environ.copy()
    if (password := options.pop("password", None)) is not None:
        env["PGPASSWORD"] = str(password)
    try:
        result = subprocess.run(
            [
                "psql",
                "-X",
                "-w",
                "-q",
                "-d",
                make_conninfo("", **options),
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"runtime_role={runtime}",
                "-v",
                f"admin_role={key_admin}",
                "-f",
                str(_GRANT_ROLES),
            ],
            env=env,
            check=False,
        )
    except FileNotFoundError:
        pytest.fail("真库测试需要在 PATH 中提供 psql 以执行部署授权脚本")
    if result.returncode:
        pytest.fail(f"部署授权脚本执行失败（psql 退出码 {result.returncode}）")


@pytest.fixture(scope="session")
def database(admin_dsn: str, database_factory: DatabaseFactory) -> Iterator[PgDatabase]:
    """迁移临时库并用部署脚本配置运行和凭据管理角色。

    Args:
        admin_dsn: 具有建库和创建角色权限的测试实例连接串。
        database_factory: 创建临时库的工厂。

    Yields:
        临时数据库的管理、运行和凭据管理账号连接信息。
    """
    suffix = uuid4().hex
    runtime, key_admin = f"mnemo_run_{suffix}", f"mnemo_keys_{suffix}"
    passwords = {role: token_urlsafe(24) for role in (runtime, key_admin)}
    with psycopg.connect(admin_dsn, autocommit=True) as cluster:
        with cluster.transaction():
            for role, password in passwords.items():
                cluster.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        try:
            with database_factory() as dsn:
                apply_migrations(dsn)
                _grant_roles(dsn, runtime, key_admin)
                yield PgDatabase(
                    admin=dsn,
                    runtime=make_conninfo(
                        dsn, user=runtime, password=passwords[runtime]
                    ),
                    key_admin=make_conninfo(
                        dsn, user=key_admin, password=passwords[key_admin]
                    ),
                )
        finally:
            cluster.execute(
                sql.SQL("DROP ROLE {}, {}").format(
                    sql.Identifier(runtime), sql.Identifier(key_admin)
                )
            )


def pytest_sessionfinish(session: pytest.Session) -> None:
    """在必跑真库作业中把跳过真库测试视为失败。

    Args:
        session: 提供测试结果及退出状态的 pytest 会话。
    """
    if os.environ.get("MNEMOTRACE_REQUIRE_POSTGRES") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and any(
        report.nodeid.startswith("tests/postgres/")
        for report in reporter.stats.get("skipped", ())
    ):
        reporter.write_line("必跑真库测试出现跳过")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
