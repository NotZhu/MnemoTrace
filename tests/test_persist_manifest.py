"""Windows manifest opens during atomic replacement, through namespace discovery."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import time

import pytest

from mnemotrace import Memory
from mnemotrace._namespace_paths import namespace_path
from mnemotrace.consolidate.pipeline import PipelineTasks
from mnemotrace.store import persist

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows manifest sharing semantics')


@pytest.fixture
def namespace(tmp_path):
    path = namespace_path(str(tmp_path), 'u')
    with Memory() as memory:
        episode = memory.add('Synthetic pending source', user_id='u', consolidate=False)
        PipelineTasks(memory, 'u').register([episode], 100)
        memory.save(path)
    return tmp_path, Path(path)


def test_discovery_retries_transient_manifest_sharing_violation(namespace, monkeypatch):
    import _winapi
    root, _ = namespace
    original = _winapi.CreateFile
    calls, delays = [], []

    def opening(*args):
        calls.append(args[0])
        if len(calls) <= 2:
            raise ctypes.WinError(32)
        return original(*args)

    monkeypatch.setattr(_winapi, 'CreateFile', opening)
    monkeypatch.setattr(time, 'sleep', delays.append)
    assert persist.discover_processing_namespaces(str(root)) == [('u', True)]
    assert len(calls) == 3 and len(delays) == 2 and all(delay > 0 for delay in delays)


def test_manifest_sharing_retry_is_bounded(namespace, monkeypatch):
    import _winapi
    _, path = namespace
    calls, delays = [], []

    def blocked(*args):
        calls.append(args[0])
        raise ctypes.WinError(32)

    monkeypatch.setattr(_winapi, 'CreateFile', blocked)
    monkeypatch.setattr(time, 'sleep', delays.append)
    with pytest.raises(OSError) as failure:
        persist.snapshot_version(str(path))
    assert failure.value.winerror == 32
    assert 1 < len(calls) <= 20 and len(delays) == len(calls) - 1


def test_manifest_access_denied_is_not_retried(namespace, monkeypatch):
    import _winapi
    _, path = namespace
    calls, delays = [], []

    def denied(*args):
        calls.append(args[0])
        raise ctypes.WinError(5)

    monkeypatch.setattr(_winapi, 'CreateFile', denied)
    monkeypatch.setattr(time, 'sleep', delays.append)
    with pytest.raises(OSError) as failure:
        persist.snapshot_version(str(path))
    assert failure.value.winerror == 5 and len(calls) == 1 and not delays


def test_opened_corrupt_manifest_is_not_retried_or_treated_as_empty(namespace, monkeypatch):
    import _winapi
    root, path = namespace
    (path / 'manifest.json').write_text('{', encoding='utf-8')
    original = _winapi.CreateFile
    calls, delays = [], []

    def opening(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(_winapi, 'CreateFile', opening)
    monkeypatch.setattr(time, 'sleep', delays.append)
    with pytest.raises(persist.StoreFormatError):
        persist.discover_processing_namespaces(str(root))
    assert len(calls) == 1 and not delays
