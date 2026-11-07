"""Agent diagnostics probe the explicit runtime and authorized space without writing memories."""
import json
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

from mnemotrace import agent_doctor as doctor
from mnemotrace.credentials import store_key


def test_runtime_exports_postgres_entry_and_operation_tools(tmp_path, monkeypatch):
    pytest.importorskip('mcp')
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.chdir(tmp_path)
    probe = doctor._python_probe(sys.executable)
    assert probe['status'] == 'ok', probe
    assert probe['data'] == {'missing_tools': [], 'postgres_entry': 'PostgresMemory'}


@pytest.mark.parametrize('remote', [False, True])
def test_stdio_diagnostic_uses_explicit_scope_and_keeps_key_out_of_command(monkeypatch, remote):
    def run(parts, timeout, env):
        assert 'synthetic-secret' not in ' '.join(parts)
        assert env['MNEMOTRACE_SPACE_ID'] == 'space-test'
        if remote:
            assert env['MNEMOTRACE_API_KEY'] == 'synthetic-secret'
            assert env['MNEMOTRACE_API_URL'] == 'http://test.invalid'
            assert 'MNEMOTRACE_USER_ID' not in env
        else:
            assert env['MNEMOTRACE_USER_ID'] == 'local-owner'
            assert 'MNEMOTRACE_API_KEY' not in env
        return subprocess.CompletedProcess(parts, 0, stdout='{"space_id":"space-test","storage":"postgres"}')

    monkeypatch.setattr(doctor, '_run', run)
    result = doctor._stdio_probe('python', space_id='space-test', user_id='local-owner',
                                 api_url='http://test.invalid' if remote else None, api_key='synthetic-secret')
    assert result['status'] == 'ok'


def test_private_file_credential_is_redacted_from_remote_failure(tmp_path, monkeypatch, capsys):
    key = 'synthetic-doctor-secret'
    reference = store_key(key, directory=tmp_path / 'credentials')
    observed = []
    monkeypatch.setattr(doctor, '_python_probe', lambda *_: {'name': 'runtime', 'status': 'ok'})
    monkeypatch.setattr(doctor, '_stdio_probe', lambda *a, **k: {'name': 'stdio', 'status': 'ok'})

    def remote(url, value, space_id):
        observed.append((url, value, space_id))
        return {'name': 'remote', 'status': 'fail', 'detail': value}

    monkeypatch.setattr(doctor, '_remote_http_probe', remote)
    with pytest.raises(SystemExit) as stopped:
        doctor.main(['--api-url', 'http://test.invalid', '--api-key-file', reference, '--space-id', 'space-test', '--json'])
    assert stopped.value.code == 1
    assert observed == [('http://test.invalid', key, 'space-test')]
    output = capsys.readouterr().out
    assert key not in output and '<REDACTED>' in output


@pytest.mark.parametrize('remote', [False, True])
def test_agent_configuration_checks_python_identity_and_space(tmp_path, remote):
    key = 'synthetic-remote' if remote else None
    api_url = 'http://test.invalid' if remote else None
    user = None if remote else 'local-owner'
    args = ['-m', 'mnemotrace.mcp', '--space-id', 'space-test']
    if remote:
        reference = store_key(key, directory=tmp_path / 'credentials')
        args += ['--api-url', api_url, '--api-key-file', reference]
    else:
        args += ['--user-id', user]
    config = tmp_path / '.mcp.json'
    config.write_text(json.dumps({'mcpServers': {'mnemotrace': {'command': 'python', 'args': args}}}))
    values = dict(server_name='mnemotrace', api_url=api_url, api_key=key, user_id=user, space_id='space-test')
    assert doctor._mcp_json_config_probe(str(config), python_cmd='python', **values)['status'] == 'ok'
    assert doctor._mcp_json_config_probe(str(config), python_cmd='wrong', **values)['status'] == 'fail'
    assert doctor._mcp_json_config_probe(str(config), python_cmd='python', **{**values, 'space_id': 'other'})[
        'status'] == 'fail'


def test_remote_diagnostic_reports_401_without_echoing_identity(monkeypatch):
    def denied(url, key, path, params):
        assert path == '/v1/stats' and params == {'space_id': 'space-test'}
        raise urllib.error.HTTPError(url, 401, 'synthetic-secret', {}, None)
    monkeypatch.setattr(doctor, '_http_get_json', denied)
    result = doctor._remote_http_probe('http://test.invalid', 'synthetic-secret', 'space-test')
    assert result['status'] == 'fail' and result['detail'] == 'HTTP 401'
