"""Offline storage-refactor reference, using the existing synthetic eval harness.

Run from the repository: python -m eval.storage_baseline --out <new directory>.
No dotenv loading, provider discovery, database or external model calls occur.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import random
import subprocess
from unittest.mock import patch

from eval import harness
from mnemotrace import Config, MemoryEngine
from mnemotrace.retrieve.lexical import bm25_scores
from mnemotrace.retrieve.lexical import lexical_terms

ROOT = Path(__file__).resolve().parents[1]
CLOCK = harness.BASE + 120 * 86400
WORKLOADS = Path(__file__).with_name('fixtures') / 'storage_workloads.json'
SOURCE_FILES = (
    'mnemotrace/store/contracts.py', 'mnemotrace/retrieve/structured.py', 'eval/storage_baseline.py',
    'eval/fixtures/storage_baseline.json', 'eval/fixtures/storage_workloads.json',
    'tests/test_storage_contracts.py', 'tests/test_storage_baseline.py',
    'docs/architecture/storage-contracts.md', 'docs/architecture/README.md', 'eval/README.md',
    'eval/storage_postgres.py', 'tests/postgres/conftest.py', 'tests/postgres/test_retrieval.py',
    'tests/postgres/test_migrations.py', 'mnemotrace/retrieve/postgres.py',
    'mnemotrace/store/postgres/graph_reads.py', 'mnemotrace/store/postgres/lexical.py',
    'mnemotrace/store/postgres/retrieval.py', 'mnemotrace/store/postgres/sql/005_retrieval.sql',
    'mnemotrace/store/postgres/sql/006_backup_constraints.sql',
)
EVALUATION_PATHS = ('mnemotrace', 'eval', 'tests/model_fixtures.py',
                    'tests/extraction_responses.json', *SOURCE_FILES)


def lexical_observations() -> dict:
    docs = [('old', 'Ana works at Acme'), ('current', 'Ana works at Orbit'),
            ('repeat', 'Orbit Orbit office'), ('empty', '')]
    return {
        'tokens': lexical_terms('Wei works_at Orbit; worked working 中文 2023-11'),
        'current': bm25_scores('works Orbit', docs[1:]),
        'history': bm25_scores('works Orbit', docs),
        'empty_query': bm25_scores('', docs),
    }


def observe() -> tuple[list[dict], dict, dict]:
    # Tests own these recorded responses; this repository-only eval is not a product backend.
    from tests.model_fixtures import RecordedLLM

    observations, captured = {}, {}

    def memory_for(storage, llm):
        if storage != 'memory':
            raise ValueError('This baseline only observes the offline memory reference')
        return MemoryEngine(llm=llm)

    def capture(user_id, memory, result):
        captured[user_id] = memory, result

    def run(item, **kwargs):
        return harness.run_mnemotrace_detail(item, capture=capture, **kwargs)

    with patch('time.time', return_value=CLOCK), patch.object(harness, '_memory_for', memory_for):
        rows, summary = harness.evaluate(storage='memory', llm=RecordedLLM(), run_item=run)
    # Materialize observations after the harness stops its retrieval timer.
    for user_id, (memory, result) in captured.items():
        sources = {ep.id: f'source-{i}' for i, ep in enumerate(memory.episodes_doc.values(), 1)}
        facts = sorted(memory._all_facts(), key=lambda f: (
            f.subject, f.predicate, f.object, f.valid_at or 0))
        identities = {fact.id: f'fact-{i}' for i, fact in enumerate(facts, 1)}
        observations[user_id] = {
                'facts': [dict(id=identities[f.id], subject=f.subject, predicate=f.predicate,
                               object=f.object, source=f.source,
                               source_roles=f.source_roles, valid_at=f.valid_at, invalid_at=f.invalid_at,
                               created_at=f.created_at, expired_at=f.expired_at,
                               supersedes=identities.get(f.supersedes),
                               provenance=[sources[s] for s in f.provenance]) for f in facts],
                'ranked': [identities[f.id] for f in result.facts],
                'scores': result.scores, 'via': result.via,
        }
        memory.close()
    for row in rows:
        row['fact_ids'] = observations[row['id']]['ranked']
    stable = {
        'format_version': 1,
        'clock': CLOCK,
        'config': asdict(Config()),
        'rows': [{k: v for k, v in row.items() if k != 'latency_ms'} for row in rows],
        'observations': observations,
        'lexical': lexical_observations(),
    }
    return rows, summary, stable


def workload_manifest() -> dict:
    return json.loads(WORKLOADS.read_text(encoding='utf-8'))


def workload_records(name: str):
    """Stream stable synthetic inputs for the Postgres/pgvector comparison.

    Filter bucket zero has exactly 1%, buckets 0..9 exactly 10% per complete
    block of 100 rows. Historical rows are every fourth row. No model is needed.
    """
    manifest = workload_manifest()
    spec = next(case for case in manifest['cases'] if case['name'] == name)
    rng = random.Random(manifest['seed'])
    for space in range(spec['spaces']):
        count = spec['large_space_facts'] if space == 0 else spec['facts_per_space']
        for index in range(count):
            yield {
                'user_id': f'user-{space % 10}', 'space_id': f'space-{space}',
                'id': f'fact-{space}-{index}', 'subject': f'Person-{index % 100}',
                'predicate': 'works_at', 'object': f'Company-{index % 100}',
                'text': f'Person-{index % 100} works at Company-{index % 100}',
                'valid_at': harness.BASE, 'invalid_at': harness.BASE + 86400 if index % 4 == 0 else None,
                'filter_bucket': index % 100,
                'vector': [rng.uniform(-1, 1) for _ in range(manifest['dimension'])],
            }


def _git(*args: str) -> str:
    result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', check=True)
    return result.stdout.strip()


def capture_sources() -> tuple[bytes, dict[str, str]]:
    """Include dirty runtime inputs without archiving unrelated workspace files.

    Known storage work files can be recorded before staging. Future new evaluation
    sources must be staged or committed so Git can include their complete diff.
    """
    result = subprocess.run(['git', 'ls-files', '-z', '--others', '--exclude-standard',
                             '--', *EVALUATION_PATHS], cwd=ROOT, capture_output=True, check=True)
    changed = [name.decode('utf-8') for name in result.stdout.split(b'\0') if name]
    unknown = set(changed) - set(SOURCE_FILES)
    if unknown:
        raise ValueError('Stage or commit new evaluation sources before recording: ' + ', '.join(sorted(unknown)))
    diff = subprocess.run(['git', 'diff', '--binary', 'HEAD', '--', *EVALUATION_PATHS],
                          cwd=ROOT, capture_output=True, check=True).stdout
    return diff, {path: (ROOT / path).read_text(encoding='utf-8') for path in changed}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path, help='New output directory; never overwrite a run')
    args = parser.parse_args(argv)
    diff, sources = capture_sources()
    args.out.mkdir(parents=True, exist_ok=False)
    rows, summary, stable = observe()
    harness.write_jsonl(str(args.out / 'synthetic.jsonl'), rows, summary)
    (args.out / 'observation.json').write_text(json.dumps(stable, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.out / 'tracked.diff').write_bytes(diff)
    (args.out / 'untracked.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    metadata = {
        'command': ['python', '-m', 'eval.storage_baseline', '--out', str(args.out)],
        'commit': _git('rev-parse', 'HEAD'), 'status': _git('status', '--short'),
        'python': platform.python_version(), 'platform': platform.platform(),
        'clock': CLOCK, 'config': stable['config'], 'workloads': workload_manifest(),
        'fixture_sha256': hashlib.sha256((ROOT / 'tests/extraction_responses.json').read_bytes()).hexdigest(),
        'scope': 'offline synthetic reference; lexical retrieval; no answerer or judge; not database performance',
    }
    (args.out / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
