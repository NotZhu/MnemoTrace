"""Exercise product adapters with the same limited role as ordinary requests."""
import asyncio
from contextlib import contextmanager
import importlib
import json

import pytest

from mnemotrace import cli
from mnemotrace.consolidate.planning import WINDOW_PREFIX
from mnemotrace.llm import FakeLLM
from mnemotrace.service import MemoryService
from mnemotrace.store.postgres.maintenance import rebuild_graph
from tests.postgres.fact_fixtures import seed_fact
from tests.postgres.ontology_fixtures import ontology_reply, typed_result


@pytest.fixture
def product(runtime, user, accounts, spaces, monkeypatch):
    http = pytest.importorskip('fastapi.testclient')
    app = importlib.import_module('mnemotrace.server.app')
    space = spaces.create(user, 'Synthetic space')
    context = spaces.authorize(user, space.space_id)
    key = accounts.issue_key(user)

    def extract(prompt, system=''):
        if system.startswith('MNEMOTRACE_RELATION_RESOLUTION'):
            return ontology_reply(prompt, system)
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        return json.dumps(typed_result(dict(facts=[
            dict(categories=[], sensitive=False, time_kind='current', effective_date=None, retention='unknown', subject='Synthetic person', predicate='likes', object='Bluebird tea',
                 text='Synthetic person likes Bluebird tea',
                 source_ids=[source['id']])
            for source in data['pending']
        ], digests=[])))

    service = MemoryService(runtime, llm=FakeLLM(handler=extract))
    monkeypatch.setattr(app, '_svc', service)
    client = http.TestClient(app.app, headers={'Authorization': f'Bearer {key.value}'})
    yield service, context, key, client
    client.close()


def test_retired_ingestion_routes_and_mode_are_absent(product):
    service, context, _, client = product
    schema = client.get('/openapi.json').json()
    assert '/v1/import' not in schema['paths'] and '/v1/documents' not in schema['paths']
    props = schema['components']['schemas']['RememberReq']['properties']
    assert 'mode' not in props
    for path in ('/v1/import', '/v1/documents', '/v1/messages', '/v1/processing/flush'):
        assert client.post(path, json={'space_id': context.space_id}).status_code == 404
    body = {'space_id': context.space_id, 'session_id': 's', 'mode': 'live',
            'messages': [{'message_id': 'm', 'role': 'user', 'content': 'Synthetic dialogue'}]}
    assert client.post('/v1/remember', json=body).status_code == 422
    assert not service.memory(context).list_sources()
    assert not hasattr(service, 'import_') and not hasattr(service, 'import_document')


