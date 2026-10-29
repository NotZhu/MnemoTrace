"""Console seed output must be readable through the actual service namespace."""
from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.model_fixtures import Memory
from tests.model_fixtures import MemoryService


ROOT = Path(__file__).resolve().parents[1]
FIRST_DIRECTORY = "1--6b86b273ff34fce1"


@pytest.fixture
def seed_input(tmp_path, monkeypatch):
    # Neither the CLI nor service should discover developer credentials or providers.
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("MNEMOTRACE_"):
            monkeypatch.delenv(name)
    source = tmp_path / "synthetic.json"
    source.write_text(json.dumps([
        {
            "question_type": "synthetic",
            "question": "Where do I live?",
            "answer": city,
            "haystack_sessions": [[{"role": "user", "content": f"I live in {city}."}]],
            "haystack_session_ids": [f"synthetic-{i}"],
        }
        for i, city in enumerate(("Paris", "London", "Tokyo", "Berlin"))
    ]), encoding="utf-8")
    return source


def run_seed(source, output, *args):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys,runpy;sys.path.insert(0,sys.argv[1]);"
         "from mnemotrace.llm import providers;from tests.model_fixtures import RecordedLLM;"
         "providers.make_llm=lambda *a,**k:RecordedLLM();"
         "sys.argv=sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')",
         str(ROOT), str(ROOT / "eval/seed_console.py"),
         "--data", str(source), "--out", str(output), "--embedder", "hashing",
         "--llm", "synthetic-fixture", *args],
        cwd=source.parent, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=30,
    )


def snapshot(directory):
    return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def service_at(directory):
    return MemoryService(data_dir=str(directory), embedder_name="hashing", llm_name="")


@pytest.fixture
def copied_seed(seed_input):
    output = seed_input.parent / "generated"
    result = run_seed(seed_input, output, "--limit", "1")
    assert result.returncode == 0, result.stderr
    before = snapshot(output)
    data = seed_input.parent / "service-data"
    data.mkdir()
    # Copy complete store directories while preserving names, as an operator would.
    rows = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    for row in rows:
        shutil.copytree(output / row["directory"], data / row["directory"])
    assert snapshot(output) == before
    return output, data


def test_generated_seed_is_visible_in_service_after_copy_and_restart(copied_seed):
    output, data = copied_seed
    for _ in range(2):
        service = service_at(data)
        facts = service.memories("1")["facts"]
        assert {f["object"] for f in facts} == {"Paris"}
        assert service.memories("2")["facts"] == []
    assert (output / FIRST_DIRECTORY).is_dir()
    assert not (output / "1").exists()


def test_manifest_describes_namespace_and_store_directory(copied_seed):
    output, _ = copied_seed
    rows = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert rows[0]["namespace"] == "1"
    assert rows[0]["directory"] == FIRST_DIRECTORY
    assert "key" not in rows[0]
    assert rows[0]["facts_live"] == 1
    markdown = (output / "manifest.md").read_text(encoding="utf-8")
    assert FIRST_DIRECTORY in markdown
    assert "API Key" in markdown and "开放模式" in markdown


@pytest.mark.parametrize("protected", [False, True])
def test_login_identity_reaches_generated_namespace(copied_seed, monkeypatch, protected):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod

    _, data = copied_seed
    monkeypatch.setattr(appmod, "_svc", service_at(data))
    if protected:
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", "1:synthetic-seed-key,2:synthetic-other-key")
        token, other = "synthetic-seed-key", "synthetic-other-key"
    else:
        monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
        token, other = "1", "2"
    with TestClient(appmod.app) as client:
        response = client.get("/v1/memories", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert {f["object"] for f in response.json()["facts"]} == {"Paris"}
        other_response = client.get("/v1/memories", headers={"Authorization": f"Bearer {other}"})
        assert other_response.status_code == 200
        assert other_response.json()["facts"] == []


@pytest.mark.parametrize("collision", [FIRST_DIRECTORY, "manifest.json", "manifest.md"])
@pytest.mark.parametrize("miss_preflight", [False, True])
def test_existing_outputs_are_rejected_before_loading_models(
    seed_input, collision, miss_preflight, monkeypatch, capsys,
):
    output = seed_input.parent / "generated"
    target = output / collision
    target.parent.mkdir(parents=True)
    if collision == FIRST_DIRECTORY:
        target.mkdir()
        (target / "marker").write_text("SYNTHETIC_EXISTING_STORE", encoding="utf-8")
    else:
        target.write_text("SYNTHETIC_EXISTING_MANIFEST", encoding="utf-8")
    before = snapshot(output)
    if miss_preflight:
        # Simulate another writer creating an output after the advisory preflight.
        monkeypatch.setattr(os.path, "lexists", lambda path: False)

    def forbid_models(*args, **kwargs):
        pytest.fail("model initialization must not happen when outputs already exist")

    monkeypatch.setattr("mnemotrace.llm.providers.make_embedder", forbid_models)
    monkeypatch.setattr("mnemotrace.llm.providers.make_llm", forbid_models)
    monkeypatch.setattr(sys, "argv", ["seed_console.py", "--data", str(seed_input),
                                    "--out", str(output), "--limit", "1"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "eval/seed_console.py"), run_name="__main__")
    assert exc.value.code == 2
    assert "already exists" in capsys.readouterr().err
    after = snapshot(output)
    assert all(after[name] == content for name, content in before.items())
    if not miss_preflight:
        assert after == before


def test_shards_keep_original_numbers_and_do_not_overwrite(seed_input):
    output = seed_input.parent / "generated"
    first = run_seed(seed_input, output, "--limit", "3", "--offset", "1", "--shard", "0/2")
    assert first.returncode == 0, first.stderr
    before = snapshot(output)
    second = run_seed(seed_input, output, "--limit", "3", "--offset", "1", "--shard", "1/2")
    assert second.returncode == 0, second.stderr
    assert all((output / name).read_bytes() == content for name, content in before.items())
    rows = []
    for manifest in sorted(output.glob("manifest.shard*.json")):
        rows.extend(json.loads(manifest.read_text(encoding="utf-8")))
    assert {row["namespace"] for row in rows} == {"2", "3"}
    assert len({row["directory"] for row in rows}) == 2
    service = service_at(output)
    assert {f["object"] for f in service.memories("2")["facts"]} == {"London"}
    assert {f["object"] for f in service.memories("3")["facts"]} == {"Tokyo"}
    assert service.memories("1")["facts"] == []
    assert service.memories("4")["facts"] == []


def test_regeneration_refuses_to_overwrite_completed_output(seed_input):
    output = seed_input.parent / "generated"
    assert run_seed(seed_input, output, "--limit", "1").returncode == 0
    before = snapshot(output)
    result = run_seed(seed_input, output, "--limit", "1")
    assert result.returncode != 0
    assert "already exists" in result.stderr
    assert snapshot(output) == before


def test_generation_preserves_existing_raw_namespace_store(seed_input):
    output = seed_input.parent / "generated"
    legacy = output / "1"
    memory = Memory()
    memory.add_fact("project", "marker", "SYNTHETIC_LEGACY_ONLY", user_id="1")
    memory.save(str(legacy))
    before = snapshot(legacy)
    result = run_seed(seed_input, output, "--limit", "1")
    assert result.returncode == 0, result.stderr
    assert snapshot(legacy) == before
    assert {f["object"] for f in service_at(output).memories("1")["facts"]} == {"Paris"}
