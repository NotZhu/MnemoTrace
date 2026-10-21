"""Validate generated UTF-8 TOML with an independent parser."""
from __future__ import annotations

import builtins
import io
import re
from pathlib import Path

import pytest

from mnemotrace import agent_doctor as doctor
from mnemotrace import agent_setup as setup
from mnemotrace.agent_config import load_codex_server
from mnemotrace.credentials import CredentialError, read_key, store_key

try:
    import tomllib as _parser
except ModuleNotFoundError:
    _parser = None


def _parse(text):
    parser = _parser if _parser is not None else pytest.importorskip("tomli")
    return parser.load(io.BytesIO(text.encode("utf-8")))["mcp_servers"]["mnemotrace"]


@pytest.mark.parametrize("value", [
    "ascii", "中文", "😀", "𠀀", "space # value", 'quote" and backslash\\',
    "".join(chr(code) for code in range(32)) + "\x7f",
])
def test_command_and_local_args_roundtrip_as_utf8_toml(value):
    block = setup.codex_toml_block(api_url=None, namespace=value, python_cmd=value)
    parsed = _parse(block)
    assert parsed == {"command": value, "args": ["-m", "mnemotrace.mcp", "--namespace", value]}


@pytest.mark.parametrize("field", ["api_url", "api_key_file"])
@pytest.mark.parametrize("segment", ["中文", "😀", "𠀀", "with spaces"])
def test_remote_parameters_roundtrip_without_writing(tmp_path, field, segment):
    options = dict(api_url="http://example.invalid", api_key_file=None, namespace=None)
    options[field] = f"http://example.invalid/{segment}" if field == "api_url" else str(tmp_path / segment / "server.key")
    expected = setup.mcp_args(**options)
    block = setup.codex_toml_block(python_cmd="python", **options)
    assert _parse(block) == {"command": "python", "args": expected}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field", ["python_cmd", "namespace"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff", "\ud83d\ude00"], ids=["high", "low", "pair-code-units"])
def test_surrogate_code_units_fail_with_safe_error(field, surrogate):
    options = dict(api_url=None, namespace="synthetic", python_cmd="python")
    options[field] = "synthetic-prefix" + surrogate
    with pytest.raises(CredentialError) as stopped:
        setup.codex_toml_block(**options)
    assert str(stopped.value) == "Codex config strings must contain valid Unicode scalar values."


def test_surrogate_does_not_overwrite_config_or_backup(tmp_path, capsys):
    reference = store_key("synthetic-unicode-key", directory=tmp_path / "credentials")
    path = tmp_path / "config.toml"
    path.write_text('model = "synthetic"\n', encoding="utf-8")
    before = path.read_bytes()
    files = set(tmp_path.rglob("*"))
    with pytest.raises(CredentialError):
        setup.install_codex_config(config_path=str(path), api_key_file=reference, python_cmd="synthetic\ud800")
    with pytest.raises(SystemExit) as stopped:
        setup.main(["--install-codex", "--codex-config", str(path), "--api-key-file", reference,
                    "--python", "synthetic\ud800"])
    assert str(stopped.value) == "Codex config strings must contain valid Unicode scalar values."
    assert path.read_bytes() == before and set(tmp_path.rglob("*")) == files
    assert not capsys.readouterr().out


@pytest.mark.parametrize("fallback", [False, True], ids=["native", "without-tomllib"])
@pytest.mark.parametrize("credential_source", ["home", "explicit"])
@pytest.mark.parametrize("entry", ["direct", "cli", "bootstrap", "dry-run"])
def test_unicode_install_reuse_and_reader_paths(tmp_path, monkeypatch, capsys, fallback, credential_source, entry):
    home = tmp_path / "用户😀𠀀"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    key = "synthetic-unicode-key"
    monkeypatch.setenv("MNEMOTRACE_API_KEY", key)
    reference = store_key(key, directory=home / "显式😀 credentials") if credential_source == "explicit" else None
    if fallback:
        original = builtins.__import__

        def no_tomllib(name, *args, **kwargs):
            if name == "tomllib":
                raise ModuleNotFoundError("simulated Python 3.10")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_tomllib)
    path = home / "config.toml"
    untouched = '[mcp_servers.other]\ncommand = "保持😀"\n'
    path.write_text('model = "合成"\n' + untouched, encoding="utf-8")
    before = path.read_bytes()
    files = set(home.rglob("*"))
    python = str(home / "Python😀" / "python.exe")

    def install():
        if entry == "direct":
            return setup.install_codex_config(config_path=str(path), api_key_file=reference, python_cmd=python)["block"]
        command = ["--codex-config", str(path), "--python", python]
        if reference:
            command += ["--api-key-file", reference]
        command += ["--bootstrap", "--bootstrap-targets", "codex", "--no-doctor"] if entry == "bootstrap" else ["--install-codex"]
        if entry == "dry-run":
            command += ["--dry-run"]
        setup.main(command)
        output = capsys.readouterr().out
        return re.search(r"```toml\n(.*?)```", output, re.S).group(1) if entry != "bootstrap" else path.read_text(encoding="utf-8")

    block = install()
    server = _parse(block)
    assert server["command"] == python
    if entry == "dry-run":
        assert path.read_bytes() == before and set(home.rglob("*")) == files
        assert server["args"][-1] == (reference or "<private-credential-file>")
        return
    first = path.read_bytes()
    installed_files = set(home.rglob("*"))
    assert untouched in first.decode("utf-8")
    assert _parse(first.decode("utf-8")) == server == load_codex_server(path)
    saved_reference = server["args"][-1]
    assert read_key(saved_reference) == key
    install()
    assert path.read_bytes() == first and set(home.rglob("*")) == installed_files
    check = doctor._codex_config_probe(str(path), python_cmd=python, api_url="http://localhost:8000", api_key=key)
    assert check["status"] == "ok"
    snapshot = set(home.rglob("*"))
    setup.main(["--client", "codex", "--api-key-file", saved_reference, "--python", python])
    snippet = re.search(r"```toml\n(.*?)```", capsys.readouterr().out, re.S).group(1)
    assert _parse(snippet) == server and set(home.rglob("*")) == snapshot
