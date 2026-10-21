"""Credential references survive config generation and optional config reuse."""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import shlex
from pathlib import Path

import pytest

from mnemotrace import agent_doctor as doctor
from mnemotrace import agent_setup as setup
from mnemotrace import credentials
from mnemotrace.agent_config import ConfigReadError, load_codex_server
from mnemotrace.credentials import CredentialError, credential_path, read_key, store_key

try:
    import tomllib as _parser
except ModuleNotFoundError:
    _parser = None


def _toml_args(text):
    parser = _parser if _parser is not None else pytest.importorskip("tomli")
    return parser.load(io.BytesIO(text.encode("utf-8")))["mcp_servers"]["mnemotrace"]["args"]


@pytest.fixture
def no_tomllib(monkeypatch):
    original = builtins.__import__

    def without_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ModuleNotFoundError("simulated Python 3.10")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_tomllib)


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "synthetic-config-key")


def test_generated_reference_survives_changed_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    reference = store_key("synthetic-config-key", directory=tmp_path / ".secrets with spaces")
    relative = str(Path(reference).relative_to(tmp_path))
    setup.main(["--client", "codex", "--api-key-file", relative])
    output = capsys.readouterr().out
    block = output.split("```toml\n", 1)[1].split("```", 1)[0]
    args = _toml_args(block)
    emitted = args[args.index("--api-key-file") + 1]
    assert Path(emitted).is_absolute()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert read_key(emitted) == "synthetic-config-key"
    assert "synthetic-config-key" not in output


@pytest.mark.parametrize("entry", ["hidden", "environment", "bootstrap"])
def test_multiline_config_install_without_tomllib(tmp_path, monkeypatch, capsys, no_tomllib, entry):
    reference = store_key("synthetic-config-key")
    path = tmp_path / "config.toml"
    args = ["-m", "mnemotrace.mcp", "--api-url", "http://localhost:8000", "--api-key-file", reference]
    unrelated = '[mcp_servers.other]\ncommand = "other"\nargs = ["keep"]\n'
    path.write_text(
        'model = "synthetic"\n[mcp_servers.mnemotrace]\ncommand = "python"\nargs = [\n'
        + "".join(f"  {json.dumps(arg, ensure_ascii=False)}, # comment\n" for arg in args)
        + "]\n" + unrelated, encoding="utf-8",
    )
    monkeypatch.setattr("getpass.getpass", lambda *a: "synthetic-config-key")
    command = ["--codex-config", str(path)]
    if entry == "bootstrap":
        run = setup.bootstrap_main
        command += ["--bootstrap-targets", "codex", "--no-doctor"]
    else:
        run = setup.main
        command += ["--install-codex"]
        if entry == "hidden":
            command += ["--prompt-api-key"]
    run(command)
    first = path.read_text(encoding="utf-8")
    run(command)
    assert path.read_text(encoding="utf-8") == first
    assert unrelated in first and first.startswith('model = "synthetic"\n')
    assert reference in _toml_args(first)
    assert len(list((tmp_path / ".mnemotrace/credentials").glob("*.key"))) == 1
    assert "synthetic-config-key" not in first + capsys.readouterr().out


def _recipe_args(output):
    for language, block in re.findall(r"```(bash|toml|json)\n(.*?)```", output, re.S):
        if language == "json":
            yield json.loads(block)["mcpServers"]["mnemotrace"]["args"]
        elif language == "toml":
            yield _toml_args(block)
        else:
            command = shlex.split(block)
            if command[:3] == ["claude", "mcp", "add-json"]:
                yield json.loads(command[-1])["args"]
            elif command[:3] == ["codex", "mcp", "add"]:
                yield command[command.index("--") + 2:]


