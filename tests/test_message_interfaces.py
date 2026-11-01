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
        status = client.get('/v1/processing', headers=headers).json()['messages']
        assert len(status['jobs']) == 1 and 'content' not in json.dumps(status)


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
