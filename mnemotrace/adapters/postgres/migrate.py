"""显式应用 PostgreSQL schema 迁移并报告版本状态。"""

import argparse
import os
import sys
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

import psycopg
from psycopg.rows import TupleRow

from ...runtime_settings import read_postgres_dsn

_LOCK_KEY = 698771117284033230  # 此数据库内为 MnemoTrace 迁移保留的锁键。
_LEDGER = "mnemotrace.schema_migrations"


class MigrationError(RuntimeError):
    """表示迁移文件、数据库版本或执行状态异常。"""


@dataclass(frozen=True)
class _Migration:
    version: str
    statement: str

    @property
    def checksum(self) -> str:
        return sha256(self.statement.encode("utf-8")).hexdigest()


def _migrations() -> tuple[_Migration, ...]:
    return tuple(
        _Migration(entry.name, entry.read_text(encoding="utf-8"))
        for entry in sorted(
            files(__package__).joinpath("migrations").iterdir(), key=lambda x: x.name
        )
        if entry.name.endswith(".sql")
    )


def _applied(conn: psycopg.Connection[TupleRow]) -> dict[str, str]:
    exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (_LEDGER,)).fetchone()
    assert exists is not None
    if not exists[0]:
        return {}
    return dict(
        conn.execute("SELECT version, checksum FROM mnemotrace.schema_migrations")
    )


def migration_checksums() -> dict[str, str]:
    """返回当前包所需的迁移及校验和，供服务就绪检查使用。"""
    return {migration.version: migration.checksum for migration in _migrations()}


def _check_history(migrations: tuple[_Migration, ...], applied: dict[str, str]) -> None:
    known = {migration.version: migration.checksum for migration in migrations}
    for version, checksum in applied.items():
        if version not in known:
            raise MigrationError(f"数据库包含当前包未知的迁移 {version}")
        if checksum != known[version]:
            raise MigrationError(f"已应用的迁移 {version} 校验和不匹配")
    if set(applied) != {migration.version for migration in migrations[: len(applied)]}:
        raise MigrationError("数据库的迁移记录缺少前序版本")


def _check_prerequisites(conn: psycopg.Connection[TupleRow]) -> None:
    row = conn.execute("""
        SELECT to_regnamespace('mnemotrace') IS NOT NULL,
               EXISTS (
                   SELECT 1 FROM pg_extension AS extension
                   JOIN pg_namespace AS namespace ON namespace.oid = extension.extnamespace
                   WHERE extension.extname = 'vector' AND namespace.nspname = 'public'
               )
        """).fetchone()
    assert row is not None
    has_schema, has_vector = row
    if not has_schema:
        raise MigrationError("请先初始化 mnemotrace schema")
    if not has_vector:
        raise MigrationError("请先在 public schema 安装 vector 扩展")


def apply_migrations(dsn: str) -> tuple[str, ...]:
    """在迁移锁保护下应用待执行的迁移并更新账本。

    Args:
        dsn: 具有迁移所需权限的数据库连接串。

    Returns:
        本次提交的迁移文件名，按应用顺序排列。

    Raises:
        MigrationError: 缺少 schema 或扩展、迁移历史不符，或迁移执行失败。
        psycopg.Error: 无法连接数据库，或迁移事务初始化、提交或回滚失败。
    """
    migrations = _migrations()
    completed: list[str] = []
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED READ WRITE")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        _check_prerequisites(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mnemotrace.schema_migrations (
                version text PRIMARY KEY,
                checksum text NOT NULL
            )
            """)
        applied = _applied(conn)
        _check_history(migrations, applied)
        for migration in migrations[len(applied) :]:
            try:
                conn.execute(migration.statement, prepare=False)
                conn.execute(
                    """
                    INSERT INTO mnemotrace.schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (migration.version, migration.checksum),
                )
            except psycopg.Error as exc:
                raise MigrationError(
                    f"迁移 {migration.version} 失败（SQLSTATE {exc.sqlstate}）"
                ) from None
            completed.append(migration.version)
    return tuple(completed)


def migration_status(dsn: str) -> dict[str, bool]:
    """核对数据库迁移账本和当前包的迁移历史。

    Args:
        dsn: 可读取迁移账本的数据库连接串。

    Returns:
        以迁移文件名为键的已应用状态。

    Raises:
        MigrationError: 已应用版本或校验和与当前包不一致。
        psycopg.Error: 无法连接数据库或读取迁移账本。
    """
    migrations = _migrations()
    with psycopg.connect(dsn) as conn:
        conn.read_only = True
        applied = _applied(conn)
        _check_history(migrations, applied)
    return {migration.version: migration.version in applied for migration in migrations}


def main() -> int:
    """读取环境中的迁移连接配置并执行 up 或 status。

    Returns:
        成功时为 0，数据库操作或迁移历史校验失败时为 1。

    Raises:
        SystemExit: 参数无效、缺少数据库连接配置或请求帮助。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "status"))
    options = parser.parse_args()
    try:
        dsn = read_postgres_dsn(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        if options.command == "up":
            for version in apply_migrations(dsn):
                print(f"{version} 已应用")
        else:
            for version, applied in migration_status(dsn).items():
                print(f"{version} {'已应用' if applied else '待应用'}")
    except MigrationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"数据库连接或迁移事务失败（SQLSTATE {exc.sqlstate}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
