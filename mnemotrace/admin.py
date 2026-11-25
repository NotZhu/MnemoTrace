"""在服务主机上初始化默认账号和管理账号级 API key。"""

import argparse
import json
import os
import sys

import psycopg

from .adapters.postgres.identity import (
    DEFAULT_OWNER_ID,
    ApiKeyInfo,
    DefaultOwnerAlreadyConfigured,
    bootstrap_default_owner,
    issue_key,
    list_keys,
    revoke_key,
)
from .adapters.runtime import SystemClock, UuidGenerator
from .domain.space import Space
from .runtime_settings import read_postgres_dsn


def _key_json(info: ApiKeyInfo) -> dict[str, str | None]:
    return {
        "key_id": info.key_id,
        "created_at": info.created_at.isoformat(),
        "revoked_at": info.revoked_at.isoformat() if info.revoked_at else None,
    }


def main(argv: list[str] | None = None) -> int:
    """读取管理连接配置，执行指定的凭据操作。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="为默认账号签发首把 key 并创建初始空间")
    for command in ("issue", "list", "revoke"):
        operation = commands.add_parser(command)
        operation.add_argument("--owner-id", default=DEFAULT_OWNER_ID)
        if command == "revoke":
            operation.add_argument("key_id")
    args = parser.parse_args(argv)
    owner_id: str = getattr(args, "owner_id", DEFAULT_OWNER_ID)
    if not owner_id.strip() or "\x00" in owner_id:
        parser.error("账号标识须非空且不能包含空字符")
    try:
        dsn = read_postgres_dsn(os.environ)
    except ValueError as exc:
        parser.error(str(exc))

    result: object
    try:
        with psycopg.connect(dsn) as conn:
            if args.command == "bootstrap":
                conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
                space = Space(
                    UuidGenerator().new_space_id(),
                    DEFAULT_OWNER_ID,
                    "default",
                    SystemClock().now(),
                )
                issued = bootstrap_default_owner(conn, space)
                result = {
                    **_key_json(issued.info),
                    "token": issued.token,
                    "space_id": space.id.value,
                }
            elif args.command == "issue":
                issued = issue_key(conn, owner_id)
                result = {**_key_json(issued.info), "token": issued.token}
            elif args.command == "list":
                result = [_key_json(info) for info in list_keys(conn, owner_id)]
            elif revoke_key(conn, owner_id, args.key_id):
                result = {"key_id": args.key_id, "revoked": True}
            else:
                print("未找到指定账号的 key", file=sys.stderr)
                return 1
    except DefaultOwnerAlreadyConfigured:
        print(
            "默认账号已配置，可用 admin list 查看密钥，或用 admin issue 签发新密钥",
            file=sys.stderr,
        )
        return 1
    except psycopg.Error as exc:
        print(f"凭据操作失败（SQLSTATE {exc.sqlstate}）", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
