#!/usr/bin/env python3
"""核对发行元数据、交付文件和部署配置；文档导航由独立检查负责。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from zipfile import ZipFile

try:
    import tomllib
except ModuleNotFoundError:  # 兼容 Python 3.10 的发布/开发工具链。
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _check(condition: bool, message: str, errors: list[str]) -> None:
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {message}")
    if not condition:
        errors.append(message)


def main(*, wheel=None) -> int:
    errors: list[str] = []
    pyproject = tomllib.loads(_read("pyproject.toml"))
    sdk = json.loads(_read("clients/typescript/package.json"))
    sdk_lock = json.loads(_read("clients/typescript/package-lock.json"))
    frontend = json.loads(_read("frontend/package.json"))
    init_text = _read("mnemotrace/__init__.py")
    init_version = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)

    versions = {
        "python package": pyproject["project"]["version"],
        "python runtime": init_version.group(1) if init_version else "missing",
        "TypeScript SDK": sdk["version"],
        "TypeScript lock": sdk_lock["version"],
        "TypeScript lock root": sdk_lock["packages"][""]["version"],
        "management console": frontend["version"],
    }
    for component, version in versions.items():
        _check(version == VERSION, f"{component} version is {VERSION}", errors)

    _check(
        "Development Status :: 4 - Beta" in pyproject["project"]["classifiers"],
        "package status is Beta",
        errors,
    )
    _check(
        pyproject["project"]["license"] == "AGPL-3.0-only",
        "package license is AGPL-3.0-only",
        errors,
    )
    extras = pyproject['project']['optional-dependencies']
    _check(bool(extras.get('postgres')), 'Postgres dependencies are an explicit extra', errors)
    _check(all('postgres' in ''.join(extras[name]) for name in ('serve', 'all')),
           'service installations include the Postgres extra', errors)
    _check(not pyproject['project']['dependencies'], 'algorithm core has no hard dependencies', errors)

    required = (
        "LICENSE",
        "COMMERCIAL-LICENSE.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "Dockerfile",
        ".dockerignore",
        "deploy/docker-compose.yml",
        "deploy/.env.example",
        "deploy/README.md",
        "README.md",
        "README.zh-CN.md",
        "docs/reference/http-api.md",
        "docs/README.md",
        "docs/architecture/README.md",
        ".github/workflows/ci.yml",
    )
    for path in required:
        _check((ROOT / path).is_file(), f"required release file exists: {path}", errors)

    docker_text = _read("Dockerfile")
    compose_text = _read("deploy/docker-compose.yml")
    _check("USER 10001:10001" in docker_text, "container runs as non-root", errors)
    _check("MNEMOTRACE_OPEN" not in docker_text + compose_text, "container is not open by default", errors)
    _check("/ready" in docker_text + compose_text, "container health uses readiness", errors)
    _check(all(service in compose_text for service in (
        'pgvector/pgvector:0.8.7-pg18', 'db-init:', 'mnemotrace.worker', 'postgres:/var/lib/postgresql',
    )), 'Compose defines database, initialization, worker and persistent storage', errors)
    if wheel is not None:
        sys.path.insert(0, str(ROOT))
        from mnemotrace.store.postgres.migrate import MIGRATIONS

        with ZipFile(wheel) as archive:
            files = set(archive.namelist())
        required_wheel = {
            'mnemotrace/py.typed', 'mnemotrace/admin.py', 'mnemotrace/worker.py',
            'mnemotrace/quickstart.py', 'mnemotrace/memory.py',
            'mnemotrace/memory_engine.py', 'mnemotrace/service.py',
            *(f'mnemotrace/store/postgres/sql/{script}' for script in MIGRATIONS),
        }
        for path in sorted(required_wheel):
            _check(path in files, f'wheel contains {path}', errors)

    _check("商业授权" in _read("COMMERCIAL-LICENSE.md"), "commercial license has Chinese terms", errors)
    _check("漏洞" in _read("SECURITY.md"), "security policy has Chinese reporting guidance", errors)

    if errors:
        print(f"\nrelease metadata and deployment checks failed: {len(errors)}", file=sys.stderr)
        return 1
    print("\nOK release metadata and deployment checks passed")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path)
    raise SystemExit(main(wheel=parser.parse_args().wheel))
