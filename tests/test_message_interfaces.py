"""The dialogue contract reaches HTTP and both MCP backends without weakening ACLs."""
import json

import pytest

from tests.test_message_pipeline import message, services as services


def test_http_dialogue_acl_conflict_and_strict_roles(services, monkeypatch):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc, _, _ = services()
    monkeypatch.setattr(appmod, '_svc', svc)
    monkeypatch.setenv('MNEMOTRACE_SPACES', json.dumps({'synthetic-key': {'home': 'u', 'read': ['u'], 'write': ['u']}}))
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', '')
    headers = {'Authorization': 'Bearer synthetic-key'}
    with TestClient(appmod.app) as client:
        payload = dict(session_id='s', messages=[message('a')])
        first = client.post('/v1/messages', headers=headers, json=payload)
        assert first.status_code == 202 and first.json()['accepted'] == 1
        repeated = client.post('/v1/messages', headers=headers, json=payload)
        assert repeated.json()['accepted'] == 0
        assert repeated.json()['messages'] == first.json()['messages']
        payload['messages'] = [message('a', 'conflict')]
        assert client.post('/v1/messages', headers=headers, json=payload).status_code == 409
        payload['messages'] = [message('b', role='tool')]
        assert client.post('/v1/messages', headers=headers, json=payload).status_code == 422
        payload['messages'] = [message('b')]
        payload['space'] = 'other'
        assert client.post('/v1/messages', headers=headers, json=payload).status_code == 403
        status = client.get('/v1/processing', headers=headers).json()
        assert len(status['jobs']) == 1 and 'content' not in json.dumps(status)


def test_shutdown_closes_service_even_when_a_namespace_is_corrupt(services, monkeypatch, caplog):
    from pathlib import Path
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc, _, _ = services()
    svc.add_fact('u', 'owner', 'owns', 'synthetic marker')
    manifest = Path(svc._path('u')) / 'manifest.json'
    data = json.loads(manifest.read_text(encoding='utf-8'))
    data.pop('snapshot_id')
    manifest.write_text(json.dumps(data), encoding='utf-8')
    original = manifest.read_bytes()
    monkeypatch.setattr(appmod, '_svc', svc)
    with TestClient(appmod.app):
        pass
    assert svc._closed and manifest.read_bytes() == original
    assert 'Shutdown flush failed' in caplog.text


@pytest.mark.asyncio
async def test_mcp_local_and_remote_use_same_messages(services):
    import httpx
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend
    svc, _, _ = services()
    local = LocalBackend(namespace='u', service=svc)
    result = await local.remember_messages([message('a')], 's', mode='historical')
    assert result['queued'] and result['messages'][0]['message_id'] == 'a'
    requests = []
    def respond(req):
        requests.append(req)
        return httpx.Response(202, json=result)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        remote = RemoteBackend('https://synthetic.invalid', 'synthetic-key', client)
        assert await remote.remember_messages([message('a')], 's', 'historical') == result
    assert requests[0].url.path == '/v1/messages'
    assert json.loads(requests[0].content)['messages'][0]['role'] == 'user'

def test_http_immediate_extraction_failure_is_reported_not_200(services, monkeypatch):
    """#10：/v1/messages 的 immediate 抽取失败必须让调用方看到（与 /v1/remember 一致），
    不能返回 200 或 202 顶层 ok=True 把失败当成功。"""
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    from mnemotrace.llm import FakeLLM

    def broken(prompt, system):
        raise RuntimeError('synthetic model failure')

    svc, _, _ = services()
    svc.llm = FakeLLM(handler=broken)
    monkeypatch.setattr(appmod, '_svc', svc)
    monkeypatch.setenv('MNEMOTRACE_SPACES', json.dumps({'synthetic-key': {'home': 'u', 'read': ['u'], 'write': ['u']}}))
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', '')
    headers = {'Authorization': 'Bearer synthetic-key'}
    with TestClient(appmod.app) as client:
        payload = dict(session_id='s', processing='immediate', messages=[message('a')])
        resp = client.post('/v1/messages', headers=headers, json=payload)
        # 失败必须以错误状态码暴露，而不是 200/202 的“成功”外观。
        assert resp.status_code >= 400, resp.status_code
        body = resp.json()
        detail = body.get('detail', {})
        assert detail.get('stored_raw') is True and detail.get('episode_ids')
        assert detail.get('code') in {'extraction_failed', 'processing_incomplete',
                                      'extraction_invalid_response', 'extraction_conflict'}
