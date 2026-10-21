"""Read the MCP launch fields shared by installation and diagnostics.

The Python 3.10 fallback is deliberately a reader for string launch fields, not a
general TOML implementation. Unsupported syntax is explicit, so callers can skip
optional reference reuse without treating an unverified config as healthy.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from .credentials import read_config


class ConfigReadError(ValueError):
    """Config syntax cannot be read safely; never include config values in errors."""


def _unsupported() -> ConfigReadError:
    return ConfigReadError(
        "Unsupported or invalid MCP config syntax. The Python 3.10 reader supports "
        "command strings and args string arrays with single-line quoted strings."
    )


def _statements(text: str) -> Iterator[str]:
    """Collect complete statements before parsing; comments only exist outside quotes."""
    buffer: list[str] = []
    brackets: list[str] = []
    quote = ""
    escaped = False
    comment = False
    for index, char in enumerate(text + "\n"):
        if comment and char != "\n":
            continue
        if quote:
            if char in "\r\n":
                raise _unsupported()
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char == "#":
            comment = True
            continue
        if char in "\"'":
            if text[index:index + 3] == char * 3:
                raise _unsupported()
            quote = char
        elif char in "[{":
            brackets.append(char)
        elif char in "]}":
            expected = "[" if char == "]" else "{"
            if not brackets or brackets.pop() != expected:
                raise _unsupported()
        if char == "\n":
            comment = False
            if not brackets:
                statement = "".join(buffer).strip()
                buffer.clear()
                if statement:
                    yield statement
                continue
        buffer.append(char)
    if quote or brackets:
        raise _unsupported()


class _LaunchValue:
    def __init__(self, text: str):
        self.text = text
        self.position = 0

    def _space(self) -> None:
        while self.position < len(self.text) and self.text[self.position] in " \t\r\n":
            self.position += 1

    def _string(self) -> str:
        self._space()
        if self.position == len(self.text) or self.text[self.position] not in "\"'":
            raise _unsupported()
        quote = self.text[self.position]
        self.position += 1
        result: list[str] = []
        escapes = {'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r', '"': '"', '\\': '\\'}
        while self.position < len(self.text):
            char = self.text[self.position]
            self.position += 1
            if char == quote:
                return "".join(result)
            if (ord(char) < 32 and char != "\t") or ord(char) == 127:
                raise _unsupported()
            if char == "\\" and quote == '"':
                if self.position == len(self.text):
                    raise _unsupported()
                char = self.text[self.position]
                self.position += 1
                if char in ("u", "U"):
                    size = 4 if char == "u" else 8
                    digits = self.text[self.position:self.position + size]
                    if len(digits) != size or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise _unsupported()
                    code = int(digits, 16)
                    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                        raise _unsupported()
                    char = chr(code)
                    self.position += size
                elif char in escapes:
                    char = escapes[char]
                else:
                    raise _unsupported()
            result.append(char)
        raise _unsupported()

    def parse(self, *, array: bool) -> str | list[str]:
        self._space()
        if not array:
            result: str | list[str] = self._string()
        else:
            if not self.text.startswith("[", self.position):
                raise _unsupported()
            self.position += 1
            items: list[str] = []
            self._space()
            while not self.text.startswith("]", self.position):
                items.append(self._string())
                self._space()
                if self.text.startswith(",", self.position):
                    self.position += 1
                    self._space()
                elif not self.text.startswith("]", self.position):
                    raise _unsupported()
            self.position += 1
            result = items
        self._space()
        if self.position != len(self.text):
            raise _unsupported()
        return result


def _load_launch_fields(text: str) -> dict:
    target = False
    found = False
    fields: dict = {}
    for statement in _statements(text):
        if statement.startswith("["):
            if not re.fullmatch(r"\[[\w.-]+\]", statement, flags=re.ASCII):
                raise _unsupported()
            target = statement == "[mcp_servers.mnemotrace]"
            if target:
                if found:
                    raise _unsupported()
                found = True
            continue
        if not target:
            continue
        key, separator, raw = statement.partition("=")
        key = key.strip()
        if not separator:
            raise _unsupported()
        if key in ("command", "args"):
            if key in fields:
                raise _unsupported()
            fields[key] = _LaunchValue(raw).parse(array=key == "args")
        elif key.strip("\"'") in ("command", "args"):
            raise _unsupported()
    if not found:
        raise KeyError("[mcp_servers.mnemotrace]")
    return fields


def load_codex_server(path: Path) -> dict:
    """Read launch fields; syntax errors are distinct from credential safety errors."""
    text = read_config(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    try:
        import tomllib
    except ModuleNotFoundError:
        return _load_launch_fields(text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigReadError("Invalid TOML in MCP config; launch fields could not be verified.") from exc
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ConfigReadError("MCP server config must be a table.")
    server = servers.get("mnemotrace")
    if server is None:
        raise KeyError("[mcp_servers.mnemotrace]")
    if not isinstance(server, dict):
        raise ConfigReadError("MCP launch config must be a table.")
    if "command" in server and not isinstance(server["command"], str):
        raise ConfigReadError("MCP command must be a string.")
    if "args" in server and (
        not isinstance(server["args"], list) or not all(isinstance(arg, str) for arg in server["args"])
    ):
        raise ConfigReadError("MCP args must be a string array.")
    return server
