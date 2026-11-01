"""Service access must keep snapshots, cache ownership and file locks consistent."""
from __future__ import annotations

import json
import multiprocessing
import os
import queue
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mnemotrace import _flock

from mnemotrace import Memory
from mnemotrace.embed import HashingEmbedder
from tests.model_fixtures import MemoryService
from mnemotrace.store import PersistenceError, persist


def service(path, **kwargs):
    return MemoryService(data_dir=str(path), embedder_name="hashing", llm_name="", **kwargs)


def objects(mem):
    return {f.object for f in mem.fact_store.values() + mem.cold_store.values()}


class Call:
    def __init__(self, target, name="test-operation"):
        self.done = threading.Event()
        self.result = None
        self.error = None

        def run():
            try:
                self.result = target()
            except BaseException as exc:  # noqa: BLE001 -- transfer all worker failures to pytest
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, name=name, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def finish(self):
        self.thread.join(timeout=10)
        assert not self.thread.is_alive(), "operation did not finish"
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.parametrize("same_service", [True, False])
@pytest.mark.parametrize("reload", [False, True])
@pytest.mark.parametrize("operation", ["write", "forget"])
def test_loading_cannot_publish_after_write_or_forget(tmp_path, monkeypatch, same_service, reload, operation):
    seed = service(tmp_path)
    seed.add_fact("u", "project", "old_marker", "old")
    reader = service(tmp_path)
    if reload:
        reader.get("u")
        seed.add_fact("u", "project", "external_marker", "external")
    writer = reader if same_service else service(tmp_path)
    loaded, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    original = Memory.open

    def paused(*args, **kwargs):
        mem = original(*args, **kwargs)
        if threading.current_thread().name == "loader":
            loaded.set()
            assert resume.wait(10)
        return mem

    def change():
        attempted.set()
        if operation == "write":
            writer.add_fact("u", "project", "saved_marker", "saved")
        else:
            writer.forget("u")

    monkeypatch.setattr(Memory, "open", paused)
    read = Call(lambda: reader.memories("u"), "loader").start()
    change_call = None
    try:
        assert loaded.wait(5)
        change_call = Call(change).start()
        assert attempted.wait(5)
        assert not change_call.done.wait(0.15), "write/forget bypassed snapshot loading"
    finally:
        resume.set()
        read.finish()
        if change_call is not None:
            change_call.finish()
    # This second write used to persist the stale reader's cache over successful writes/forget.
    reader.add_fact("u", "project", "next_marker", "next")
    disk = original(reader._path("u"), embedder=reader.embedder)
    assert objects(disk) == ({"old", "saved", "next"} | ({"external"} if reload else set())
                             if operation == "write" else {"next"})
    assert objects(service(tmp_path).get("u")) == objects(disk)


@pytest.mark.parametrize("same_service", [True, False])
def test_all_collections_are_loaded_under_the_service_file_lock(tmp_path, monkeypatch, same_service):
    reader = service(tmp_path)
    mem = Memory.open(reader._path("u"), embedder=reader.embedder)
    mem.add("generation-1", user_id="u")
    mem.add_fact("project", "marker", "generation-1", user_id="u")
    mem.save()
    writer = reader if same_service else service(tmp_path)
    loaded, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    original = persist._read_jsonl

    def paused(path, name, limit=None):
        rows = original(path, name, limit)
        if name == "episodes" and threading.current_thread().name == "loader":
            loaded.set()
            assert resume.wait(10)
        return rows

    def change():
        attempted.set()
        with writer.write_lock("u"):
            current = writer.get("u")
            current.episodes_doc.values()[0].content = "generation-2"
            current.fact_store.values()[0].object = "generation-2"
            writer._save("u", current)

    monkeypatch.setattr(persist, "_read_jsonl", paused)
    read = Call(lambda: reader.export("u", include_sensitive=True), "loader").start()
    change_call = None
    try:
        assert loaded.wait(5)
        change_call = Call(change).start()
        assert attempted.wait(5)
        assert not change_call.done.wait(0.15), "save interleaved with collection loading"
    finally:
        resume.set()
        snapshot = read.finish()
        if change_call is not None:
            change_call.finish()
    assert snapshot["episodes"][0]["content"] == "generation-1"
    assert {f["object"] for f in snapshot["facts"]} == {"generation-1"}
    reopened = service(tmp_path).get("u")
    assert reopened.episodes_doc.values()[0].content == "generation-2"
    assert objects(reopened) == {"generation-2"}


