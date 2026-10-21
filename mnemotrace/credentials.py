"""Private local credentials shared by setup, MCP and doctor (standard library only)."""
from __future__ import annotations

import getpass
import os
import re
import stat
import tempfile
import uuid
import warnings
from pathlib import Path

from .spaces import is_valid_api_key


class CredentialError(ValueError):
    """Safe to display: never includes credential contents or provider output."""


def validate_key(key: str) -> str:
    if len(key) > 8192 or not is_valid_api_key(key):
        raise CredentialError("API key must be non-empty printable ASCII.")
    return key


def prompt_key() -> str:
    try:
        # getpass normally falls back to echoed stdin; this input must fail closed instead.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return validate_key(getpass.getpass("MnemoTrace API Key (hidden): ").strip())
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt, OSError) as exc:
        raise CredentialError("Hidden credential input unavailable or cancelled; no config written.") from exc


def _plain_path(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CredentialError("Credential/config paths must not contain links or reparse points.")


def _windows_security(path: Path, *, protect: bool = False) -> None:
    """Use protected native DACLs, rather than Windows' ineffective POSIX mode bits."""
    import ctypes as c
    from ctypes import wintypes as w

    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [w.LPVOID, c.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(w.LPVOID), c.POINTER(w.DWORD)]
    adv.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID]
    adv.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
    adv.GetSecurityDescriptorDacl.argtypes = [w.LPVOID, c.POINTER(w.BOOL), c.POINTER(w.LPVOID), c.POINTER(w.BOOL)]
    adv.GetSecurityDescriptorControl.argtypes = [w.LPVOID, c.POINTER(w.WORD), c.POINTER(w.DWORD)]
    adv.GetSecurityDescriptorOwner.argtypes = [w.LPVOID, c.POINTER(w.LPVOID), c.POINTER(w.BOOL)]
    adv.GetAce.argtypes = [w.LPVOID, w.DWORD, c.POINTER(w.LPVOID)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [w.LPVOID]

    def require(ok):
        if not ok:
            raise CredentialError("Cannot establish/verify private Windows credential permissions.")

    def sid_text(pointer):
        value = w.LPWSTR()
        require(adv.ConvertSidToStringSidW(pointer, c.byref(value)))
        try:
            return value.value
        finally:
            kernel.LocalFree(c.cast(value, w.LPVOID))

    token = w.HANDLE()
    require(adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, c.byref(token)))
    try:
        needed = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        buffer = c.create_string_buffer(needed.value)
        require(adv.GetTokenInformation(token, 1, buffer, needed, c.byref(needed)))
        user_sid = sid_text(c.cast(buffer, c.POINTER(w.LPVOID))[0])
    finally:
        kernel.CloseHandle(token)

    if protect:
        inheritance = "OICI" if path.is_dir() else ""
        # Elevated tokens can default new objects to the Administrators group.
        # Establish the owner as well as the DACL before writing private bytes.
        sddl = f"O:{user_sid}D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;{user_sid})"
        descriptor = w.LPVOID()
        require(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
        try:
            require(adv.SetFileSecurityW(str(path), 0x80000005, descriptor))
        finally:
            kernel.LocalFree(descriptor)

    needed = w.DWORD()
    adv.GetFileSecurityW(str(path), 5, None, 0, c.byref(needed))
    descriptor = c.create_string_buffer(needed.value)
    require(adv.GetFileSecurityW(str(path), 5, descriptor, needed, c.byref(needed)))
    control, revision = w.WORD(), w.DWORD()
    require(adv.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
    require(control.value & 0x1000)  # SE_DACL_PROTECTED: no broad inherited grants.
    owner, defaulted = w.LPVOID(), w.BOOL()
    require(adv.GetSecurityDescriptorOwner(descriptor, c.byref(owner), c.byref(defaulted)))
    require(sid_text(owner) in {user_sid, "S-1-5-18"})
    present, acl = w.BOOL(), w.LPVOID()
    require(adv.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(acl), c.byref(defaulted)))
    require(present.value and acl.value)
    # ACL header: revision/size followed by AceCount at byte offset 4.
    count = c.c_ushort.from_address(acl.value + 4).value
    grants = set()
    for index in range(count):
        ace = w.LPVOID()
        require(adv.GetAce(acl, index, c.byref(ace)))
        require(c.c_ubyte.from_address(ace.value).value == 0)  # ACCESS_ALLOWED_ACE
        require(not (c.c_ubyte.from_address(ace.value + 1).value & 0x08))  # not inherit-only
        mask = w.DWORD.from_address(ace.value + 4).value
        identity = sid_text(ace.value + 8)
        require(identity in {user_sid, "S-1-5-18"} and mask == 0x1F01FF)
        grants.add(identity)
    require(user_sid in grants)


def check_private(path: Path) -> None:
    path = Path(path).absolute()
    try:
        _plain_path(path)
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise CredentialError("Credential path must be a regular file or directory.")
        if os.name == "nt":
            _windows_security(path)
        elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if path.is_dir() else 0o600):
            raise CredentialError("Private credentials require owner-only 0700 directories / 0600 files.")
    except OSError as exc:
        raise CredentialError("Cannot access private credential path.") from exc


