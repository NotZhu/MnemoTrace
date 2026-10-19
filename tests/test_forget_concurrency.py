from __future__ import annotations

import multiprocessing
import os
import queue
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from mnemotrace import Memory, _flock
from mnemotrace.service import MemoryService


def _waiting_writer(data_dir, user, attempting, result):
    acquire = _flock.acquire

    def announce(fh, **kwargs):
        attempting.set()
        return acquire(fh, **kwargs)

    _flock.acquire = announce
    try:
        # spawn 不继承父进程的 monkeypatch；子进程也必须隔离开发者的 .env。
        with patch("mnemotrace.llm.providers.load_dotenv"):
            service = MemoryService(data_dir=data_dir, embedder_name="hashing", llm_name="")
            service.add_fact(user, "project", "marker", "after-forget")
        result.put({"ok": True})
    except BaseException as exc:
        result.put({"error": repr(exc)})


@pytest.mark.parametrize("layout", ["secure", "legacy", "legacy.pkl"])
@pytest.mark.parametrize("same_service", [True, False])
def test_forget_blocks_writes_until_directory_removal(tmp_path, monkeypatch, layout, same_service):
    user = "synthetic-user"
    if layout != "secure":
        memory = Memory.open(str(tmp_path / (user + (".pkl" if layout.endswith(".pkl") else ""))))
        memory.add_fact("project", "marker", "before-forget", user_id=user)
        memory.save()
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    service.add_fact(user, "project", "marker", "before-forget")
    writer_service = service if same_service else MemoryService(
        data_dir=str(tmp_path), embedder_name="hashing", llm_name=""
    )
    store = Path(service._path(user))
    assert store == Path(service._secure_path(user))
    attempting, finished = threading.Event(), threading.Event()
    errors = []
    acquire, rmdir = _flock.acquire, os.rmdir

    def write():
        if same_service:
            attempting.set()
        try:
            writer_service.add_fact(user, "project", "marker", "after-forget")
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = threading.Thread(target=write)

    def announce(fh, **kwargs):
        if threading.current_thread() is writer:
            attempting.set()
        return acquire(fh, **kwargs)

    def remove_directory(path, *args, **kwargs):
        if Path(path) == store:
            writer.start()
            assert attempting.wait(5), "writer did not start"
            assert not finished.wait(0.2), "write completed before forget removed the directory"
        return rmdir(path, *args, **kwargs)

    monkeypatch.setattr(_flock, "acquire", announce)
    monkeypatch.setattr(os, "rmdir", remove_directory)
    try:
        assert service.forget(user)["ok"] is True
    finally:
        if writer.ident is not None:
            writer.join(timeout=5)
    assert not writer.is_alive()
    assert not errors
    reloaded = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    assert [fact["object"] for fact in reloaded.memories(user)["facts"]] == ["after-forget"]
    if layout != "secure":
        old = tmp_path / (user + (".pkl" if layout.endswith(".pkl") else ""))
        assert [fact.object for fact in Memory.open(str(old)).fact_store.values()] == ["before-forget"]


def test_forget_retains_lock_across_store_recreation(tmp_path, monkeypatch):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    user = "synthetic-user"
    acquired = []
    acquire = _flock.acquire

    def capture(fh, **kwargs):
        acquired.append(Path(fh.name))
        return acquire(fh, **kwargs)

    monkeypatch.setattr(_flock, "acquire", capture)
    with service.write_lock(user):
        lock = acquired[-1]
        identity = lock.stat().st_ino
    service.add_fact(user, "project", "marker", "before-forget")
    assert service.forget(user)["ok"] is True
    assert not Path(service._path(user)).exists()
    assert lock.exists() and lock.stat().st_ino == identity
    # 锁控制文件不能被当作另一个租户的旧数据路径删除。
    assert service.forget(lock.name)["ok"] is True
    assert lock.exists() and lock.stat().st_ino == identity
    with service.write_lock(user):
        assert acquired[-1] == lock
    service.add_fact(user, "project", "marker", "after-forget")
    assert lock.stat().st_ino == identity


def test_forget_blocks_waiting_process_until_directory_removal(tmp_path, monkeypatch):
    user = "synthetic-user"
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    service.add_fact(user, "project", "marker", "before-forget")
    store = Path(service._path(user))
    context = multiprocessing.get_context("spawn")
    attempting, result = context.Event(), context.Queue()
    writer = context.Process(target=_waiting_writer, args=(str(tmp_path), user, attempting, result))
    rmdir = os.rmdir

    def remove_directory(path, *args, **kwargs):
        if Path(path) == store:
            writer.start()
            assert attempting.wait(10), "child did not reach the service lock"
            try:
                early = result.get(timeout=0.2)
            except queue.Empty:
                early = None
            assert early is None, f"child write finished during forget: {early}"
        return rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", remove_directory)
    try:
        assert service.forget(user)["ok"] is True
        assert result.get(timeout=10) == {"ok": True}
        writer.join(timeout=5)
        assert writer.exitcode == 0
    finally:
        if writer.pid is not None:
            if writer.is_alive():
                writer.terminate()
            writer.join(timeout=5)
        result.close()
        result.join_thread()
    reloaded = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    assert [fact["object"] for fact in reloaded.memories(user)["facts"]] == ["after-forget"]