def test_write_migration_does_not_reacquire_service_file(tmp_path, monkeypatch):
    svc = service(tmp_path)
    mem = Memory.open(svc._path("u"), embedder=HashingEmbedder(64))
    mem.add_fact("project", "marker", "old", user_id="u")
    mem.save()
    monkeypatch.setenv("MNEMOTRACE_REEMBED_ON_MISMATCH", "1")
    original = _flock.acquire
    acquisitions = []

    def acquire(fh, **kwargs):
        if Path(fh.name).name.startswith("@"):
            acquisitions.append(fh.name)
            assert len(acquisitions) == 1, "service file lock acquired twice before release"
        return original(fh, **kwargs)

    monkeypatch.setattr(_flock, "acquire", acquire)
    svc.add_fact("u", "project", "next_marker", "next")
    assert len(acquisitions) == 1
    assert objects(Memory.open(svc._path("u"), embedder=svc.embedder)) == {"old", "next"}


def test_snapshot_identity_survives_equal_size_and_timestamp(tmp_path, monkeypatch):
    first = service(tmp_path)
    first.add_fact("u", "project", "marker", "old")
    old = first.get("u")
    manifest_path = Path(first._path("u")) / "manifest.json"
    initial = manifest_path.stat()
    previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(persist, "now", lambda: previous["updated_at"])
    second = service(tmp_path)
    with second.write_lock("u"):
        mem = second.get("u")
        mem.fact_store.values()[0].object = "new"
        second._save("u", mem)
    os.utime(manifest_path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    assert manifest_path.stat().st_size == initial.st_size
    assert manifest_path.stat().st_mtime_ns == initial.st_mtime_ns
    refreshed = first.get("u")
    assert refreshed is not old and objects(refreshed) == {"new"}
    assert refreshed._snapshot_version != old._snapshot_version
    assert objects(Memory.open(first._path("u"), embedder=first.embedder)) == {"new"}


def test_legacy_manifest_without_commit_identity_is_rejected_without_upgrade(tmp_path):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "marker", "old")
    manifest_path = Path(svc._path("u")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("snapshot_id")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = manifest_path.read_bytes()
    reopened = service(tmp_path)
    with pytest.raises(persist.StoreFormatError, match="snapshot_id"):
        reopened.get("u")
    assert manifest_path.read_bytes() == before
    assert "u" not in reopened._hot


def test_warm_cache_cannot_bypass_invalid_manifest_state(tmp_path):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "marker", "old")
    svc.get("u")
    manifest_path = Path(svc._path("u")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["state"].pop("import_seen")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(persist.StoreFormatError, match="import_seen"):
        svc.get("u")


def test_live_manifest_reader_does_not_prevent_atomic_commit(tmp_path):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "old_marker", "old")
    manifest = Path(svc._path("u")) / "manifest.json"
    with persist._open_manifest(str(manifest)) as reader:
        previous = reader.read()
        svc.add_fact("u", "project", "next_marker", "next")
        reader.seek(0)
        assert reader.read() == previous
    assert manifest.read_bytes() != previous
    assert objects(service(tmp_path).get("u")) == {"old", "next"}


def test_save_requires_owned_current_object_and_rejects_stale_nested_write(tmp_path):
    first = service(tmp_path)
    first.add_fact("u", "project", "marker", "old")
    old = first.get("u")
    with pytest.raises(PersistenceError, match="write transaction"):
        first._save("u", old)
    with pytest.raises(PersistenceError, match="stale memory snapshot"), first.write_lock("u"):
        mem = first.get("u")
        # Same-thread, cross-instance nesting can commit before the outer object is saved.
        service(tmp_path).add_fact("u", "project", "other_marker", "other")
        first._save("u", mem)
    assert "u" not in first._hot
    assert objects(service(tmp_path).get("u")) == {"old", "other"}


