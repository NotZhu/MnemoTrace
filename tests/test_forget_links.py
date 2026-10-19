"""Forget removes namespace entries without following links into other stores."""
import errno
import os
from pathlib import Path

import pytest

from mnemotrace.service import MemoryService


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "junction"])
def test_forget_preserves_link_targets(tmp_path, kind):
    data = tmp_path / "data"
    service = MemoryService(data_dir=str(data), embedder_name="hashing", llm_name="")
    service.add_fact("synthetic-user", "project", "marker", "erase-this")
    store = Path(service._path("synthetic-user"))
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep-other-data", encoding="utf-8")
    link = store / "linked-entry"
    target = marker if kind == "file" else outside
    if kind == "dangling":
        target = outside / "missing"
    try:
        if kind == "junction":
            if os.name != "nt":
                pytest.skip("Windows junction test")
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=kind == "directory")
    except OSError as exc:
        if getattr(exc, "winerror", None) in (5, 1314):
            pytest.skip("link creation requires Windows privileges")
        raise
    assert service.forget("synthetic-user")["ok"] is True
    assert not store.exists()
    assert marker.read_text(encoding="utf-8") == "keep-other-data"


def test_forget_propagates_directory_scan_failure(tmp_path, monkeypatch):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    service.add_fact("synthetic-user", "project", "marker", "keep-on-error")
    store = Path(service._path("synthetic-user"))
    store_identity = store.stat()
    scandir = os.scandir

    def fail_namespace_scan(path):
        is_store = (
            (os.fstat(path).st_dev, os.fstat(path).st_ino)
            == (store_identity.st_dev, store_identity.st_ino)
            if isinstance(path, int) else Path(path) == store
        )
        if is_store:
            raise PermissionError(errno.EACCES, "synthetic namespace scan failure")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", fail_namespace_scan)
    with pytest.raises(PermissionError, match="synthetic namespace scan failure"):
        service.forget("synthetic-user")
    assert store.is_dir()