def test_http_space_and_operation_lifecycle(product, user, accounts, spaces):
    service, context, key, client = product
    scope = {'space_id': context.space_id}
    assert client.get('/v1/spaces').json()['spaces'][0]['space_id'] == context.space_id
    assert client.get('/v1/stats').status_code == 422
    created = client.post('/v1/spaces', json={'name': 'Empty second space'})
    assert created.status_code == 201
    sibling = created.json()['space_id']
    assert len(client.get('/v1/spaces').json()['spaces']) == 2
    assert client.patch(f'/v1/spaces/{context.space_id}', json={'name': 'Renamed'}).json()['space_id'] == context.space_id

    payload = dict(**scope, session_id='synthetic-thread', agent_id='synthetic-agent',
                   messages=[dict(message_id='msg-1', role='user', content='Synthetic person likes Bluebird tea')])
    forged = client.post('/v1/remember', json={**payload, 'user_id': 'someone-else'})
    assert forged.status_code == 422 and 'someone-else' not in forged.text
    accepted = client.post('/v1/remember', json=payload)
    assert accepted.status_code == 202
    assert accepted.json()['messages'][0]['sequence'] == 0
    operation_id = accepted.json()['task']['operation_id']
    source_id, = accepted.json()['source_ids']
    assert client.post('/v1/remember', json=payload).json()['source_ids'] == [source_id]
    progress = client.get('/v1/processing', params={**scope, 'session_id': 'synthetic-thread'}).json()
    assert progress['operations'] == [accepted.json()['task']]
    assert client.get(f'/v1/episodes/{source_id}', params=scope).json()['agent_id'] == 'synthetic-agent'

    assert service.memory(context)._processing.run_next_stage(operation_id)
    completed = client.post('/v1/remember', json=dict(payload, wait=True))
    assert completed.status_code == 200 and completed.json()['ok'], completed.text
    assert completed.json()['task']['operation_id'] == operation_id
    operation_path = f'/v1/operations/{operation_id}'
    status = client.get(operation_path, params=scope).json()
    assert 'stored_raw' not in status and status['core_committed']
    assert client.get(operation_path, params={'space_id': sibling}).status_code == 404
    assert client.post(operation_path + '/retry', json=scope).json()['operation_id'] == operation_id
    retrieved = client.post('/v1/recall', json={**scope, 'query': 'What does Synthetic person like?', 'lean': False})
    assert retrieved.status_code == 200 and 'Bluebird' in retrieved.text
    seed_fact(service.memory(spaces.authorize(user, sibling)), 'Synthetic person', 'likes', 'Bluebird coffee',
              text='Synthetic person likes Bluebird coffee')
    recall = {'query': 'What Bluebird drinks does Synthetic person like?'}
    isolated = client.post('/v1/recall', json={**scope, **recall}).json()
    assert 'Bluebird tea' in isolated['context'] and 'Bluebird coffee' not in isolated['context']
    fused = client.post('/v1/recall', json={**recall, 'space_ids': [context.space_id, sibling]}).json()
    assert set(fused['coverage_by_space']) == {context.space_id, sibling}
    assert f'## Space: {context.space_id}' in fused['context'] and 'Bluebird tea' in fused['context']
    assert f'## Space: {sibling}' in fused['context'] and 'Bluebird coffee' in fused['context']
    for invalid in ([], [sibling, sibling]):
        assert client.post('/v1/recall', json={**recall, 'space_ids': invalid}).status_code == 422
        with pytest.raises(ValueError):
            service.memory(context).recall_multi(invalid, recall['query'])
    facts = client.get('/v1/memories', params=scope).json()['facts']
    assert source_id in facts[0]['provenance']
    exported = client.get('/v1/export', params={**scope, 'include_sensitive': True}).json()
    assert exported['episodes'][0]['id'] == source_id and exported['facts']
    assert client.delete(f"/v1/facts/{facts[0]['id']}", params=scope).json()['ok']
    assert client.get(f'/v1/episodes/{source_id}', params=scope).status_code == 200

    outsider = accounts.create_user()
    try:
        hidden = spaces.create(outsider, 'Other owner')
        response = client.post('/v1/recall', json={'query': 'Bluebird',
                                                   'space_ids': [context.space_id, hidden.space_id]})
        assert response.status_code == 404 and response.json()['detail']['code'] == 'space_not_found'
    finally:
        accounts.delete_user(outsider)
    accounts.revoke_key(user, key.info.key_id)
    assert client.get('/v1/spaces').status_code == 401


def test_http_remember_requires_a_model_and_rejects_raw_only_fields(product):
    service, context, _, client = product
    service.llm = None
    scope = {'space_id': context.space_id}
    batch = dict(scope, session_id='chat', messages=[dict(message_id='m1', role='user', content='Synthetic extraction')])
    automatic = client.post('/v1/remember', json=batch)
    assert automatic.status_code == 503
    assert automatic.json()['detail']['stored_raw'] is False
    for invalid in ({**batch, 'scope': 'raw'}, {**batch, 'extract': False}, {**batch, 'processing': 'buffered'}):
        assert client.post('/v1/remember', json=invalid).status_code == 422
    assert not service.memory(context).list_sources()
    assert not service.memory(context).list_operations()