def test_save_failure_drops_dirty_cache_and_does_not_advance_identity(tmp_path, monkeypatch):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "marker", "old")
    old = svc.get("u")
    version = old._snapshot_version
    original = persist._replace_manifest

    def fail_manifest(source, destination):
        if str(destination).endswith("manifest.json"):
            raise OSError("synthetic manifest commit failure")
        return original(source, destination)

    with monkeypatch.context() as scoped:
        scoped.setattr(persist, "_replace_manifest", fail_manifest)
        with pytest.raises(OSError, match="synthetic"):
            svc.add_fact("u", "project", "next_marker", "next")
    assert "u" not in svc._hot
    assert old._snapshot_version == version
    # A failed multi-file save is outside crash recovery guarantees; only cache/token cleanup
    # is asserted here, not rollback of collection replacements.
    assert persist.snapshot_version(svc._path("u")) == version


def test_nested_reads_pin_object_across_eviction_and_external_commit(tmp_path, monkeypatch):
    svc = service(tmp_path, max_hot_users=1)
    svc.add_fact("u", "project", "marker", "old")
    original = svc.stats
    called = []

    def evict_then_count(user):
        pinned = svc.get(user)
        svc.get("another-space")
        assert user not in svc._hot
        service(tmp_path).add_fact(user, "project", "other_marker", "other")
        assert svc.get(user) is pinned
        called.append(True)
        return original(user)

    monkeypatch.setattr(svc, "stats", evict_then_count)
    status = svc.agent_status("u")
    assert called == [True]
    assert status["counts"]["facts_live"] == 1
    assert objects(svc.get("u")) == {"old", "other"}


def test_nested_cross_instance_read_reuses_native_file_lock(tmp_path, monkeypatch):
    first = service(tmp_path)
    first.add_fact("u", "project", "marker", "old")
    second = service(tmp_path / ".")
    assert first.lock("u") is second.lock("u")
    original = _flock.acquire
    files = []

    def acquire(fh, **kwargs):
        files.append(Path(fh.name).name)
        return original(fh, **kwargs)

    monkeypatch.setattr(_flock, "acquire", acquire)
    with first.write_lock("u"):
        assert first.get("u")._snapshot_version == second.get("u")._snapshot_version
        assert second.agent_status("u")["counts"]["facts_live"] == 1
    assert len([name for name in files if name.startswith('@')]) == 1
    assert files.count('.lock') == 1  # One coherent cold snapshot load, reused by nested reads.


def test_cache_metadata_lock_never_contains_version_io(tmp_path, monkeypatch):
    svc = service(tmp_path)
    original = svc._store_version

    def checked(user):
        assert svc._g.acquire(blocking=False), "manifest I/O while holding cache metadata lock"
        svc._g.release()
        return original(user)

    monkeypatch.setattr(svc, "_store_version", checked)
    svc.get("u")
    svc.add_fact("u", "project", "marker", "old")
    svc.stats("u")


def test_response_provenance_is_detached_from_memory(tmp_path):
    svc = service(tmp_path)
    svc.remember("u", "I live in Syntheticville.", scope="long")
    result = svc.export("u", include_sensitive=True)
    assert result["facts"] and result["facts"][0]["provenance"]
    result["facts"][0]["provenance"].append("not-a-real-episode")
    svc.add_fact("u", "project", "marker", "next")
    assert all("not-a-real-episode" not in f.provenance for f in service(tmp_path).get("u").fact_store.values())


class NamedHashingEmbedder(HashingEmbedder):
    def __init__(self, dim, model_name):
        super().__init__(dim)
        self.model_name = model_name


def migration_embedder(mismatch, old=False):
    if mismatch == "dimension":
        return HashingEmbedder(64 if old else 128)
    return NamedHashingEmbedder(64, "synthetic-old" if old else "synthetic-new")


def _migration_process(path, mismatch, entry, enabled, result):
    # spawn must not consume the developer's .env or construct network providers.
    for key in list(os.environ):
        if key.startswith("MNEMOTRACE_"):
            os.environ.pop(key)
    os.environ["MNEMOTRACE_REEMBED_ON_MISMATCH"] = "1" if enabled else "0"
    os.environ["MNEMOTRACE_BUFFERED_EXTRACTION"] = "1" if entry == "async" else "0"
    try:
        with patch("mnemotrace.llm.providers.load_dotenv"):
            svc = service(path)
            svc.embedder = migration_embedder(mismatch)
            if entry == "read":
                svc.memories("u")
            elif entry == "remember":
                svc.remember("u", "I live in Syntheticville.", scope="long")
            elif entry == "add_fact":
                svc.add_fact("u", "project", "next_marker", "next")
            elif entry == "import":
                svc.import_("u", format="messages", data=[{"role": "user", "content": "SYNTHETIC_IMPORT"}],
                            consolidate=False, summarize=False)
            else:
                svc.remember("u", "I live in Syntheticville.", scope="long", processing="buffered")
                assert svc.flush("u")["ok"]
            svc.close()
            result.put({"ok": True})
    except BaseException as exc:  # noqa: BLE001 -- report child failures before the timeout
        result.put({"error": type(exc).__name__})


