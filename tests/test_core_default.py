from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from tests.model_fixtures import MemoryEngine
import pytest


def test_algorithm_memory_does_not_import_postgres_driver():
    before = "psycopg" in sys.modules
    mem = MemoryEngine()
    mem.add("I like Python.", user_id="u")
    assert ("psycopg" in sys.modules) == before


def test_offline_core_script_runs_without_services(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {name: value for name, value in os.environ.items() if not name.startswith("MNEMOTRACE_")}
    env.update({
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "bytecode"),
        "PYTHONUTF8": "1",
    })
    result = subprocess.run(
        [sys.executable, "scripts/check_core.py"],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK offline core verification passed" in result.stdout


def test_quickstart_missing_optional_dependency_has_actionable_message(monkeypatch):
    from mnemotrace import quickstart
    from mnemotrace.llm import providers

    def missing_dependency(*args):
        raise ImportError('Synthetic missing model library')

    monkeypatch.setattr(providers, 'make_llm', missing_dependency)
    monkeypatch.setattr(providers, 'load_dotenv', lambda: None)
    monkeypatch.setenv('MNEMOTRACE_LLM', 'synthetic-fixture')
    monkeypatch.setenv('MNEMOTRACE_USER_ID', 'user')
    monkeypatch.setenv('MNEMOTRACE_SPACE_ID', 'space')
    with pytest.raises(SystemExit, match=r'\[llm\]'):
        quickstart.main()


def test_core_missing_model_check_is_isolated_from_dotenv(tmp_path, monkeypatch):
    import runpy
    from unittest.mock import patch
    from mnemotrace.llm.providers import load_dotenv

    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / 'scripts' / 'check_core.py'))
    dotenv = tmp_path / '.env'
    dotenv.write_text('MNEMOTRACE_LLM=synthetic-fixture\n', encoding='utf-8')
    quickstart_calls = []

    def simulate_command(parts, **kwargs):
        if parts[-1] == 'mnemotrace.quickstart':
            with patch.dict(os.environ, kwargs['env'], clear=True):
                load_dotenv(str(dotenv))
                # Exercise the actual dotenv merge without constructing any model client.
                assert os.environ.get('MNEMOTRACE_LLM') == ''
            quickstart_calls.append(parts)
            return subprocess.CompletedProcess(parts, 1, stdout='', stderr='Configure MNEMOTRACE_LLM')
        return subprocess.CompletedProcess(parts, 0)

    monkeypatch.setattr(subprocess, 'run', simulate_command)
    assert namespace['main']() == 0
    assert len(quickstart_calls) == 1
