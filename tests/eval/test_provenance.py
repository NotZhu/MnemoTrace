"""验证评测源码、协议和依赖锁可从 Git 提交重算。"""

import subprocess
from pathlib import Path

import pytest

from eval import provenance


def test_source_commit_matches_the_evaluation_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """使用独立 Git 仓库验证可发布提交及未提交的评测变化。"""
    monkeypatch.setattr(provenance, "_ROOT", tmp_path)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init", "--quiet")
    package = tmp_path / "eval"
    package.mkdir()
    source = package / "runner.py"
    source.write_bytes(b"value = 1\n")
    (package / "dataset-lock.json").write_bytes(b"{}\n")
    (tmp_path / ".python-version").write_bytes(b"3.12\n")
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\n")
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"version = 1\n")
    git("-c", "core.autocrlf=false", "add", ".")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "--message",
        "evaluation inputs",
    )
    commit = git("rev-parse", "HEAD")
    expected = provenance.code_fingerprint()
    assert provenance.source_commit(expected) == commit
    assert provenance.committed_fingerprint(commit) == expected

    source.write_bytes(b"value = 1\r\n")
    (package / "README.md").write_bytes(b"review notes")
    assert provenance.source_commit(provenance.code_fingerprint()) == commit
    for path, content in (
        (package / "new.py", b"value = 2\n"),
        (source, b"value = 2\n"),
        (lock, b"version = 2\n"),
    ):
        previous = path.read_bytes() if path.exists() else None
        path.write_bytes(content)
        assert provenance.source_commit(provenance.code_fingerprint()) is None
        assert provenance.committed_fingerprint(commit) == expected
        if previous is None:
            path.unlink()
        else:
            path.write_bytes(previous)