@pytest.mark.parametrize("path_kind", ["relative", "spaces", "home"])
@pytest.mark.parametrize("client", ["all", "codex", "claude-code", "cursor"])
def test_cli_and_direct_recipes_share_absolute_paths(tmp_path, monkeypatch, capsys, path_kind, client):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ("private with spaces" if path_kind == "spaces" else "private")
    reference = store_key("synthetic-config-key", directory=directory)
    relative = Path(reference).relative_to(tmp_path).as_posix()
    supplied = "~/" + relative if path_kind == "home" else relative
    before = set(tmp_path.rglob("*"))
    setup.main(["--client", client, "--api-key-file", supplied])
    outputs = [capsys.readouterr().out, setup.render_markdown(
        client, api_key_file=supplied,
    )]
    for output in outputs:
        recipes = list(_recipe_args(output))
        assert recipes
        for args in recipes:
            assert args[args.index("--api-key-file") + 1] == reference
        assert "synthetic-config-key" not in output
    assert set(tmp_path.rglob("*")) == before
    command = setup._doctor_verify_command(
        client=client, python_cmd="python", api_url="http://localhost:8000",
        namespace=None, api_key_file=supplied,
    )
    assert command[-1] == reference


def test_direct_generators_do_not_create_files_and_keep_placeholder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for value in (None, "<private-credential-file>", "not-created/key"):
        args = setup.mcp_args(api_url="http://localhost:8000", namespace=None, api_key_file=value)
        block = setup.codex_toml_block(
            api_url="http://localhost:8000", namespace=None,
            api_key_file=value, python_cmd="python",
        )
        assert _toml_args(block) == args
        assert args[-1] == (str(tmp_path / "not-created/key") if value == "not-created/key" else "<private-credential-file>")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("installer", [setup.install_codex_config, setup.install_mcp_json_config])
def test_installers_expand_home_and_retain_absolute_reference(tmp_path, monkeypatch, installer):
    reference = store_key("synthetic-config-key", directory=tmp_path / "private files")
    supplied = "~/" + Path(reference).relative_to(tmp_path).as_posix()
    path = tmp_path / "client.config"
    installed = installer(config_path=str(path), api_key_file=supplied)
    assert installed["api_key_file"] == reference
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert read_key(installed["api_key_file"]) == "synthetic-config-key"


@pytest.mark.parametrize("fallback", [False, True], ids=["native", "without-tomllib"])
@pytest.mark.parametrize("body, expected", [
    ('command = "python"\nargs = ["-m", "mnemotrace.mcp"]',
     {"command": "python", "args": ["-m", "mnemotrace.mcp"]}),
    ("command = 'C:\\Program Files\\Python\\python.exe' # comment\n"
     "args = [\n  '-m', # comment\n  'mnemotrace.mcp',\n  'C:\\private # files\\key',\n] # end",
     {"command": r"C:\Program Files\Python\python.exe", "args": ["-m", "mnemotrace.mcp", r"C:\private # files\key"]}),
    (r'command = "C:\\Python\\python.exe"' + "\n" + r'args = ["a#b", "quote\"#text", "tab\t", "\u4e2d\U0001F600",]',
     {"command": r"C:\Python\python.exe", "args": ["a#b", 'quote"#text', "tab\t", "中😀"]}),
    ('command = "python"\nargs = [\n# empty array\n]', {"command": "python", "args": []}),
])
def test_shared_reader_launch_formats(tmp_path, request, fallback, body, expected):
    if fallback:
        request.getfixturevalue("no_tomllib")
    path = tmp_path / "config.toml"
    path.write_text(
        '# [mcp_servers.mnemotrace]\n[unrelated]\nargs = ["before"]\n'
        '[mcp_servers.mnemotrace] # target\n' + body
        + '\n[mcp_servers.other]\ncommand = "other"\nargs = ["after"]\n', encoding="utf-8",
    )
    assert load_codex_server(path) == expected