def test_graph_audit_reports_scoped_structural_issues_before_limiting(product, spaces):
    service, context, _, client = product
    scope = {'space_id': context.space_id}
    memory = service.memory(context)
    missing = seed_fact(memory, '我', 'works_at', 'Missing Ltd.', linked=True, sensitive=False, text='user works at Missing Ltd.')
    mismatch = seed_fact(memory, '我', 'based_in', 'Mismatch City', linked=True, sensitive=False, text='user based in Mismatch City')
    private = seed_fact(memory, '我', 'knows', 'Synthetic private person', linked=True, sensitive=True, text='user knows Synthetic private person')
    seed_fact(memory, 'this', 'path', 'fixture/demo.json', sensitive=False, text='this path fixture/demo.json')
    sibling = service.memory(spaces.authorize(context.user_id, spaces.create(context.user_id, 'Sibling').space_id))
    outside = seed_fact(sibling, '我', 'works_at', 'Outside Ltd.', linked=True, sensitive=False, text='user works at Outside Ltd.')
    with sibling.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = %s', (outside.id,))
    with memory.dao._transaction() as sql:
        sql.delete('graph_relations', 'fact_id = ANY(%s)', ([missing.id, private.id],))
        sql.update('graph_relations', {'predicate': 'wrong'}, 'fact_id = %s', (mismatch.id,))

    report = client.get('/v1/audit', params=scope).json()
    limited = client.get('/v1/audit', params={**scope, 'limit': 1}).json()
    assert report['checked']['facts'] == 3
    assert report['by_kind'] == {'missing_edge': 1, 'edge_mismatch': 1}
    assert limited['by_kind'] == report['by_kind']
    assert limited['total_findings'] == 2 and limited['truncated']
    assert [row['kind'] for row in limited['findings']] == ['missing_edge']
    assert [row['fact_id'] for row in limited['findings']] == [missing.id]
    assert 'Synthetic private person' not in str(report) and private.id not in str(report)
    cursor = None
    for _ in range(len(memory.facts())):
        next_cursor = rebuild_graph(memory.dao, after_id=cursor, limit=1)
        assert next_cursor is not None and (cursor is None or next_cursor > cursor)
        cursor = next_cursor
    assert rebuild_graph(memory.dao, after_id=cursor, limit=1) is None
    assert client.get('/v1/audit', params=scope).json()['findings'] == []


@pytest.mark.parametrize('stream', [False, True])
def test_chat_returns_operation_identity_after_acceptance(product, stream):
    service, context, _, client = product
    service.llm = FakeLLM(handler=lambda *_: 'Synthetic reply')
    response = client.post('/v1/chat/completions', json={
        'stream': stream, 'messages': [{'role': 'user', 'content': 'Synthetic chat'}],
        'memory': {'space_id': context.space_id, 'session_id': 'proxy-chat', 'recall': False},
    })
    assert response.status_code == 200
    if stream:
        end = next(line for line in response.text.splitlines() if '"finish_reason": "stop"' in line)
        metadata = json.loads(end.removeprefix('data: '))['mnemotrace']
    else:
        metadata = response.json()['mnemotrace']
    assert metadata['space_id'] == context.space_id
    assert metadata['remembered'] and metadata['remember_queued']
    assert len(metadata['source_ids']) == 2
    assert metadata['task']['state'] == 'pending'
    assert service.memory(context).operation(metadata['task']['operation_id']) is not None


