"""Exercise native ACL handling with an isolated administrator-default-owner model."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mnemotrace import credentials as C

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows security descriptors")


@pytest.fixture
def administrator_default_owner(monkeypatch):
    """Model TokenOwner=Administrators without changing the caller's process token.

    Only filesystem owner state is simulated; SID/descriptor conversion, DACL
    installation and validation still use the real Windows APIs. Inode tracking
    preserves the modeled owner when private_write atomically renames a file.
    """
    import ctypes as c
    from ctypes import wintypes as w

    load = c.WinDLL
    adv = load("advapi32", use_last_error=True)
    kernel = load("kernel32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, c.POINTER(w.LPVOID)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID]
    adv.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
    adv.GetSecurityDescriptorOwner.argtypes = [w.LPVOID, c.POINTER(w.LPVOID), c.POINTER(w.BOOL)]
    kernel.LocalFree.argtypes = [w.LPVOID]
    group_sid = w.LPVOID()
    assert adv.ConvertStringSidToSidW("S-1-5-32-544", c.byref(group_sid))
    assigned = set()
    state = {"reject_owner": False, "owner_calls": 0}

    def identity(path):
        info = Path(path).stat()
        return info.st_dev, info.st_ino

    def set_security(path, flags, descriptor):
        if flags & 1:  # OWNER_SECURITY_INFORMATION
            state["owner_calls"] += 1
            if state["reject_owner"]:
                # Protection must precede writing even when owner assignment fails.
                assert Path(path).read_bytes() == b""
                return 0
        result = adv.SetFileSecurityW(path, flags, descriptor)
        if result and flags & 1:
            assigned.add(identity(path))
        return result

    def get_security(path, *args):
        state["current"] = identity(path)
        return adv.GetFileSecurityW(path, *args)

    def get_owner(descriptor, owner, defaulted):
        result = adv.GetSecurityDescriptorOwner(descriptor, owner, defaulted)
        if result and state["current"] not in assigned:
            c.cast(owner, c.POINTER(w.LPVOID))[0] = group_sid.value
        return result

    class Advapi:
        SetFileSecurityW = staticmethod(set_security)
        GetFileSecurityW = staticmethod(get_security)
        GetSecurityDescriptorOwner = staticmethod(get_owner)

        def __getattr__(self, name):
            return getattr(adv, name)

    proxy = Advapi()
    monkeypatch.setattr(c, "WinDLL", lambda name, **kw: proxy if name == "advapi32" else load(name, **kw))
    try:
        yield state
    finally:
        kernel.LocalFree(group_sid)


def test_new_credentials_normalize_administrator_default_owner(tmp_path, administrator_default_owner):
    directory = tmp_path / "credentials"
    first = C.store_key("synthetic-owner-key", directory=directory)
    assert C.read_key(first) == "synthetic-owner-key"
    assert C.store_key("synthetic-owner-key", directory=directory, previous=first) == first
    assert list(directory.iterdir()) == [Path(first)]
    assert administrator_default_owner["owner_calls"] == 2


def test_private_config_and_backup_normalize_default_owner(tmp_path, administrator_default_owner):
    config = tmp_path / "config.toml"
    backup = tmp_path / "config.toml.bak"
    for path in (config, backup):
        C.private_write(path, b"synthetic-config")
        C.check_private(path)
        assert path.read_bytes() == b"synthetic-config"
    assert administrator_default_owner["owner_calls"] == 2


def test_read_only_validation_does_not_repair_group_owner(tmp_path, request):
    # Prepare an otherwise valid private file before injecting the default owner.
    path = tmp_path / "existing.key"
    C.private_write(path, b"synthetic-existing")
    state = request.getfixturevalue("administrator_default_owner")
    with pytest.raises(C.CredentialError, match="private Windows credential permissions"):
        C.check_private(path)
    assert state["owner_calls"] == 0
    assert path.read_bytes() == b"synthetic-existing"


def test_owner_assignment_failure_preserves_destination(tmp_path, administrator_default_owner):
    path = tmp_path / "config.toml"
    path.write_bytes(b"synthetic-original")
    administrator_default_owner["reject_owner"] = True
    with pytest.raises(C.CredentialError, match="private Windows credential permissions"):
        C.private_write(path, b"synthetic-new-secret")
    assert path.read_bytes() == b"synthetic-original"
    assert list(tmp_path.iterdir()) == [path]