@pytest.mark.parametrize("expression", ['["synthetic-value" "missing-comma"]', '[42]', '"not-an-array"', '["bad\\q"]'])
def test_unsupported_launch_values_have_safe_errors(tmp_path, no_tomllib, expression):
    path = tmp_path / "config.toml"
    path.write_text('[mcp_servers.mnemotrace]\ncommand = "python"\nargs = ' + expression, encoding="utf-8")
    with pytest.raises(ConfigReadError) as exc:
        load_codex_server(path)
    assert expression not in str(exc.value)
    report = doctor._codex_config_probe(str(path), python_cmd="python", api_url=None, api_key=None)
    assert report["status"] == "fail" and "Unsupported or invalid" in report["detail"]


def test_unsupported_optional_reuse_does_not_block_install(tmp_path, no_tomllib):
    path = tmp_path / "config.toml"
    # Legal TOML, deliberately outside the fallback's launch-string subset.
    untouched = '[mcp_servers.other]\ncommand = "other"\n'
    path.write_text('[mcp_servers.mnemotrace]\ncommand = """python"""\nargs = []\n' + untouched, encoding="utf-8")
    report = doctor._codex_config_probe(str(path), python_cmd="python", api_url=None, api_key=None)
    assert report["status"] == "fail" and "supports" in report["detail"]
    installed = setup.install_codex_config(config_path=str(path), api_key="synthetic-config-key")
    assert read_key(installed["api_key_file"]) == "synthetic-config-key"
    assert untouched in path.read_text(encoding="utf-8")
    again = setup.install_codex_config(config_path=str(path), api_key="synthetic-config-key")
    assert not again["changed"] and again["api_key_file"] == installed["api_key_file"]
    assert len(list((tmp_path / ".mnemotrace/credentials").glob("*.key"))) == 1


@pytest.mark.parametrize("failure", ["missing", "invalid", "permissions"])
def test_recognized_reference_safety_failure_still_blocks_install(tmp_path, monkeypatch, no_tomllib, failure):
    reference = Path(store_key("synthetic-config-key"))
    path = tmp_path / "config.toml"
    args = ["-m", "mnemotrace.mcp", "--api-url", "http://localhost:8000", "--api-key-file", str(reference)]
    path.write_text('[mcp_servers.mnemotrace]\ncommand = "python"\nargs = [\n'
                    + "".join(json.dumps(arg, ensure_ascii=False) + ",\n" for arg in args) + "]\n", encoding="utf-8")
    before = path.read_bytes()
    if failure == "missing":
        reference.unlink()
    elif failure == "invalid":
        credentials.private_write(reference, b"synthetic\ninvalid")
    else:
        original = credentials.check_private

        def unsafe_file(candidate):
            if candidate == reference:
                raise CredentialError("Synthetic unsafe permission failure.")
            original(candidate)

        monkeypatch.setattr(credentials, "check_private", unsafe_file)
    files = set(tmp_path.rglob("*"))
    with pytest.raises(CredentialError):
        setup.install_codex_config(config_path=str(path), api_key="synthetic-config-key")
    assert path.read_bytes() == before and set(tmp_path.rglob("*")) == files


def test_link_reference_is_not_resolved_before_rejection(tmp_path, monkeypatch, no_tomllib):
    reference = Path(store_key("synthetic-config-key"))
    alias = tmp_path / "alias"
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(reference.parent), str(alias))
        else:
            alias.symlink_to(reference.parent, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) in (5, 1314):
            pytest.skip("link creation requires Windows privileges")
        raise
    try:
        monkeypatch.chdir(tmp_path)
        supplied = str(Path("alias") / reference.name)
        absolute = credential_path(supplied)
        assert absolute.parent == alias
        args = setup.mcp_args(api_url="http://localhost:8000", namespace=None, api_key_file=supplied)
        assert args[-1] == str(absolute)
        with pytest.raises(CredentialError):
            read_key(args[-1])
        path = tmp_path / "config.toml"
        path.write_text('[mcp_servers.mnemotrace]\ncommand = "python"\nargs = ' + json.dumps(args, ensure_ascii=False), encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(CredentialError):
            setup.install_codex_config(config_path=str(path), api_key="synthetic-config-key")
        assert path.read_bytes() == before
    finally:
        alias.rmdir() if os.name == "nt" else alias.unlink()
