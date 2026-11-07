"""Wire size and credential checks for the historical replay reader."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'eval'))
    from mnemotrace.llm import providers
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setenv('MNEMOTRACE_BUFFERED_EXTRACTION', '1')
    monkeypatch.setenv('MNEMOTRACE_CONFLICT_DETECTION', '0')
    return importlib.import_module('buffered_replay_runner')


def test_wire_byte_parts_are_exact_with_unicode_escaping(runner):
    window = dict(pending=[dict(id='a', content='中文\n"quote"')], context=[dict(id='b', content='前文\\')])
    prompt = 'prefix'+json.dumps(window, ensure_ascii=False)+'suffix'
    parts = runner.byte_parts(prompt, '系统', window)
    assert parts['total'] == sum(v for k, v in parts.items() if k != 'total')
    assert parts['metadata'] > 0 and parts['context'] > 0 and parts['template'] == len('prefixsuffix')


def test_credential_failure_occurs_before_store_or_budget(runner, monkeypatch, tmp_path):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    args = SimpleNamespace(out=tmp_path, extractor='deepseek-chat')
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        runner.run_case({}, args)
    assert list(tmp_path.iterdir()) == []


def test_current_runtime_is_rejected_before_replay(runner, tmp_path):
    args = SimpleNamespace(out=tmp_path, extractor=None)
    with pytest.raises(ValueError, match='Select the historical runtime'):
        runner.run_case({}, args)
    assert list(tmp_path.iterdir()) == []
