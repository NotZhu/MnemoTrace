"""Run MnemoTrace MCP over local stdio or authenticated Streamable HTTP."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


def http_app(service):
    from starlette.datastructures import Headers
    from starlette.responses import JSONResponse

    from .backends import LocalBackend
    from . import server
    from ..store.postgres.errors import AccountNotFound, AuthenticationError, DatabaseError, SpaceNotFound

    server.require_request_backend = True
    server.mcp.settings.stateless_http = True
    app = server.mcp.streamable_http_app()

    async def authorized(scope, receive, send):
        if scope['type'] != 'http':
            await app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        scheme, _, key = headers.get('authorization', '').partition(' ')
        space_id = headers.get('x-mnemotrace-space-id')
        try:
            if scheme.lower() != 'bearer' or not key:
                raise AuthenticationError('Invalid key')
            identity = await asyncio.to_thread(service.spaces.authenticate, key)
            current = await asyncio.to_thread(LocalBackend, identity.user_id, space_id, service)
        except (AuthenticationError, AccountNotFound):
            status, code = 401, 'invalid_key'
        except SpaceNotFound:
            status, code = 404, 'space_not_found'
        except ValueError:
            status, code = 422, 'invalid_parameter'
        except DatabaseError:
            status, code = 503, 'database_unavailable'
        else:
            token = server.request_backend.set(current)
            try:
                await app(scope, receive, send)
            finally:
                server.request_backend.reset(token)
            return
        await JSONResponse({'detail': {'code': code}}, status_code=status)(scope, receive, send)

    return authorized


def serve_http(service, host, port):
    import uvicorn

    uvicorn.run(http_app(service), host=host, port=port, lifespan='on')


def main():
    from ..credentials import CredentialError, resolve_key
    from ..store.postgres.errors import DatabaseError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--http', action='store_true')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--user-id', default=os.environ.get('MNEMOTRACE_USER_ID'))
    parser.add_argument('--space-id', default=os.environ.get('MNEMOTRACE_SPACE_ID'))
    parser.add_argument('--api-url', default=os.environ.get('MNEMOTRACE_API_URL'))
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument('--api-key')
    credentials.add_argument('--api-key-file')
    args = parser.parse_args()

    try:
        if args.http:
            if args.api_url or args.api_key or args.api_key_file or args.user_id or args.space_id:
                parser.error('--http receives caller credentials and space on each request')
            from ..postgres_service import MemoryService

            service = MemoryService.from_env()
            try:
                serve_http(service, args.host, args.port)
            finally:
                service.close()
            return

        if not args.space_id:
            parser.error('stdio requires --space-id or MNEMOTRACE_SPACE_ID')
        from .backends import LocalBackend, RemoteBackend
        from .server import mcp, set_backend

        if args.api_url:
            if args.user_id:
                parser.error('remote stdio identity is determined by the API key')
            key = resolve_key(key=args.api_key, key_file=args.api_key_file, required=True)
            backend = RemoteBackend(args.api_url, key, args.space_id)
        else:
            if not args.user_id:
                parser.error('local stdio requires --user-id or MNEMOTRACE_USER_ID')
            backend = LocalBackend(args.user_id, args.space_id)
        set_backend(backend)
        try:
            print(f'MnemoTrace MCP ready · {backend.describe()}', file=sys.stderr)
            mcp.run()
        finally:
            set_backend(None)
            if isinstance(backend, LocalBackend):
                backend.close()
    except (DatabaseError, CredentialError, ValueError):
        parser.error('MCP configuration or authorization failed')


if __name__ == '__main__':
    main()
