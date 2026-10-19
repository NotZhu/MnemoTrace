"""Local import shares the service namespace boundary and read-modify-save lock."""
from __future__ import annotations

import json
import threading
from argparse import Namespace
from pathlib import Path

import pytest

from mnemotrace import Memory, _flock
from mnemotrace.connectors import parse
from mnemotrace.connectors.__main__ import _import_local, main
from mnemotrace.service import MemoryService


def args_for(root, user="synthetic-user", **overrides):
    values = dict(data_dir=str(root), namespace=user, embedder=None, llm=None,
                  no_consolidate=True, no_summarize=True)
    return Namespace(**{**values, **overrides})


def sessions():
    return parse([{"role": "user", "content": "SYNTHETIC_CLI_EPISODE"}], format="messages")


@pytest.mark.parametrize("old_layout", [None, "directory", "pkl-directory", "pkl-file"])
def test_cli_entrypoint_uses_service_digest_and_preserves_old_paths(
    tmp_path, monkeypatch, capsys, old_layout,
):
    data = tmp_path / "data"
    data.mkdir()
    old = data / ("synthetic-user" if old_layout == "directory" else "synthetic-user.pkl")
    before = {}
    if old_layout == "pkl-file":
        old.write_bytes(b"SYNTHETIC_LEGACY_FILE")
    elif old_layout:
        memory = Memory.open(str(old))
        memory.add_fact("project", "marker", "SYNTHETIC_LEGACY_ONLY")
        memory.save()
        before = {path.name: path.read_bytes() for path in old.iterdir() if path.is_file()}
    source = tmp_path / "input.json"
    source.write_text(json.dumps([{"role": "user", "content": "SYNTHETIC_CLI_EPISODE"}]))
    monkeypatch.setattr("sys.argv", ["mnemotrace-import", "--file", str(source),
                                    "--namespace", "synthetic-user", "--data-dir", str(data),
                                    "--no-consolidate", "--no-summarize"])
    main()
    result = json.loads(capsys.readouterr().out)
    service = MemoryService(data_dir=str(data), embedder_name="hashing", llm_name="")
    assert result["store"] == service._path("synthetic-user")
    assert result["episodes"] == 1
    memory = service.get("synthetic-user")
    assert [ep.content for ep in memory.episodes_doc.values()] == ["user: SYNTHETIC_CLI_EPISODE"]
    assert memory.fact_store.values() == []
    assert all(not ep.summary for ep in memory.episodes_doc.values())
    if old_layout == "pkl-file":
        assert old.read_bytes() == b"SYNTHETIC_LEGACY_FILE"
    elif old_layout:
        assert {path.name: path.read_bytes() for path in old.iterdir() if path.is_file()} == before
    else:
        assert not (data / "synthetic-user").exists()
        assert not (data / "synthetic-user.pkl").exists()


@pytest.mark.parametrize("user", [".", "..", "../outside", "a/b", "合成用户"])
def test_cli_special_namespace_stays_in_its_digest_directory(tmp_path, user):
    data = tmp_path / "data"
    result = _import_local(args_for(data, user), sessions())
    service = MemoryService(data_dir=str(data), embedder_name="hashing", llm_name="")
    assert result["store"] == service._path(user)
    assert Path(result["store"]).parent == data
    assert len(service.get(user).episodes_doc.values()) == 1


def test_cli_and_service_append_to_same_store_and_dedupe_after_restart(tmp_path):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    service.add_fact("synthetic-user", "project", "marker", "SYNTHETIC_SERVICE_FACT")
    first = _import_local(args_for(tmp_path), sessions())
    assert first["episodes"] == 1
    assert [ep.content for ep in service.get("synthetic-user").episodes_doc.values()] == ["user: SYNTHETIC_CLI_EPISODE"]
    assert {fact.object for fact in service.get("synthetic-user").fact_store.values()} == {"SYNTHETIC_SERVICE_FACT"}
    again = _import_local(args_for(tmp_path), sessions())
    assert again["episodes"] == 0 and again["skipped"] == 1


def test_cli_waits_for_service_transaction_and_preserves_its_write(tmp_path, monkeypatch):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    user = "synthetic-user"
    attempting, finished = threading.Event(), threading.Event()
    acquire = _flock.acquire
    errors = []

    def import_in_thread():
        try:
            _import_local(args_for(tmp_path, user), sessions())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=import_in_thread, daemon=True)

    def announce(fh, **kwargs):
        if threading.current_thread() is worker:
            attempting.set()
        return acquire(fh, **kwargs)

    monkeypatch.setattr(_flock, "acquire", announce)
    try:
        with service.write_lock(user):
            worker.start()
            assert attempting.wait(5)
            assert not finished.wait(0.2), "CLI bypassed the service transaction lock"
            memory = service.get(user)
            memory.add_fact("project", "marker", "SYNTHETIC_CONCURRENT_FACT", user_id=user)
            service._save(user, memory)
    finally:
        worker.join(timeout=10)
    assert not worker.is_alive() and not errors
    reloaded = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    assert {fact.object for fact in reloaded.get(user).fact_store.values()} == {"SYNTHETIC_CONCURRENT_FACT"}
    assert [ep.content for ep in reloaded.get(user).episodes_doc.values()] == ["user: SYNTHETIC_CLI_EPISODE"]


def test_cli_default_still_consolidates_and_summarizes_offline(tmp_path):
    parsed = parse([{"role": "user", "content": "I live in SyntheticCity."}], format="messages")
    result = _import_local(args_for(tmp_path, no_consolidate=False, no_summarize=False), parsed)
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    memory = service.get("synthetic-user")
    assert result["facts_added"] >= 1
    assert memory.episodes_doc.values()[0].summary
    assert any(fact.object == "SyntheticCity" for fact in memory.fact_store.values())
