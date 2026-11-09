"""Exercise deployment entry points with local database and model doubles."""
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent
from unittest.mock import Mock

import pytest

from eval import seed_console
from mnemotrace import quickstart
from mnemotrace.llm import providers


@pytest.fixture
def deployment(monkeypatch):
    monkeypatch.setenv('MNEMOTRACE_DATABASE_URL', 'synthetic-runtime')
    monkeypatch.setenv('MNEMOTRACE_USER_ID', 'synthetic-user')
    monkeypatch.setenv('MNEMOTRACE_SPACE_ID', 'synthetic-space')
    monkeypatch.setenv('MNEMOTRACE_LLM', 'synthetic-model')
    monkeypatch.setenv('MNEMOTRACE_EMBEDDER', 'lexical')
    monkeypatch.setattr(providers, 'make_llm', Mock())


def test_quickstart_passes_deployment_config_to_memory(deployment, monkeypatch):
    memory = Mock()
    monkeypatch.setattr(quickstart, 'Database', lambda _: nullcontext(object()))
    monkeypatch.setattr(quickstart, 'Spaces', Mock())
    monkeypatch.setattr(quickstart, 'Memory', memory)
    run_demo = Mock()
    monkeypatch.setattr(quickstart, 'run_demo', run_demo)

    quickstart.main()

    assert memory.call_args.kwargs['config'] is not None
    run_demo.assert_called_once_with(memory.return_value)


def test_seed_creates_new_output_directory_and_passes_deployment_config(deployment, monkeypatch, tmp_path):
    source = tmp_path / 'data.json'
    source.write_text('[{}]', encoding='utf-8')
    output = tmp_path / '.artifacts' / 'spaces.jsonl'
    spaces = Mock()
    spaces.create.return_value.space_id = 'synthetic-space'
    memory = Mock()
    monkeypatch.setattr(seed_console, 'Database', lambda _: nullcontext(object()))
    monkeypatch.setattr(seed_console, 'Spaces', lambda _: spaces)
    monkeypatch.setattr(seed_console, 'Memory', memory)
    ingest = Mock()
    monkeypatch.setattr(seed_console, 'ingest', ingest)
    args = ['--data', str(source), '--out', str(output)]

    seed_console.main(args)

    assert memory.call_args.kwargs['config'] is not None
    ingest.assert_called_once_with(memory.return_value, {})
    assert json.loads(output.read_text(encoding='utf-8')) == {'case': 1, 'space_id': 'synthetic-space'}
    with pytest.raises(FileExistsError):
        seed_console.main(args)
    spaces.create.assert_called_once()


def test_idle_worker_exits_on_sigterm():
    # Run in a subprocess so a shutdown regression cannot hang the test runner.
    script = dedent("""
        import signal
        from contextlib import contextmanager
        from unittest.mock import Mock, patch

        from mnemotrace import worker

        discovery = Mock()
        discovery.discover_processing.return_value = ()
        closed = []

        @contextmanager
        def database(config, *, purpose='runtime'):
            try:
                yield discovery
            finally:
                closed.append(purpose)

        def idle_wait(seconds):
            assert seconds == 1
            signal.raise_signal(signal.SIGTERM)

        previous = signal.getsignal(signal.SIGTERM)
        with (patch.object(worker, 'load_dotenv'),
              patch.object(worker.DatabaseConfig, 'from_env'),
              patch.object(worker, 'Database', database),
              patch.object(worker, 'sleep', idle_wait, create=True)):
            assert worker.main([]) == 0
        discovery.discover_processing.assert_called_once()
        assert closed == ['discovery', 'runtime']
        assert signal.getsignal(signal.SIGTERM) == previous
    """)
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
