"""Restricted database administration: python -m mnemotrace.admin --help."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from .store.postgres import Accounts, Database, DatabaseConfig
from .store.postgres.errors import DatabaseError
from .store.postgres.migrate import bootstrap_roles, migrate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-db')
    init.add_argument('--bootstrap', action='store_true',
                      help='Provision roles/extension with MNEMOTRACE_BOOTSTRAP_DATABASE_URL first')
    sub.add_parser('create-user')
    for command in ('delete-user', 'issue-key', 'list-keys', 'revoke-key', 'rotate-key'):
        command_parser = sub.add_parser(command)
        command_parser.add_argument('--user-id', required=True)
        if command in {'revoke-key', 'rotate-key'}:
            command_parser.add_argument('--key-id', required=True)
        if command in {'issue-key', 'rotate-key'}:
            command_parser.add_argument('--name', default='')
        if command == 'list-keys':
            command_parser.add_argument('--after-id')
            command_parser.add_argument('--limit', type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init-db':
            if args.bootstrap:
                bootstrap_roles(DatabaseConfig.from_env('bootstrap'))
            migrate(DatabaseConfig.from_env('migration'))
            result = {'initialized': True}
        else:
            with Database(DatabaseConfig.from_env('admin'), purpose='admin') as database:
                accounts = Accounts(database)
                if args.command == 'create-user':
                    result = {'user_id': accounts.create_user()}
                elif args.command == 'delete-user':
                    result = {'deleted': accounts.delete_user(args.user_id)}
                elif args.command == 'list-keys':
                    result = {'keys': [asdict(key) for key in accounts.list_keys(
                        args.user_id, after_id=args.after_id, limit=args.limit)]}
                elif args.command == 'revoke-key':
                    accounts.revoke_key(args.user_id, args.key_id)
                    result = {'revoked': True}
                else:
                    issued = (accounts.issue_key(args.user_id, args.name) if args.command == 'issue-key'
                              else accounts.rotate_key(args.user_id, args.key_id, args.name))
                    result = {**asdict(issued.info), 'key': issued.value}
        print(json.dumps(result, ensure_ascii=False, default=lambda v: v.isoformat()))
        return 0
    except (DatabaseError, ValueError) as exc:
        # Driver exceptions may contain credentials. Only our sanitized boundary
        # exception is emitted; never print its cause or a traceback here.
        print(json.dumps({'error': getattr(exc, 'code', 'invalid_argument'),
                          'detail': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
