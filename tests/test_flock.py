"""Lock contention must not hide broken descriptors or filesystem errors."""
import errno
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mnemotrace import _flock


@pytest.fixture
def lock_backend(monkeypatch):
    def install(platform, outcomes):
        operation = Mock(side_effect=outcomes)
        if platform == "windows":
            monkeypatch.setitem(sys.modules, "fcntl", None)
            monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
                locking=operation, LK_NBLCK=2,
            ))
        else:
            monkeypatch.setitem(sys.modules, "fcntl", SimpleNamespace(
                flock=operation, LOCK_EX=2, LOCK_NB=4,
            ))
        seek = Mock(return_value=0)
        sleep = Mock(side_effect=AssertionError("unexpected lock retry"))
        monkeypatch.setattr(_flock.os, "lseek", seek)
        monkeypatch.setattr("time.sleep", sleep)
        return SimpleNamespace(
            operation=operation, seek=seek, sleep=sleep,
            handle=SimpleNamespace(fileno=lambda: 42),
        )
    return install


@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("error", [errno.EBADF, errno.EACCES])
def test_windows_seek_failure_is_not_contention(lock_backend, blocking, error):
    backend = lock_backend("windows", [None])
    failure = OSError(error, "seek failed")
    backend.seek.side_effect = failure
    with pytest.raises(OSError) as caught:
        _flock.acquire(backend.handle, blocking=blocking)
    assert caught.value is failure
    backend.operation.assert_not_called()
    backend.sleep.assert_not_called()


@pytest.mark.parametrize("platform", ["windows", "posix"])
@pytest.mark.parametrize("blocking", [True, False])
@pytest.mark.parametrize("error", [errno.EBADF, errno.EINVAL, errno.EIO])
def test_lock_failure_propagates(lock_backend, platform, blocking, error):
    failure = OSError(error, "lock failed")
    backend = lock_backend(platform, [failure])
    with pytest.raises(OSError) as caught:
        _flock.acquire(backend.handle, blocking=blocking)
    assert caught.value is failure
    backend.sleep.assert_not_called()


@pytest.mark.parametrize("platform,error", [
    ("windows", errno.EACCES),
    ("posix", errno.EACCES),
    ("posix", errno.EAGAIN),
])
def test_nonblocking_contention_returns_false(lock_backend, platform, error):
    backend = lock_backend(platform, [OSError(error, "locked")])
    assert _flock.acquire(backend.handle, blocking=False) is False
    assert backend.operation.call_count == 1
    backend.sleep.assert_not_called()


def test_windows_blocking_waits_until_owner_releases(lock_backend):
    backend = lock_backend("windows", [OSError(errno.EACCES, "locked"), None])
    backend.sleep.side_effect = None
    assert _flock.acquire(backend.handle) is True
    assert backend.operation.call_count == 2
    backend.sleep.assert_called_once_with(0.01)


@pytest.mark.parametrize("platform", ["windows", "posix"])
@pytest.mark.parametrize("blocking", [True, False])
def test_interrupted_lock_retries_without_reporting_contention(lock_backend, platform, blocking):
    backend = lock_backend(platform, [OSError(errno.EINTR, "interrupted"), None])
    assert _flock.acquire(backend.handle, blocking=blocking) is True
    assert backend.operation.call_count == 2
    backend.sleep.assert_not_called()


def test_native_lock_contention_and_reacquisition(tmp_path):
    path = tmp_path / "native.lock"
    with path.open("a+b") as first, path.open("a+b") as second:
        assert _flock.acquire(first, blocking=False) is True
        try:
            assert _flock.acquire(second, blocking=False) is False
        finally:
            _flock.release(first)
        assert _flock.acquire(second, blocking=False) is True
        _flock.release(second)


@pytest.mark.parametrize("connector", ["watch", "session_hook"])
def test_connector_closes_handle_when_lock_fails(monkeypatch, tmp_path, connector):
    from mnemotrace.connectors import session_hook, watch

    module = watch if connector == "watch" else session_hook
    handles = []

    def tracked_open(*args, **kwargs):
        handle = open(*args, **kwargs)
        handles.append(handle)
        return handle

    failure = OSError(errno.EIO, "lock failed")
    monkeypatch.setattr(module, "open", tracked_open, raising=False)
    monkeypatch.setattr(_flock, "acquire", Mock(side_effect=failure))
    with pytest.raises(OSError) as caught:
        if connector == "watch":
            module._lock(str(tmp_path / "watch.lock"))
        else:
            module._lock(str(tmp_path), "synthetic-transcript")
    try:
        assert caught.value is failure
        assert len(handles) == 1
        assert handles[0].closed
    finally:
        for handle in handles:
            handle.close()