def test_direct_and_proxy_mcp_and_cli_share_records(product, monkeypatch, capsys):
    httpx = pytest.importorskip('httpx')
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend

    service, context, key, client = product
    source = seed_fact(service.memory(context), 'Synthetic archive', 'access_code', 'Emerald', sensitive=False, text='Synthetic archive access code Emerald')
    seed_fact(service.memory(context), 'Synthetic garden', 'favorite_flower', 'Iris', sensitive=False, text='Synthetic garden favorite flower Iris')

    async def mcp_lifecycle():
        local = LocalBackend(context.user_id, context.space_id, service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app)) as http:
            remote = RemoteBackend('http://testserver', key.value, context.space_id, http)
            result = await local.remember(
                [{'message_id': 'mcp-1', 'role': 'user', 'content': 'Synthetic preference'}], 'mcp-thread')
            operation_id = result['task']['operation_id']
            assert service.memory(context)._processing.run_next_stage(operation_id)
            status = await remote.operation(operation_id)
            assert status['operation_id'] == operation_id and 'stored_raw' not in status
            recalled = await local.recall('What is the synthetic archive access code?', lean=False)
            assert recalled['status'] == 'evidence_only' and any('Emerald' in fact for fact in recalled['facts'])
            first = await remote.memories(categories=None, limit=1, offset=0, include_superseded=False)
            second = await local.memories(categories=None, limit=1, offset=1, include_superseded=False)
            assert first['facts_page']['next_offset'] == 1
            assert first['facts'][0]['id'] != second['facts'][0]['id']
            assert (await remote.delete_fact(source.id))['ok']
            return operation_id

    operation_id = asyncio.run(mcp_lifecycle())

    @contextmanager
    def use_database(args):
        assert args.user_id == context.user_id and args.space_id == context.space_id
        yield service, context

    monkeypatch.setattr(cli, 'local_space', use_database)
    cli_scope = ['--user-id', context.user_id, '--space-id', context.space_id]
    assert cli.main([*cli_scope, 'operation', operation_id]) == 0
    assert json.loads(capsys.readouterr().out)['core_committed']
    assert cli.main([*cli_scope, 'remember', 'Synthetic CLI source', '--session-id', 'cli']) == 0
    source_id, = json.loads(capsys.readouterr().out)['source_ids']
    assert service.memory(context).get_episode(source_id).content == 'Synthetic CLI source'
    fact = seed_fact(service.memory(context), 'Synthetic CLI archive', 'access_code', 'Amethyst', text='Synthetic CLI archive access code Amethyst')
    assert cli.main([*cli_scope, 'recall', 'What is the synthetic CLI archive access code?']) == 0
    assert 'Amethyst' in json.loads(capsys.readouterr().out)['context']
    assert cli.main([*cli_scope, 'delete-fact', fact.id, '--confirm']) == 0
    assert service.memory(context).get_fact(fact.id) is None


def test_streamable_mcp_authenticates_each_call(product, monkeypatch):
    http = pytest.importorskip('fastapi.testclient')
    pytest.importorskip('mcp')
    from mnemotrace.mcp import server
    from mnemotrace.mcp.__main__ import http_app

    service, context, key, _ = product
    monkeypatch.setattr(server, 'require_request_backend', False)
    monkeypatch.setattr(server.mcp, '_session_manager', None)
    monkeypatch.setattr(server.mcp.settings, 'stateless_http', False)
    headers = {'Authorization': f'Bearer {key.value}', 'X-MnemoTrace-Space-Id': context.space_id,
               'Accept': 'application/json, text/event-stream'}
    body = dict(jsonrpc='2.0', id=1, method='tools/call',
                params=dict(name='mnemotrace_stats', arguments=dict(response_format='json')))
    with http.TestClient(http_app(service), base_url='http://127.0.0.1:8765', headers=headers) as client:
        wrong = client.post('/mcp', json=body, headers={'X-MnemoTrace-Space-Id': 'unknown'})
        assert wrong.status_code == 404
        invalid = client.post('/mcp', json=body, headers={'Authorization': 'Bearer synthetic-invalid'})
        assert invalid.status_code == 401
        response = client.post('/mcp', json=body)
        assert response.status_code == 200, response.text
        data = (response.json() if response.headers['content-type'].startswith('application/json')
                else json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith('data: '))))
        result = json.loads(data['result']['content'][0]['text'])
        assert result['space_id'] == context.space_id