def finish_process(process, result, timeout=15):
    try:
        try:
            outcome = result.get(timeout=timeout)
        except queue.Empty:
            pytest.fail("native-lock process timed out")
        process.join(timeout=5)
        assert not process.is_alive() and process.exitcode == 0
        return outcome
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        result.close()
        result.join_thread()


@pytest.mark.parametrize("mismatch", ["dimension", "model"])
@pytest.mark.parametrize("entry", ["read", "remember", "add_fact", "import", "async"])
def test_native_migration_all_entrypoints_finish_and_reopen(tmp_path, mismatch, entry):
    svc = service(tmp_path)
    mem = Memory.open(svc._path("u"), embedder=migration_embedder(mismatch, old=True))
    mem.add("SYNTHETIC_ORIGINAL_EPISODE", user_id="u")
    mem.add_fact("project", "old_marker", "old", user_id="u")
    mem.summarize_episodes(mem.episodes_doc.values())
    mem.save()
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    child = context.Process(target=_migration_process, args=(str(tmp_path), mismatch, entry, True, result))
    child.start()
    assert finish_process(child, result) == {"ok": True}
    target = migration_embedder(mismatch)
    reopened = Memory.open(svc._path("u"), embedder=target)
    assert "old" in objects(reopened)
    assert any(ep.content == "SYNTHETIC_ORIGINAL_EPISODE" for ep in reopened.episodes_doc.values())
    assert all(len(f.embedding) == target.dim for f in reopened.fact_store.values())
    assert all(len(ep.embedding) == target.dim for ep in reopened.episodes_doc.values())
    assert all(len(ep.summary_embedding) == target.dim for ep in reopened.episodes_doc.values()
               if ep.summary_embedding is not None)
    if entry == "add_fact":
        assert "next" in objects(reopened)
    elif entry == "remember":
        assert any("Syntheticville" in ep.content for ep in reopened.episodes_doc.values())
    elif entry == "import":
        assert any("SYNTHETIC_IMPORT" in ep.content for ep in reopened.episodes_doc.values())


@pytest.mark.parametrize("mismatch,error", [("dimension", "DimensionMismatchError"), ("model", "EmbedderMismatchError")])
def test_disabled_migration_still_refuses_native_write(tmp_path, mismatch, error):
    svc = service(tmp_path)
    mem = Memory.open(svc._path("u"), embedder=migration_embedder(mismatch, old=True))
    mem.add_fact("project", "old_marker", "old", user_id="u")
    mem.save()
    context = multiprocessing.get_context("spawn")
    result = context.Queue()
    child = context.Process(target=_migration_process, args=(str(tmp_path), mismatch, "add_fact", False, result))
    child.start()
    assert finish_process(child, result) == {"error": error}
    assert objects(Memory.open(svc._path("u"), embedder=migration_embedder(mismatch, old=True))) == {"old"}


def test_recall_and_remember_serialize_native_vector_iteration(tmp_path, monkeypatch):
    from mnemotrace.store.memory_store import InMemoryVectorStore

    svc = service(tmp_path)
    svc.config.ann_candidates = True
    svc.add_fact("u", "u", "uses", "synthetic-tool")
    mem = svc.get("u")
    ready, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    original = InMemoryVectorStore.search

    def paused(store, vector, top_k, where=None):
        if store is mem.fact_store and threading.current_thread().name == "reader":
            def predicate(payload):
                if not ready.is_set():
                    ready.set()
                    assert resume.wait(10)
                return where is None or where(payload)
            return original(store, vector, top_k, predicate)
        return original(store, vector, top_k, where)

    def write():
        attempted.set()
        return svc.remember("u", "I live in Syntheticville.", scope="long")

    monkeypatch.setattr(InMemoryVectorStore, "search", paused)
    read = Call(lambda: svc.recall("u", "Where do I live?", lean=False), "reader").start()
    change = None
    try:
        assert ready.wait(5)
        # Retrieval holds the thread lock, but another process can still obtain the file lock.
        with open(svc._coordinator("u").path, "a") as fh:
            assert _flock.acquire(fh, blocking=False)
            _flock.release(fh)
        change = Call(write).start()
        assert attempted.wait(5)
        assert not change.done.wait(0.15)
    finally:
        resume.set()
        read.finish()
        if change is not None:
            change.finish()
    disk = service(tmp_path).get("u")
    assert "synthetic-tool" in objects(disk)
    assert any("Syntheticville" in ep.content for ep in disk.episodes_doc.values())


