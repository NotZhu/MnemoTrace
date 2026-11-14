"""Manage memory in a trusted, explicitly selected PostgreSQL space."""
from argparse import ArgumentParser
from contextlib import contextmanager
import json
import os

from .extraction import ExtractionError
from .service import MemoryService, ProcessingFailure
from .store.postgres.errors import DatabaseError


@contextmanager
def local_space(args):
    service = MemoryService.from_env(embedder_name=getattr(args, 'embedder', None), llm_name=getattr(args, 'llm', None))
    try:
        yield service, service.spaces.authorize(args.user_id, args.space_id)
    finally:
        service.close()


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', default=os.environ.get('MNEMOTRACE_USER_ID'))
    parser.add_argument('--space-id', default=os.environ.get('MNEMOTRACE_SPACE_ID'))
    commands = parser.add_subparsers(dest='command', required=True)
    remember = commands.add_parser('remember')
    remember.add_argument('content')
    remember.add_argument('--session-id', required=True)
    remember.add_argument('--agent-id')
    remember.add_argument('--event-time', type=float, help='Source time as Unix seconds')
    remember.add_argument('--no-extract', dest='extract', action='store_false',
                          help='Save the original without extracting facts')
    remember.add_argument('--processing', choices=('buffered', 'immediate'), default='buffered')
    for command in ('operation', 'retry'):
        commands.add_parser(command).add_argument('operation_id')
    commands.add_parser('recall').add_argument('query')
    delete = commands.add_parser('delete-fact')
    delete.add_argument('fact_id')
    delete.add_argument('--confirm', action='store_true', required=True)
    args = parser.parse_args(argv)
    if not args.user_id or not args.space_id:
        parser.error('Provide --user-id and --space-id')
    try:
        with local_space(args) as (service, context):
            if args.command == 'remember':
                result = service.remember(context, args.content, args.session_id, args.extract,
                                          args.processing, agent_id=args.agent_id, event_time=args.event_time)
            elif args.command in ('operation', 'retry'):
                result = service.operation(context, args.operation_id, retry=args.command == 'retry')
                if result is None:
                    parser.exit(1, 'Operation unavailable in this space\n')
            elif args.command == 'recall':
                result = service.recall(context, args.query)
            else:
                result = service.delete_fact(context, args.fact_id)
    except ProcessingFailure as exc:
        print(json.dumps(exc.detail(), ensure_ascii=False))
        return 1
    except ExtractionError as exc:
        parser.exit(1, json.dumps(exc.detail(), ensure_ascii=False) + '\n')
    except DatabaseError as exc:
        parser.exit(1, f'Memory operation failed ({exc.code})\n')
    except (ImportError, ValueError):
        parser.exit(1, 'Invalid memory configuration or arguments\n')
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get('ok') is False else 0


if __name__ == '__main__':
    raise SystemExit(main())
