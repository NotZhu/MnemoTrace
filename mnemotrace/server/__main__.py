"""启动 MnemoTrace HTTP 服务。"""

import argparse
import asyncio
import os
import sys

import uvicorn

from .app import create_app
from .settings import Settings


def main() -> int:
    """读取运行配置并启动本地 ASGI 进程。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    try:
        settings = Settings.from_env(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings),
            host=args.host,
            port=args.port,
            proxy_headers=False,
            access_log=False,
        )
    )
    # psycopg 的异步连接在 Windows 上需要 Selector 事件循环。
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(server.serve(), loop_factory=loop_factory)
    return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
