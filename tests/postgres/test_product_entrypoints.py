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


@pytest.fixture
def product(runtime, user, accounts, spaces, monkeypatch):
    http = pytest.importorskip('fastapi.testclient')
    app = importlib.import_module('mnemotrace.server.app')
    space = spaces.create(user, 'Synthetic space')
    context = spaces.authorize(user, space.space_id)
    key = accounts.issue_key(user)

    def extract(prompt, system=''):
        data = json.loads(prompt.removeprefix(WINDOW_PREFIX))
        return json.dumps(dict(facts=[
            dict(subject='Synthetic person', predicate='likes', object='Bluebird tea',
                 text='Synthetic person likes Bluebird tea', assertion_kind='user_statement',
                 source_ids=[source['id']])
            for source in data['pending']
        ], working=[], summaries=[]))

    service = MemoryService(runtime, llm=FakeLLM(handler=extract))
    monkeypatch.setattr(app, '_svc', service)
    client = http.TestClient(app.app, headers={'Authorization': f'Bearer {key.value}'})
    yield service, context, key, client
    client.close()


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
    forged = client.post('/v1/messages', json={**payload, 'user_id': 'someone-else'})
    assert forged.status_code == 422 and 'someone-else' not in forged.text
    accepted = client.post('/v1/messages', json=payload)
    assert accepted.status_code == 202
    operation_id, = accepted.json()['messages'][0]['operation_ids']
    source_id, = accepted.json()['source_ids']
    assert client.post('/v1/messages', json=payload).json()['operation_ids'] == [operation_id]
    operation_path = f'/v1/operations/{operation_id}'
    status = client.get(operation_path, params=scope).json()
    assert status['stored_raw'] and not status['core_committed']
    assert client.get(operation_path, params={'space_id': sibling}).status_code == 404
    assert client.get(f'/v1/episodes/{source_id}', params=scope).json()['agent_id'] == 'synthetic-agent'
    assert client.post(operation_path + '/retry', json=scope).json()['operation_id'] == operation_id

    working = service.memory(context).remember_working('Synthetic working state', 'synthetic-thread')
    completed = client.post('/v1/processing/flush', json={**scope, 'session_id': 'synthetic-thread'})
    assert completed.json()['ok'], completed.text
    assert service.memory(context).working_memory(session_id='synthetic-thread')[0].id == working.id
    assert client.get(operation_path, params=scope).json()['core_committed']
    retrieved = client.post('/v1/recall', json={**scope, 'query': 'What does Synthetic person like?', 'lean': False})
    assert retrieved.status_code == 200 and 'Bluebird' in retrieved.text
    client.post('/v1/facts', json={'space_id': sibling, 'subject': 'Synthetic person',
                                  'predicate': 'likes', 'object': 'Bluebird coffee'})
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
    assert exported['episodes'][0]['id'] == source_id and exported['graph']['edges']
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


def test_http_allows_raw_and_manual_writes_without_a_model(product):
    service, context, _, client = product
    service.llm = None
    scope = {'space_id': context.space_id}
    automatic = client.post('/v1/remember', json={**scope, 'content': 'Synthetic extraction'})
    assert automatic.status_code == 503
    assert automatic.json()['detail']['stored_raw'] is False
    assert not service.memory(context).list_sources()
    raw = client.post('/v1/remember', json={**scope, 'content': 'Synthetic raw note', 'scope': 'raw'})
    assert raw.json()['status'] == 'stored' and raw.json()['source_ids']
    manual = client.post('/v1/facts', json={**scope, 'predicate': 'likes', 'object': 'Synthetic tea'})
    assert manual.status_code == 200 and manual.json()['id']


