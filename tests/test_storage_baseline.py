import itertools
import json
from pathlib import Path
import subprocess

import pytest

from eval.storage_baseline import lexical_observations, main, observe, workload_manifest, workload_records
from eval import storage_baseline


def test_reference_is_deterministic_and_preserves_fact_history_and_rankings():
    rows, summary, actual = observe()
    assert len(rows) == 10 and summary['accuracy'] == 100
    assert summary['latency_p95_ms'] >= summary['latency_p50_ms'] >= 0
    expected = json.loads((Path(__file__).parents[1] / 'eval/fixtures/storage_baseline.json').read_text(encoding='utf-8'))
    assert actual == expected
    assert observe()[2] == actual


def test_bm25_statistics_change_with_history_and_empty_documents_count_in_corpus():
    result = lexical_observations()
    assert result['current']['current'] == pytest.approx(1.0979275865732145)
    assert result['history']['current'] != result['current']['current']
    assert result['history']['empty'] == 0
    assert set(result['empty_query'].values()) == {0}


def test_workload_stream_is_reproducible_scoped_and_has_declared_filters():
    first = list(itertools.islice(workload_records('many-small'), 100))
    assert first == list(itertools.islice(workload_records('many-small'), 100))
    assert len({row['id'] for row in first}) == 100
    assert sum(row['filter_bucket'] < 1 for row in first) == 1
    assert sum(row['filter_bucket'] < 10 for row in first) == 10
    assert sum(row['invalid_at'] is not None for row in first) == 25
    assert all(row['space_id'] == 'space-0' and len(row['vector']) == 64 for row in first)
    manifest = workload_manifest()
    assert manifest['database_status'] == 'not_run'
    assert {case['name'] for case in manifest['cases']} == {'many-small', 'one-large', 'skewed'}


def test_run_records_reproduction_inputs_and_never_overwrites_an_existing_run(tmp_path):
    out = tmp_path / 'reference'
    main(['--out', str(out)])
    metadata = json.loads((out / 'metadata.json').read_text(encoding='utf-8'))
    assert len(metadata['commit']) == 40 and len(metadata['fixture_sha256']) == 64
    assert metadata['workloads']['database_status'] == 'not_run'
    assert metadata['command'] == ['python', '-m', 'eval.storage_baseline', '--out', str(out)]
    assert (out / 'tracked.diff').is_file() and (out / 'untracked.json').is_file()
    records = (out / 'synthetic.jsonl').read_text(encoding='utf-8').splitlines()
    assert len(records) == 11
    with pytest.raises(FileExistsError):
        main(['--out', str(out)])
    assert (out / 'synthetic.jsonl').read_text(encoding='utf-8').splitlines() == records


@pytest.fixture
def source_repository(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, check=True)
    git('init')
    path = tmp_path / 'mnemotrace/memory.py'
    path.parent.mkdir()
    path.write_text('version = 1\n', encoding='utf-8')
    git('add', 'mnemotrace/memory.py')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'fixture')
    monkeypatch.setattr(storage_baseline, 'ROOT', tmp_path)
    return tmp_path, git


def test_source_capture_includes_dirty_runtime_but_excludes_unrelated_files(source_repository):
    root, _ = source_repository
    (root / 'mnemotrace/memory.py').write_text('version = 2\n', encoding='utf-8')
    (root / 'private-notes.txt').write_text('unrelated synthetic note', encoding='utf-8')
    diff, untracked = storage_baseline.capture_sources()
    assert b'+version = 2' in diff and b'-version = 1' in diff
    assert b'unrelated synthetic note' not in diff and not untracked


def test_unknown_new_runtime_source_must_be_in_diff_before_recording(source_repository):
    root, git = source_repository
    path = root / 'mnemotrace/new_backend.py'
    path.write_text('version = 1\n', encoding='utf-8')
    with pytest.raises(ValueError, match='new_backend.py'):
        main(['--out', str(root / 'run')])
    assert not (root / 'run').exists()
    git('add', 'mnemotrace/new_backend.py')
    diff, untracked = storage_baseline.capture_sources()
    assert b'new_backend.py' in diff and b'+version = 1' in diff and not untracked
