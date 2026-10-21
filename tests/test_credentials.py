"""Private credential files and hidden input use only synthetic data."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mnemotrace import credentials as C


def test_private_roundtrip_reuse_and_rotation(tmp_path):
    directory = tmp_path / "credentials"
    first = C.store_key("synthetic-first", directory=directory)
    assert C.read_key(first) == "synthetic-first"
    assert C.store_key("synthetic-first", directory=directory, previous=first) == first
    second = C.store_key("synthetic-second", directory=directory, previous=first)
    assert first != second and C.read_key(first) == "synthetic-first"
    assert C.read_key(second) == "synthetic-second"
    C.check_private(directory)
    C.check_private(Path(first))


def test_invalid_and_failed_write_do_not_publish(tmp_path, monkeypatch):
    directory = tmp_path / "credentials"
    with pytest.raises(C.CredentialError):
        C.store_key("synthetic\ninvalid", directory=directory)
    assert not directory.exists()
    monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("synthetic failure")))
    with pytest.raises(C.CredentialError):
        C.store_key("synthetic-valid", directory=directory)
    assert list(directory.iterdir()) == []


def test_reject_nonregular_and_links(tmp_path):
    directory = tmp_path / "credentials"
    key = Path(C.store_key("synthetic-key", directory=directory))
    with pytest.raises(C.CredentialError):
        C.read_key(directory)
    link = directory / "link.key"
    try:
        link.symlink_to(key)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(C.CredentialError):
        C.read_key(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses native DACL checks")
def test_reject_loose_file_or_directory_permissions(tmp_path):
    directory = tmp_path / "credentials"
    key = Path(C.store_key("synthetic-key", directory=directory))
    key.chmod(0o644)
    with pytest.raises(C.CredentialError):
        C.read_key(key)
    key.chmod(0o600)
    directory.chmod(0o755)
    with pytest.raises(C.CredentialError):
        C.read_key(key)


def test_hidden_input_fails_closed(monkeypatch):
    import getpass
    import warnings

    def echoing(*a, **kw):
        warnings.warn("synthetic echo fallback", getpass.GetPassWarning)
        return "synthetic-key"
    monkeypatch.setattr(getpass, "getpass", echoing)
    with pytest.raises(C.CredentialError):
        C.prompt_key()
    monkeypatch.setattr(getpass, "getpass", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(C.CredentialError):
        C.prompt_key()


def test_explicit_file_failure_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "synthetic-other")
    with pytest.raises(C.CredentialError):
        C.resolve_key(key_file=tmp_path / "missing")
    with pytest.raises(C.CredentialError):
        C.resolve_key(key="synthetic-one", key_file=tmp_path / "missing")


def test_secure_backup_and_redaction(tmp_path):
    path = tmp_path / "backup.toml"
    C.private_write(path, b"synthetic-private")
    C.check_private(path)
    assert path.read_bytes() == b"synthetic-private"
    value = C.redact({"detail": "synthetic-key", "nested": ["Bearer synthetic-key"]}, "synthetic-key")
    assert "synthetic-key" not in str(value)


def test_permission_failure_writes_no_secret_bytes(tmp_path, monkeypatch):
    destination = tmp_path / "private.key"
    def reject(path):
        assert path.read_bytes() == b""
        raise C.CredentialError("synthetic permission failure")
    monkeypatch.setattr(C, "_protect", reject)
    with pytest.raises(C.CredentialError):
        C.private_write(destination, b"synthetic-secret")
    assert not destination.exists() and list(tmp_path.iterdir()) == []


def test_oversized_key_cannot_create_unreadable_file(tmp_path):
    with pytest.raises(C.CredentialError):
        C.store_key("a" * 8193, directory=tmp_path / "credentials")
    assert not (tmp_path / "credentials").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows DACL")
def test_windows_rejects_broad_dacl(tmp_path):
    import ctypes as c
    from ctypes import wintypes as w

    key = Path(C.store_key("synthetic-key", directory=tmp_path / "credentials"))
    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    descriptor = w.LPVOID()
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), c.POINTER(w.DWORD)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID]
    kernel.LocalFree.argtypes = [w.LPVOID]
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:P(A;;FA;;;WD)", 1, c.byref(descriptor), None)
    try:
        assert adv.SetFileSecurityW(str(key), 0x80000004, descriptor)
    finally:
        kernel.LocalFree(descriptor)
    with pytest.raises(C.CredentialError):
        C.read_key(key)