def test_audit_counts_scoped_findings_before_limiting_and_samples_clearable_slot(product, spaces):
    from mnemotrace.store.contracts import ChangeSet, CommitConditions, RecordWrite, StoredRecord
    from mnemotrace.store.postgres.dao import new_operation_id
    from mnemotrace.types import Entity, Fact

    service, context, _, client = product
    scope = {'space_id': context.space_id}
    dao = service.memory(context).dao
    values = [
        ('Synthetic person', 'likes', 'synthetic_machine_token'),
        ('Synthetic person', 'occupation', 'occupation'),
        ('Synthetic person', 'note', '[clipped value'),
        ('Synthetic person', 'path', 'fixture/demo.json'),
        ('Synthetic person', 'claim', 'a synthetic long statement ' * 6),
        ('this', 'color', 'a blue shade'),
        ('Synthetic session', 'finding', 'fixture/ignored.json'),
    ]
    values.extend(('Synthetic slot', 'occupation', f'candidate {index}') for index in range(3))
    writes = tuple(RecordWrite(StoredRecord.from_record(context, Fact(
        subject, predicate, obj, user_id=context.user_id, source='extracted',
    ))) for subject, predicate, obj in values)
    with dao.read_snapshot() as view:
        snapshot = view.snapshot
    orphan = RecordWrite(StoredRecord.from_record(
        context, Entity('Synthetic disconnected entity', user_id=context.user_id)))
    dao.commit(ChangeSet(CommitConditions(snapshot), new_operation_id(), writes=(*writes, orphan)))
    client.post('/v1/remember', json={**scope, 'scope': 'raw', 'content': '合成测试内容' * 100})
    sibling = spaces.create(context.user_id, 'Another audit space')
    client.post('/v1/facts', json={'space_id': sibling.space_id, 'predicate': 'likes', 'object': 'NOISE'})

    report = client.get('/v1/audit', params=scope).json()
    limited = client.get('/v1/audit', params={**scope, 'limit': 2}).json()
    assert report['checked']['facts'] == len(values)
    assert report['by_kind'] == dict(embedder_blind=1, empty_value=1, slot_overflow=1,
                                     machine_token=1, fragment=1, dangling_subject=1,
                                     unreduced_claim=1, code_artifact=1, orphan_entity=1)
    assert limited['by_kind'] == report['by_kind']
    assert limited['total_findings'] == 9 and limited['truncated']
    assert {row['kind'] for row in limited['findings']} == {'empty_value', 'embedder_blind'}
    slot = next(row for row in report['findings'] if row['kind'] == 'slot_overflow')
    assert slot['count'] == 3 and len(slot['samples']) == 3
    assert all(row['text'] and row['fact_id'] for row in slot['samples'])
    assert service.clear_slot(context, slot['subject'], slot['predicate'], slot['count'])['ok']
    assert 'slot_overflow' not in client.get('/v1/audit', params=scope).json()['by_kind']


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
    assert metadata['remembered'] and metadata['operation_ids']
    for operation_id in metadata['operation_ids']:
        operation = service.memory(context).operation(operation_id)
        assert operation.stored_raw and not operation.core_committed


def test_direct_and_proxy_mcp_and_cli_share_records(product, monkeypatch, capsys):
    httpx = pytest.importorskip('httpx')
    from mnemotrace.mcp.backends import LocalBackend, RemoteBackend

    service, context, key, client = product
    source = service.memory(context).add_fact('Synthetic archive', 'access_code', 'Emerald')
    service.memory(context).add_fact('Synthetic garden', 'favorite_flower', 'Iris')

    async def mcp_lifecycle():
        local = LocalBackend(context.user_id, context.space_id, service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app)) as http:
            remote = RemoteBackend('http://testserver', key.value, context.space_id, http)
            result = await local.remember_messages(
                [{'message_id': 'mcp-1', 'role': 'user', 'content': 'Synthetic preference'}], 'mcp-thread')
            operation_id, = result['operation_ids']
            assert (await remote.operation(operation_id))['stored_raw']
            assert (await remote.flush_processing('mcp-thread'))['ok']
            recalled = await local.recall('What is the synthetic archive access code?', lean=False)
            assert 'Emerald' in recalled['answer']
            first = await remote.memories(topic=None, limit=1, offset=0, include_superseded=False)
            second = await local.memories(topic=None, limit=1, offset=1, include_superseded=False)
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
    assert cli.main([*cli_scope, 'remember', 'Synthetic CLI source', '--session-id', 'cli', '--scope', 'raw']) == 0
    source_id, = json.loads(capsys.readouterr().out)['source_ids']
    assert service.memory(context).get_episode(source_id).content == 'Synthetic CLI source'
    fact = service.memory(context).add_fact('Synthetic CLI archive', 'access_code', 'Amethyst')
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
