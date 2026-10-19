"""忆痕 MCP server — 为任何 Model Context Protocol 客户端提供长期记忆工具。

    pip install "mnemotrace-memory[mcp]"
    python -m mnemotrace.mcp

工具见 mnemotrace/mcp/server.py，本地/远程模式见 mnemotrace/mcp/backends.py。
"""
from __future__ import annotations

__all__ = ["mcp", "backend", "set_backend", "make_backend",
           "Backend", "LocalBackend", "RemoteBackend"]


def __getattr__(name: str):  # PEP 562 — 推迟导入 MCP SDK，直到真正使用
    if name in ("mcp", "backend", "set_backend"):
        from . import server

        return getattr(server, name)
    if name in ("make_backend", "Backend", "LocalBackend", "RemoteBackend"):
        from . import backends

        return getattr(backends, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
