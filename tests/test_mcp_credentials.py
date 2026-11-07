"""Exercise the actual CLI entry without importing optional MCP transports."""
import sys
from types import SimpleNamespace

import pytest

from mnemotrace.credentials import store_key
from mnemotrace.mcp.__main__ import main


def test_mcp_launch_uses_private_file_not_argv(tmp_path, monkeypatch, capsys):
    key = "synthetic-mcp-key"
    reference = store_key(key, directory=tmp_path / "credentials")
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "synthetic-wrong-key")
    monkeypatch.delenv("MNEMOTRACE_API_URL", raising=False)
    observed = []
    fake = SimpleNamespace(set_backend=lambda _: None, mcp=SimpleNamespace(run=lambda: None))

    def remote(url, credential, space_id):
        observed.append((url, credential, space_id))
        return SimpleNamespace(describe=lambda: "synthetic remote")

    monkeypatch.setitem(sys.modules, "mnemotrace.mcp.server", fake)
    monkeypatch.setattr("mnemotrace.mcp.backends.RemoteBackend", remote)
    monkeypatch.setattr(sys, "argv", ["mcp", "--space-id", "synthetic-space", "--api-url", "http://example.invalid", "--api-key-file", reference])
    main()
    assert observed == [("http://example.invalid", key, "synthetic-space")]
    assert key not in str(sys.argv) + capsys.readouterr().err


def test_mcp_missing_file_does_not_start_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "synthetic-fallback")
    monkeypatch.setattr(sys, "argv", ["mcp", "--space-id", "synthetic-space", "--api-url", "http://example.invalid", "--api-key-file", str(tmp_path / "missing")])
    with pytest.raises(SystemExit):
        main()
    assert "synthetic-fallback" not in capsys.readouterr().err