def test_planner_llm_holds_thread_lock_but_not_file_lock(tmp_path):
    seed = service(tmp_path)
    seed.add_fact("u", "node-a", "colleague", "node-b")
    seed.add_fact("u", "node-b", "works_at", "synthetic-lab")
    svc = service(tmp_path)
    called = []

    class ProbeLLM:
        def complete(self, prompt, system=""):
            with open(svc._coordinator("u").path, "a") as fh:
                assert _flock.acquire(fh, blocking=False)
                _flock.release(fh)

            def try_thread_lock():
                lock = svc.lock("u")
                acquired = lock.acquire(blocking=False)
                if acquired:
                    lock.release()
                return acquired

            assert Call(try_thread_lock).start().finish() is False
            called.append(True)
            return '{"anchor":"node-a","predicates":["colleague","works_at"]}'

    svc.llm = ProbeLLM()
    svc.recall("u", "Where does node-a's colleague work?", lean=False)
    assert called


def test_answerer_runs_after_thread_and_file_locks_are_released(tmp_path):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "old_marker", "old")
    called = []

    class Answerer:
        def complete(self, prompt, system=""):
            result = Call(lambda: svc.add_fact("u", "project", "next_marker", "next")).start().finish()
            assert result["ok"]
            called.append(True)
            return "synthetic answer"

    svc.answerer = Answerer()
    result = svc.recall("u", "project", answer=True)
    assert called == [True] and result["answer"] == "synthetic answer"
    assert objects(service(tmp_path).get("u")) == {"old", "next"}


def _held_writer(path, ready, resume, result):
    try:
        with patch("mnemotrace.llm.providers.load_dotenv"):
            svc = service(path)
            with svc.write_lock("u"):
                mem = svc.get("u")
                mem.add_fact("project", "next_marker", "next", user_id="u")
                ready.set()
                assert resume.wait(15)
                svc._save("u", mem)
            result.put({"ok": True})
    except BaseException as exc:  # noqa: BLE001 -- report child failures before the timeout
        result.put({"error": repr(exc)})


@pytest.mark.parametrize("warm", [True, False])
def test_other_process_write_allows_warm_read_but_blocks_cold_load(tmp_path, warm):
    seed = service(tmp_path)
    seed.add_fact("u", "project", "old_marker", "old")
    reader = service(tmp_path)
    if warm:
        reader.get("u")
    context = multiprocessing.get_context("spawn")
    ready, resume, result = context.Event(), context.Event(), context.Queue()
    writer = context.Process(target=_held_writer, args=(str(tmp_path), ready, resume, result))
    writer.start()
    read = None
    try:
        assert ready.wait(10)
        read = Call(lambda: reader.memories("u")).start()
        if warm:
            assert read.done.wait(3), "warm read waited for another process's uncommitted write"
            assert {f["object"] for f in read.finish()["facts"]} == {"old"}
        else:
            assert not read.done.wait(0.15), "cold load bypassed the active file transaction"
    finally:
        resume.set()
        if read is not None:
            read_result = read.finish()
        outcome = finish_process(writer, result)
    assert outcome == {"ok": True}
    if not warm:
        assert {f["object"] for f in read_result["facts"]} == {"old", "next"}
    reader.add_fact("u", "project", "last_marker", "last")
    assert objects(service(tmp_path).get("u")) == {"old", "next", "last"}


