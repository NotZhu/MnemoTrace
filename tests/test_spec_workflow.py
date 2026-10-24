"""Exercise the minimal major-change lifecycle in an isolated repository."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def bash_executable():
    # Windows' system bash is a WSL launcher, not an installed Unix toolchain.
    git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if os.name == "nt":
        return str(git_bash) if git_bash.is_file() else None
    return shutil.which("bash")


@pytest.fixture
def workflow(tmp_path):
    bash = bash_executable()
    if bash is None:
        pytest.skip("Bash toolchain is unavailable")
    shutil.copytree(ROOT / ".specify", tmp_path / ".specify", ignore=shutil.ignore_patterns("feature.json"))
    sentinel = b"# Project rules\nDo not append generated context.\n"
    (tmp_path / "AGENTS.md").write_bytes(sentinel)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECIFY_")}

    def run(script, *args, success=True):
        result = subprocess.run(
            [bash, f".specify/scripts/bash/{script}.sh", *args],
            cwd=tmp_path, env=env, capture_output=True, text=True,
            encoding="utf-8", timeout=30,
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        assert (tmp_path / "AGENTS.md").read_bytes() == sentinel
        return json.loads(result.stdout) if success else result.stderr

    return tmp_path, run


def test_minimal_design_cycle_needs_no_placeholder_documents(workflow):
    root, run = workflow
    error = run("setup-plan", "--json", success=False)
    assert "Feature directory not found" in error
    created = run("create-new-feature", "--json", "--short-name", "memory-contract", "Refine memory contract")
    assert re.fullmatch(r"\d{8}-\d{6}-memory-contract", created["BRANCH_NAME"])
    feature = root / "specs" / created["BRANCH_NAME"]
    assert {p.name for p in feature.iterdir()} == {"spec.md"}
    assert json.loads((root / ".specify/feature.json").read_text())["feature_directory"] == f"specs/{feature.name}"
    run("setup-plan", "--json")
    tasks = run("setup-tasks", "--json")
    assert tasks["AVAILABLE_DOCS"] == []
    assert tasks["TASKS_TEMPLATE"].endswith("tasks-template.md")
    (feature / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    (feature / "analysis.md").write_text("PASS\n", encoding="utf-8")
    assert "tasks.md" in run("check-prerequisites", "--json", "--require-tasks", "--include-tasks")["AVAILABLE_DOCS"]
    assert {p.name for p in feature.iterdir()} == {"spec.md", "plan.md", "tasks.md", "analysis.md"}
    # Retiring a suite must not silently reactivate a previously completed plan.
    shutil.rmtree(feature)
    (root / ".specify/feature.json").unlink()
    assert "Feature directory not found" in run("setup-plan", "--json", success=False)


def test_major_workflow_places_analysis_before_implementation():
    workflow = (ROOT / ".specify/workflows/speckit/workflow.yml").read_text(encoding="utf-8")
    assert workflow.index("command: speckit.analyze") < workflow.index("command: speckit.implement")
    between = workflow.split("command: speckit.analyze", 1)[1].split("command: speckit.implement", 1)[0]
    assert "review-analysis" in between
    assert "type: gate" in between
    assert workflow.count("  - id: analyze\n") == 1


def test_workflow_registry_describes_the_active_workflow():
    workflow = (ROOT / ".specify/workflows/speckit/workflow.yml").read_text(encoding="utf-8")
    registry = json.loads((ROOT / ".specify/workflows/workflow-registry.json").read_text(encoding="utf-8"))
    entry = registry["workflows"]["speckit"]
    for key in ("name", "version", "description"):
        actual = re.search(rf'^  {key}: "(.+)"$', workflow, re.M)
        assert actual is not None
        assert entry[key] == actual[1]


def test_explicit_number_option_is_not_silently_ignored(workflow):
    root, run = workflow
    created = run(
        "create-new-feature", "--json", "--dry-run", "--number", "5",
        "--short-name", "memory-contract", "Refine memory contract",
    )
    assert created["BRANCH_NAME"] == "005-memory-contract"
    assert created["DRY_RUN"] is True
    assert not (root / "specs/005-memory-contract").exists()
    assert not (root / ".specify/feature.json").exists()
