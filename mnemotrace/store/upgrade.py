"""Explicit snapshot-copy upgrade: python -m mnemotrace.store.upgrade SOURCE DESTINATION."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import uuid

from ._validation import COLLECTIONS, validate_manifest, validate_record
from .persist import StoreFormatError, _lock, _open_manifest, _read_jsonl, _write_jsonl


def upgrade_snapshot(source: str, destination: str) -> dict:
    """Copy the previous snapshot contract, preserving legacy identities without guessing messages.

    The destination must be new. Existing stores are never upgraded on open or in place.
    Native vector indexes are rebuilt from the copied authoritative snapshot on first open.
    """
    source_path, destination_path = Path(source).resolve(), Path(destination).resolve()
    if (source_path == destination_path or source_path in destination_path.parents
            or destination_path in source_path.parents or destination_path.exists()):
        raise ValueError('Upgrade destination must be a new directory outside the source')
    with _lock(str(source_path)):
        with _open_manifest(str(source_path / 'manifest.json')) as handle:
            manifest = json.loads(handle.read().decode('utf-8'))
        if manifest.get('format_version') not in (None, 2):
            raise StoreFormatError('Expected a previous pure-data snapshot')
        if {'processing', 'summarization', 'messages', 'pipeline'}.intersection(manifest.get('state', {})):
            raise StoreFormatError('Snapshots containing task state cannot be upgraded')
        prepared = {}
        kinds = ('Episode', 'Fact', 'Entity', 'Relation', 'WorkingMemory', 'Conflict')
        for name, kind in zip(COLLECTIONS, kinds):
            rows = _read_jsonl(str(source_path), name, manifest['counts'][name])
            for row in rows:
                if name == 'episodes':
                    forbidden = {'extraction_progress', 'extraction_counts', 'extraction_error',
                                 'outcome_error', 'memory_result', 'memory_scope', 'extraction'}
                    if forbidden.intersection(row.get('metadata', {})) or row.get('message_id') is not None or row.get('coverage') is not None:
                        raise StoreFormatError('Snapshots containing processing progress cannot be upgraded')
                    if 'speaker' in row:
                        if 'role' in row:
                            raise StoreFormatError('Conflicting source roles')
                        row['role'] = row.pop('speaker')
                    if 'role' not in row:
                        raise StoreFormatError('Missing original source role')
                    row.setdefault('source_kind', 'raw')
                    row.setdefault('scope', 'long')
                    row.update(message_id=None, sequence=None, mode=None, coverage=None, summary_coverage=None)
                if name == 'facts':
                    row.setdefault('assertion_kind', 'unspecified')
                    row.setdefault('source_roles', [])
                validate_record(kind, row, f'{name}.jsonl', jsonl_fact=name == 'facts',
                                embedding_dim=manifest['embedding_dim'])
            prepared[name] = rows
        manifest.update(format_version=3, snapshot_id=uuid.uuid4().hex)
        manifest = validate_manifest(manifest)
        destination_path.mkdir(parents=True, exist_ok=False)
        for name, rows in prepared.items():
            _write_jsonl(str(destination_path), name, rows)
        (destination_path / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return dict(format_version=3, destination=str(destination_path), counts=manifest['counts'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(upgrade_snapshot(args.source, args.destination), ensure_ascii=False))


if __name__ == '__main__':
    main()
