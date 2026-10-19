import os
from pathlib import Path

import pytest

from mnemotrace.embed import HashingEmbedder
from mnemotrace.llm import providers
from mnemotrace.service import MemoryService


def test_service_ignores_developer_dotenv_during_tests(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "MNEMOTRACE_EMBEDDER=not-a-local-model\n"
        "MNEMOTRACE_LLM=not-a-local-model\n"
        "MNEMOTRACE_API_KEYS=test:synthetic-key\n",
        encoding="utf-8",
    )
    service = MemoryService(data_dir=str(tmp_path / "data"))
    assert isinstance(service.embedder, HashingEmbedder)
    assert service.llm is None
    assert service.answerer is None


@pytest.mark.dotenv
def test_dotenv_loader_uses_first_value_and_preserves_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MNEMOTRACE_TEST_EXISTING", "environment")
    (tmp_path / ".env").write_text(
        "MNEMOTRACE_TEST_EXISTING=file\n"
        "MNEMOTRACE_TEST_DUPLICATE=first\n"
        "MNEMOTRACE_TEST_DUPLICATE=second\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MNEMOTRACE_TEST_DUPLICATE", raising=False)
    providers.load_dotenv()

    assert os.environ["MNEMOTRACE_TEST_EXISTING"] == "environment"
    assert os.environ["MNEMOTRACE_TEST_DUPLICATE"] == "first"
    monkeypatch.delenv("MNEMOTRACE_TEST_DUPLICATE")


def test_new_default_directory_does_not_read_legacy_memory(monkeypatch, tmp_path):
    from mnemotrace import service as service_module

    assert Path(service_module.DEFAULT_DATA_DIR) == Path(os.path.expanduser("~/.mnemotrace/data"))
    monkeypatch.setattr(service_module, "DEFAULT_DATA_DIR", str(tmp_path / ".mnemotrace" / "data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".engram" / "data"
    old.mkdir(parents=True)
    sentinel = old / "do-not-change.txt"
    sentinel.write_text("synthetic old data", encoding="utf-8")
    service = MemoryService(embedder_name="hashing", llm_name="")
    assert Path(service.data_dir) == tmp_path / ".mnemotrace" / "data"
    assert sentinel.read_text(encoding="utf-8") == "synthetic old data"
    assert not list(Path(service.data_dir).glob("*/manifest.json"))
