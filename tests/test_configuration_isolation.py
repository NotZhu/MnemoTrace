import os

import pytest

from mnemotrace.llm import providers


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
