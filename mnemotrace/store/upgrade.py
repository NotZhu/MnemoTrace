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
        if manifest.get('format_version') is not None:
            raise StoreFormatError('Expected an unversioned previous snapshot')
        prepared = {}
        kinds = ('Episode', 'Fact', 'Entity', 'Relation', 'WorkingMemory', 'Conflict')
        for name, kind in zip(COLLECTIONS, kinds):
            rows = _read_jsonl(str(source_path), name, manifest['counts'][name])
            for row in rows:
                if name == 'episodes':
                    # Renaming an existing field is evidence preserving; assigning individual
                    # message identities or splitting merged transcripts would be speculation.
                    if 'role' in row or 'speaker' not in row:
                        raise StoreFormatError('Invalid previous episode role contract')
                    row['role'] = row.pop('speaker')
                    row.update(message_id=None, sequence=None, mode=None, coverage=None)
                if name == 'facts':
                    row.update(assertion_kind='unspecified', source_roles=[])
                validate_record(kind, row, f'{name}.jsonl', jsonl_fact=name == 'facts',
                                embedding_dim=manifest['embedding_dim'])
            prepared[name] = rows
        manifest.update(format_version=2, snapshot_id=uuid.uuid4().hex)
        manifest = validate_manifest(manifest)
        from ..types import Episode
        from ..consolidate.tasks import validate_task_sources
        from ..consolidate.summaries import validate_state as validate_summaries
        episodes = [Episode(**row) for row in prepared['episodes']]
        validate_task_sources(manifest['state'].get('processing'), episodes)
        validate_summaries(manifest['state'].get('summarization'), episodes)
        destination_path.mkdir(parents=True, exist_ok=False)
        for name, rows in prepared.items():
            _write_jsonl(str(destination_path), name, rows)
        (destination_path / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return dict(format_version=2, destination=str(destination_path), counts=manifest['counts'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    args = parser.parse_args()
    print(json.dumps(upgrade_snapshot(args.source, args.destination), ensure_ascii=False))


if __name__ == '__main__':
    main()
