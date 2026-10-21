"""运行 忆痕 MCP server。

  # 本地记忆（零外部服务），走 stdio —— 用于 Claude Desktop / Claude Code / Cursor：
  python -m mnemotrace.mcp
  python -m mnemotrace.mcp --namespace work          # 独立的记忆命名空间

  # 代理一个运行中的 忆痕 HTTP 服务器（托管 / 多租户）：
  python -m mnemotrace.mcp --api-url http://localhost:8000 --api-key sk-alice-123

  # 改用 streamable HTTP 而非 stdio 提供服务：
  python -m mnemotrace.mcp --http --port 8765

Claude Desktop 配置（claude_desktop_config.json）：
  {"mcpServers": {"mnemotrace": {"command": "python", "args": ["-m", "mnemotrace.mcp"]}}}
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="python -m mnemotrace.mcp",
        description="MnemoTrace MCP server — long-term memory tools for any MCP client.")
    ap.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1", help="bind host for --http (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765, help="bind port for --http (default 8765)")
    ap.add_argument("--namespace", "-n", help="local memory namespace (default $MNEMOTRACE_NAMESPACE or 'me')")
    ap.add_argument("--api-url", help="proxy a running MnemoTrace server (default $MNEMOTRACE_API_URL); else local")
    credentials = ap.add_mutually_exclusive_group()
    credentials.add_argument("--api-key", help="Explicit Bearer key (default $MNEMOTRACE_API_KEY)")
    credentials.add_argument("--api-key-file", help="Private credential file for --api-url")
    args = ap.parse_args()

    from ..credentials import CredentialError, resolve_key

    try:
        remote = bool(args.api_url or os.environ.get("MNEMOTRACE_API_URL"))
        key = None
        if remote or args.api_key is not None or args.api_key_file:
            key = resolve_key(key=args.api_key, key_file=args.api_key_file, required=remote)
    except CredentialError as exc:
        ap.error(str(exc))

    if args.namespace:
        os.environ["MNEMOTRACE_NAMESPACE"] = args.namespace
    if args.api_url:
        os.environ["MNEMOTRACE_API_URL"] = args.api_url
    if key is not None:
        os.environ["MNEMOTRACE_API_KEY"] = key

    from .server import backend, mcp

    # 提前构建 backend，让配置错误（服务器不可达、缺少 embedder extra）在这里暴露，
    # 而不是发生在工具调用中途。所有状态输出到 stderr：stdout 是 stdio 的 JSON-RPC 通道。
    b = backend()
    print(f"mnemotrace_mcp ready · {b.describe()}", file=sys.stderr)
    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(f"  transport: streamable-http on http://{args.host}:{args.port}/mcp", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        print("  transport: stdio", file=sys.stderr)
        mcp.run()


if __name__ == "__main__":
    main()
