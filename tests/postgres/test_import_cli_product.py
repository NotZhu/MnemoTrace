"""Import CLI uses the same PostgreSQL space and receipts as the service."""
import io
import json
import sys
import urllib.error

import pytest

from mnemotrace.connectors.__main__ import main
from mnemotrace.extraction import ExtractionError


@pytest.fixture
def direct_cli(cluster, product_scope, monkeypatch):
    context, _ = product_scope()
    monkeypatch.setenv("MNEMOTRACE_DATABASE_URL", cluster.runtime.dsn)
    monkeypatch.setenv("MNEMOTRACE_DB_ROLE_PREFIX", cluster.runtime.role_prefix)
    monkeypatch.setenv("MNEMOTRACE_LLM", "")
    monkeypatch.setenv("MNEMOTRACE_EMBEDDER", "hashing")
    return context, ["--space-id", context.space_id, "--user-id", context.user_id]


def invoke(monkeypatch, args, messages=None):
    messages = messages or [{"role": "user", "content": "Synthetic CLI episode"}]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(messages)))
    monkeypatch.setattr(sys, "argv", ["mnemotrace-import", "--format", "messages", *args])
    main()


def test_cli_missing_model_is_actionable_without_traceback(direct_cli, product_service, monkeypatch, capsys):
    context, args = direct_cli
    with pytest.raises(SystemExit) as stopped:
        invoke(monkeypatch, args)
    assert stopped.value.code == 1
    output = capsys.readouterr().err
    assert "llm_not_configured" in output and "Traceback" not in output
    assert not product_service.memory(context).list_sources()


def test_cli_remote_extraction_error_preserves_retry_details(monkeypatch, capsys):
    from mnemotrace.connectors import __main__ as cli
    error = ExtractionError(stored_raw=True, episode_ids=["synthetic-episode"])

    def remote(url, key, space_id, sessions, consolidate, summarize, agent_id):
        assert space_id == "synthetic-space"
        raise urllib.error.HTTPError(url, 502, "failed", {}, io.BytesIO(
            json.dumps({"detail": error.detail()}).encode()))

    monkeypatch.setattr(cli, "_post_remote", remote)
    with pytest.raises(SystemExit) as stopped:
        invoke(monkeypatch, ["--api-url", "https://synthetic.invalid", "--key", "synthetic-key",
                             "--space-id", "synthetic-space"])
    assert stopped.value.code == 1
    output = capsys.readouterr().err
    assert "synthetic-episode" in output and "extraction_failed" in output and "Traceback" not in output


def test_cli_and_service_share_scope_and_dedupe_after_restart(direct_cli, product_service, product_scope,
                                                         monkeypatch, capsys):
    context, args = direct_cli
    invoke(monkeypatch, [*args, "--no-consolidate", "--no-summarize", "--agent-id", "synthetic-cli"])
    first = json.loads(capsys.readouterr().out)
    assert first["episodes"] == 1
    invoke(monkeypatch, [*args, "--no-consolidate", "--no-summarize", "--agent-id", "synthetic-cli"])
    replay = json.loads(capsys.readouterr().out)
    assert replay["episodes"] == 0 and replay["skipped"] == 1
    source, = replay["results"][0]["source_ids"]
    assert product_service.get_episode(context, source)["agent_id"] == "synthetic-cli"
    other, _ = product_scope("different owner")
    assert product_service.get_episode(other, source) is None


def test_cli_imports_automatic_facts_with_configured_model(direct_cli, product_service, monkeypatch, capsys):
    context, args = direct_cli
    from tests.model_fixtures import RecordedLLM
    monkeypatch.setattr("mnemotrace.llm.providers.make_llm", lambda *_: RecordedLLM())
    invoke(monkeypatch, [*args, "--llm", "synthetic-fixture"], [{"role": "user", "content": "I work at Acme."}])
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["results"][0]["operations"][0]["core_committed"]
    assert any(row.object == "Acme" for row in product_service.memory(context).facts())
