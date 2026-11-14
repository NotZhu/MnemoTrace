"""Postgres consumers that previously exercised a filesystem-backed service."""
import json

import pytest

from eval import seed_console
from mnemotrace import quickstart
from mnemotrace.llm import providers
from tests.model_fixtures import RecordedLLM


def configure(monkeypatch, cluster, context):
    monkeypatch.setenv('MNEMOTRACE_DATABASE_URL', cluster.runtime.dsn)
    monkeypatch.setenv('MNEMOTRACE_DB_ROLE_PREFIX', cluster.runtime.role_prefix)
    monkeypatch.setenv('MNEMOTRACE_USER_ID', context.user_id)
    monkeypatch.setenv('MNEMOTRACE_SPACE_ID', context.space_id)
    monkeypatch.setenv('MNEMOTRACE_LLM', 'synthetic')
    monkeypatch.setenv('MNEMOTRACE_EMBEDDER', 'lexical')
    monkeypatch.setattr(providers, 'make_llm', lambda *_: RecordedLLM())


def test_quickstart_targets_existing_space_and_reuses_messages(
        cluster, product_service, product_scope, monkeypatch, capsys):
    scope, _ = product_scope()
    configure(monkeypatch, cluster, scope)
    quickstart.main()
    memory = product_service.memory(scope)
    sources = [record.id for record in memory.list_sources()]
    assert len(sources) == 5
    assert "第 10 天 -> 'Tencent'   （当前 -> 'Moonshot AI'）" in capsys.readouterr().out
    quickstart.main()
    assert [record.id for record in memory.list_sources()] == sources


def test_console_seed_creates_separate_spaces_and_maps_case_ids(
        tmp_path, cluster, product_service, product_scope, monkeypatch):
    scope, _ = product_scope()
    configure(monkeypatch, cluster, scope)
    source = tmp_path / 'data.json'
    source.write_text(json.dumps([
        {'haystack_sessions': [[{'role': 'user', 'content': text}]]}
        for text in ('I live in Beijing.', 'I live in Shenzhen.')
    ]), encoding='utf-8')
    output = tmp_path / 'spaces.jsonl'
    args = ['--data', str(source), '--out', str(output)]
    seed_console.main(args)
    rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
    assert [row['case'] for row in rows] == [1, 2]
    assert rows[0]['space_id'] != rows[1]['space_id']
    for row, city in zip(rows, ('Beijing', 'Shenzhen'), strict=True):
        context = product_service.spaces.authorize(scope.user_id, row['space_id'])
        memory = product_service.memory(context)
        assert memory.search('Where do I live?').top().object == city
    with pytest.raises(FileExistsError):
        seed_console.main(args)
