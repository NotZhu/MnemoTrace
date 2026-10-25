from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from mnemotrace import Memory


def test_default_memory_does_not_import_lancedb(tmp_path):
    sys.modules.pop("lancedb", None)
    mem = Memory()
    mem.add("I like Python.", user_id="u")
    mem.save(str(tmp_path / "store"))
    assert "lancedb" not in sys.modules


def test_default_save_uses_jsonl_manifest(tmp_path):
    path = tmp_path / "store"
    mem = Memory()
    mem.add("I like Python.", user_id="u")
    mem.save(str(path))
    assert (path / "manifest.json").exists()
    assert (path / "episodes.jsonl").exists()


def test_zero_setup_script_runs_without_services(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {name: value for name, value in os.environ.items() if not name.startswith("MNEMOTRACE_")}
    env.update({
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONPYCACHEPREFIX": str(tmp_path / "bytecode"),
        "PYTHONUTF8": "1",
    })
    result = subprocess.run(
        [sys.executable, "scripts/check_zero_setup.py"],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK zero-setup verification passed" in result.stdout