@pytest.mark.parametrize("mismatch", ["dimension", "model"])
def test_concurrent_migration_preserves_both_process_writes(tmp_path, mismatch):
    svc = service(tmp_path)
    mem = Memory.open(svc._path("u"), embedder=migration_embedder(mismatch, old=True))
    mem.add_fact("project", "old_marker", "old", user_id="u")
    mem.save()
    context = multiprocessing.get_context("spawn")
    children = []
    try:
        for entry in ("remember", "add_fact"):
            result = context.Queue()
            child = context.Process(target=_migration_process, args=(str(tmp_path), mismatch, entry, True, result))
            child.start()
            children.append((child, result))
        outcomes = [finish_process(child, result) for child, result in children]
    finally:
        for child, result in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
            result.close()
    assert outcomes == [{"ok": True}, {"ok": True}]
    disk = Memory.open(svc._path("u"), embedder=migration_embedder(mismatch))
    assert {"old", "next"} <= objects(disk)
    assert any("Syntheticville" in ep.content for ep in disk.episodes_doc.values())


@pytest.mark.parametrize("step", ["reembed", "save"])
def test_failed_migration_never_publishes_partial_cache(tmp_path, monkeypatch, step):
    svc = service(tmp_path)
    old = HashingEmbedder(64)
    mem = Memory.open(svc._path("u"), embedder=old)
    mem.add_fact("project", "old_marker", "old", user_id="u")
    mem.save()
    monkeypatch.setenv("MNEMOTRACE_REEMBED_ON_MISMATCH", "1")

    def fail(*args, **kwargs):
        raise OSError("synthetic migration failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(Memory, step, fail)
        with pytest.raises(OSError, match="synthetic"):
            svc.memories("u")
    assert "u" not in svc._hot and "u" not in svc._hot_versions
    assert objects(Memory.open(svc._path("u"), embedder=old)) == {"old"}
    assert not svc._frames()


def test_forgotten_object_cannot_be_saved_by_a_later_transaction(tmp_path):
    svc = service(tmp_path)
    svc.add_fact("u", "project", "old_marker", "old")
    stale = svc.get("u")
    svc.forget("u")
    with pytest.raises(PersistenceError, match="current memory"), svc.write_lock("u"):
        svc.get("u")
        svc._save("u", stale)
    assert not Path(svc._path("u")).exists()
    svc.add_fact("u", "project", "new_marker", "new")
    assert objects(service(tmp_path).get("u")) == {"new"}


@pytest.mark.parametrize("read_kind", ["cold", "working"])
def test_read_side_effects_finish_before_another_write(tmp_path, monkeypatch, read_kind):
    from mnemotrace.store.memory_store import InMemoryVectorStore

    svc = service(tmp_path)
    ready, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    if read_kind == "cold":
        svc.add_fact("u", "project", "cold_marker", "cold")
        svc.add_fact("u", "project", "hot_marker", "hot")
        with svc.write_lock("u"):
            mem = svc.get("u")
            mem.evict_cold(1)
            svc._save("u", mem)
        cold_id = mem.cold_store.values()[0].id
        original = InMemoryVectorStore.upsert

        def pause_upsert(store, key, vector, payload):
            if store is mem.fact_store and key == cold_id and threading.current_thread().name == "reader":
                assert mem.cold_store.get(cold_id) is None
                ready.set()
                assert resume.wait(10)
            return original(store, key, vector, payload)

        def cold_context(current, *args, **kwargs):
            current._page_hot([current.cold_store.get(cold_id)])
            return "synthetic cold context"

        monkeypatch.setattr(InMemoryVectorStore, "upsert", pause_upsert)
        monkeypatch.setattr(Memory, "lean_context", cold_context)
        def read_operation():
            return svc.recall("u", "cold")
    else:
        svc.add_working("u", "expired", ttl_seconds=-1)
        original = Memory.sweep_working

        def sweep(current, as_of=None):
            if threading.current_thread().name == "reader":
                ready.set()
                assert resume.wait(10)
            return original(current, as_of)

        monkeypatch.setattr(Memory, "sweep_working", sweep)
        def read_operation():
            return svc.working_memory("u")

    def write():
        attempted.set()
        return svc.add_fact("u", "project", "next_marker", "next")

    read = Call(read_operation, "reader").start()
    change = None
    try:
        assert ready.wait(5)
        change = Call(write).start()
        assert attempted.wait(5)
        assert not change.done.wait(0.15)
    finally:
        resume.set()
        read.finish()
        if change is not None:
            change.finish()
    disk = service(tmp_path).get("u")
    if read_kind == "cold":
        assert objects(disk) == {"cold", "hot", "next"}
    else:
        assert not disk.working_mem and objects(disk) == {"next"}