def _protect(path: Path) -> None:
    if os.name == "nt":
        _windows_security(path, protect=True)
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    check_private(path)


def private_write(path: Path, data: bytes) -> None:
    """Secure an empty temporary file before writing any bytes, then atomically publish."""
    path = Path(path).absolute()
    temporary = None
    try:
        _plain_path(path)
        if path.exists() and not path.is_file():
            raise CredentialError("Config destination must be a regular file.")
        fd, name = tempfile.mkstemp(prefix=".mnemotrace-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            _protect(temporary)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _plain_path(path)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise CredentialError("Could not securely write credential/config file; destination not updated.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_config(path: Path) -> str:
    """Validate links before reading an existing installation target or its backup bytes."""
    path = Path(path).absolute()
    try:
        _plain_path(path)
        if not path.exists():
            return ""
        if not path.is_file():
            raise CredentialError("Config destination must be a regular file.")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CredentialError("Cannot read installation config.") from exc


def credential_path(path: str | Path) -> Path:
    """Anchor a reference at call time without resolving links before safety checks."""
    return Path(path).expanduser().absolute()


def read_key(path: str | Path) -> str:
    path = credential_path(path)
    try:
        check_private(path.parent)
        check_private(path)
        if not path.is_file():
            raise CredentialError("Credential must be a regular file.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not os.path.samestat(info, path.stat()):
                raise CredentialError("Credential file changed during access.")
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise CredentialError("Credential file is too large.")
        return validate_key(raw.decode("ascii"))
    except (OSError, UnicodeError) as exc:
        raise CredentialError("Cannot read private credential file.") from exc


def store_key(key: str, *, directory: Path | None = None, previous: str | None = None) -> str:
    validate_key(key)
    if previous is not None and read_key(previous) == key:
        return str(credential_path(previous))
    directory = Path(directory) if directory is not None else Path.home() / ".mnemotrace" / "credentials"
    directory = credential_path(directory)
    try:
        _plain_path(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            check_private(directory)
        else:
            _protect(directory)
        path = directory / f"{uuid.uuid4().hex}.key"
        private_write(path, key.encode("ascii"))
        return str(path)
    except OSError as exc:
        raise CredentialError("Cannot create private credential directory.") from exc


def resolve_key(*, key: str | None = None, key_file: str | Path | None = None,
                prompt: bool = False, required: bool = True) -> str | None:
    if sum((key is not None, key_file is not None, prompt)) > 1:
        raise CredentialError("Choose exactly one credential input: key, file, or hidden prompt.")
    if key_file is not None:
        return read_key(key_file)
    if prompt:
        return prompt_key()
    value = key if key is not None else os.environ.get("MNEMOTRACE_API_KEY")
    if value is not None:
        return validate_key(value)
    if required:
        raise CredentialError("Remote access requires --prompt-api-key, --api-key-file or MNEMOTRACE_API_KEY.")
    return None


def redact(value, key: str | None):
    """Redact content values; dictionary keys belong to the caller's schema."""
    if not key:
        return value
    if isinstance(value, str):
        # Short development namespace keys must not corrupt words such as "remember".
        if len(key) < 8:
            return re.sub(r"(?<![\w-])" + re.escape(key) + r"(?![\w-])", "<REDACTED>", value)
        return value.replace(key, "<REDACTED>")
    if isinstance(value, dict):
        return {k: redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value
