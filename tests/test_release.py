from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import mnemotrace
import pytest

from scripts import check_release

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 没有 tomllib
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"


def test_release_versions_are_consistent():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdk = json.loads((ROOT / "clients/typescript/package.json").read_text(encoding="utf-8"))
    sdk_lock = json.loads(
        (ROOT / "clients/typescript/package-lock.json").read_text(encoding="utf-8")
    )
    frontend = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))

    assert mnemotrace.__version__ == VERSION
    assert pyproject["project"]["version"] == VERSION
    assert sdk["version"] == VERSION
    assert sdk_lock["version"] == VERSION
    assert sdk_lock["packages"][""]["version"] == VERSION
    assert frontend["version"] == VERSION
    assert "Development Status :: 4 - Beta" in pyproject["project"]["classifiers"]


def test_commercial_release_assets_exist():
    required = (
        "Dockerfile",
        ".dockerignore",
        "SECURITY.md",
        "CHANGELOG.md",
        "deploy/docker-compose.yml",
        "deploy/.env.example",
        "deploy/README.md",
        "docs/reference/http-api.md",
        "docs/architecture/README.md",
        ".github/workflows/ci.yml",
        "scripts/check_release.py",
    )

    missing = [path for path in required if not (ROOT / path).is_file()]
    assert missing == []


def test_release_gate_passes():
    result = subprocess.run(
        [sys.executable, "scripts/check_release.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK release metadata and deployment checks passed" in result.stdout


@pytest.fixture
def release_repository(tmp_path, monkeypatch):
    for rel in (
        "pyproject.toml", "mnemotrace/__init__.py",
        "clients/typescript/package.json", "clients/typescript/package-lock.json",
        "frontend/package.json", "LICENSE", "COMMERCIAL-LICENSE.md", "SECURITY.md",
        "CHANGELOG.md", "Dockerfile", ".dockerignore", "deploy/docker-compose.yml",
        "deploy/.env.example", "deploy/README.md", "README.md", "README.zh-CN.md",
        "docs/reference/http-api.md", "docs/README.md",
        "docs/architecture/README.md", ".github/workflows/ci.yml",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    monkeypatch.setattr(check_release, "ROOT", tmp_path)
    return tmp_path


def test_release_check_needs_no_historical_artifacts(release_repository):
    for name in ("RESULTS.md", "results", "paper", ".specify", "specs"):
        assert not (release_repository / name).exists()
    assert check_release.main() == 0


@pytest.mark.parametrize("defect", ["version", "root", "anonymous", "liveness"])
def test_release_check_rejects_invalid_metadata_and_deployment(release_repository, defect):
    def replace(rel, old, new):
        path = release_repository / rel
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new), encoding="utf-8")

    if defect == "version":
        replace("pyproject.toml", 'version = "0.1.0"', 'version = "0.1.1"')
    elif defect == "root":
        replace("Dockerfile", "USER 10001:10001", "USER root")
    elif defect == "anonymous":
        replace("Dockerfile", "USER 10001:10001", "ENV MNEMOTRACE_OPEN=1\nUSER 10001:10001")
    else:
        for rel in ("Dockerfile", "deploy/docker-compose.yml"):
            replace(rel, "/ready", "/health")
    assert check_release.main() == 1
