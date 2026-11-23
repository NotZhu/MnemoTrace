"""把评测代码指纹锚定到可检出的 Git 提交。"""

import re
import subprocess
import tarfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parents[1]
_BUILD_INPUTS = (".python-version", "pyproject.toml", "uv.lock")
_COMMIT = re.compile(r"[a-f0-9]{40}(?:[a-f0-9]{24})?")


def _is_source(name: str) -> bool:
    path = PurePosixPath(name)
    return name in _BUILD_INPUTS or (
        path.is_relative_to("eval") and path.suffix in {".py", ".json"}
    )


def _fingerprint(files: dict[str, bytes]) -> str:
    digest = sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode() + b"\0")
        digest.update(sha256(content.replace(b"\r\n", b"\n")).digest())
    return digest.hexdigest()


def _git(*args: str) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=_ROOT, capture_output=True, check=False
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def code_fingerprint() -> str:
    """计算实际工作区的评测源码、协议文件和依赖输入指纹。"""
    paths = (*(_ROOT / "eval").rglob("*"), *(_ROOT / name for name in _BUILD_INPUTS))
    files = {}
    for path in paths:
        name = path.relative_to(_ROOT).as_posix()
        if path.is_file() and _is_source(name):
            files[name] = path.read_bytes()
    return _fingerprint(files)


def committed_fingerprint(commit: str | None) -> str | None:
    """从指定提交重算同一组文件的指纹，供发布校验使用。"""
    if commit is None or not _COMMIT.fullmatch(commit):
        return None
    archive = _git("archive", "--format=tar", commit, "eval", *_BUILD_INPUTS)
    if archive is None:
        return None
    files = {}
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if member.isfile() and _is_source(member.name):
                source = tar.extractfile(member)
                assert source is not None
                with source:
                    files[member.name] = source.read()
    return _fingerprint(files)


def source_commit(fingerprint: str) -> str | None:
    """仅在 HEAD 持有同一份评测代码时记录提交。"""
    head = _git("rev-parse", "HEAD")
    commit = head.decode("ascii").strip() if head else None
    return commit if committed_fingerprint(commit) == fingerprint else None
