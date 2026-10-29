from __future__ import annotations

import json

import pytest

from tests.model_fixtures import Memory
from mnemotrace.store import StoreFormatError, load_memory


def test_torn_trailing_jsonl_record_beyond_manifest_is_ignored(tmp_path):
    mem = Memory()
    mem.add("I live in Shenzhen.", user_id="u")
    mem.add("I work at Moonshot AI.", user_id="u")
    path = tmp_path / "store"
    mem.save(str(path))

    with (path / "episodes.jsonl").open("ab") as fh:
        fh.write(b'{"id":"half"')

    loaded = Memory.open(str(path))
    assert len(loaded.episodes_doc.values()) == 2
    assert {e.content for e in loaded.episodes_doc.values()} == {
        "I live in Shenzhen.",
        "I work at Moonshot AI.",
    }


def test_missing_committed_jsonl_record_fails_loudly(tmp_path):
    mem = Memory()
    mem.add("I live in Shenzhen.", user_id="u")
    mem.add("I work at Moonshot AI.", user_id="u")
    path = tmp_path / "store"
    mem.save(str(path))

    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["episodes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StoreFormatError):
        Memory.open(str(path))


def test_failed_load_does_not_clear_existing_memory(tmp_path):
    existing = Memory()
    existing.add("I live in Hangzhou.", user_id="u")
    before = [e.content for e in existing.episodes_doc.values()]

    source = Memory()
    source.add("I work at Moonshot AI.", user_id="u")
    bad_path = tmp_path / "bad-store"
    source.save(str(bad_path))

    manifest_path = bad_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["episodes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(StoreFormatError):
        load_memory(existing, str(bad_path))

    assert [e.content for e in existing.episodes_doc.values()] == before


def test_invalid_utf8_beyond_committed_prefix_is_not_decoded(tmp_path):
    mem = Memory()
    mem.add("Synthetic source", user_id="u")
    mem.save(str(tmp_path))
    with (tmp_path / "episodes.jsonl").open("ab") as fh:
        fh.write(b'\xff\xfe{"uncommitted":')
    restored = Memory.open(str(tmp_path))
    assert [ep.content for ep in restored.episodes_doc.values()] == ["Synthetic source"]


def test_invalid_utf8_inside_committed_prefix_is_format_error(tmp_path):
    mem = Memory()
    mem.add("Synthetic source", user_id="u")
    mem.save(str(tmp_path))
    (tmp_path / "episodes.jsonl").write_bytes(b'\xff\xfe\n')
    with pytest.raises(StoreFormatError, match="episodes.jsonl.*line 1"):
        Memory.open(str(tmp_path))
