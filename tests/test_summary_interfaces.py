"""Original-event access across HTTP/MCP, without task or embedding payloads."""
import json

import pytest

from tests.test_pipeline_sources import LONG, services as services


def test_explicit_original_http_enforces_space_acl_and_omits_internal_state(services, monkeypatch):
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod
    svc, _ = services()
    result = svc.remember('team', LONG)
    monkeypatch.setenv('MNEMOTRACE_SPACES', json.dumps({
        'sk-agent': {'home': 'u', 'read': ['u', 'team']}, 'sk-other': {'home': 'other'}}))
    monkeypatch.setenv('MNEMOTRACE_API_KEYS', '')
    monkeypatch.setenv('MNEMOTRACE_OPEN', '')
    monkeypatch.setenv('MNEMOTRACE_ALLOW_ANONYMOUS', '')
    monkeypatch.setattr(appmod, '_svc', svc)
    with TestClient(appmod.app) as client:
        path = '/v1/episodes/' + result['episode_id']
        headers = {'Authorization': 'Bearer sk-agent'}
        assert client.get(path, headers=headers).status_code == 404
        response = client.get(path, params={'space': 'team'}, headers=headers)
        assert response.status_code == 200 and response.json()['content'] == LONG
        assert set(response.json()) == {'id', 'space', 'session_id', 'role', 'event_time', 'ingested_at',
                                        'content', 'message_id', 'sequence', 'mode', 'coverage'}
        assert client.get(path, params={'space': 'team'}, headers={
            'Authorization': 'Bearer sk-other'}).status_code == 403
        assert client.get('/v1/episodes/missing', headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_local_and_remote_mcp_explicit_original_contract(services):
    import httpx
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend
    svc, _ = services()
    result = svc.remember('u', 'synthetic original', scope='working')
    local = LocalBackend(namespace='u', service=svc)
    assert (await local.get_episode(result['episode_id']))['content'] == 'synthetic original'
    with pytest.raises(ValueError):
        await local.get_episode(result['episode_id'], 'other')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'content': 'synthetic original'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        remote = RemoteBackend('https://synthetic.invalid', 'synthetic-key', client)
        await remote.get_episode('event/id', 'shared space')
        await remote.flush_processing(include_summaries=True)
    assert requests[0].url.params['space'] == 'shared space'
    assert requests[0].url.raw_path.startswith(b'/v1/episodes/event%2Fid?')
    assert json.loads(requests[1].content)['include_summaries'] is True




@pytest.mark.asyncio
async def test_mcp_tools_report_independent_summary_failure_and_original_source_times(services):
    from mnemotrace.mcp import server as tools
    from mnemotrace.mcp.backends import LocalBackend
    from tests.test_mcp import call
    svc, _ = services(buffered=True)
    tools.set_backend(LocalBackend(namespace='u', service=svc))
    try:
        response = await call('mnemotrace_remember', content=LONG)
        assert 'Summarization: pending' in response
        episode = next(iter(svc.get('u').episodes_doc.values()))
        original = await call('mnemotrace_get_episode', episode_id=episode.id)
        assert LONG in original and str(episode.event_time) in original and str(episode.ingested_at) in original
    finally:
        tools.set_backend(None)
