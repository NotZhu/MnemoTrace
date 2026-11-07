"""Check the HTTP authentication boundary with an isolated identity lookup."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mnemotrace.server import app as appmod
from mnemotrace.store.postgres.errors import AuthenticationError, DatabaseUnavailable


@pytest.mark.parametrize("key", ["中文密钥", "café-key", "a\x00b", "a\tb", "a\nb", "a\x7fb"])
def test_invalid_request_key_is_rejected_before_database_access(monkeypatch, key):
    service = Mock()
    monkeypatch.setattr(appmod, "svc", service)
    with pytest.raises(HTTPException) as caught:
        appmod.auth(f"Bearer {key}")
    assert caught.value.status_code == 401
    assert key not in caught.value.detail
    service.assert_not_called()


@pytest.mark.parametrize("header", ["", "Basic abc", "Bearer "])
def test_missing_and_malformed_credentials_cannot_use_legacy_open_mode(monkeypatch, header):
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    monkeypatch.setenv("MNEMOTRACE_ALLOW_ANONYMOUS", "1")
    service = Mock()
    monkeypatch.setattr(appmod, "svc", service)
    with pytest.raises((AuthenticationError, HTTPException)):
        appmod.auth(header)
    service.assert_not_called()


def test_valid_key_only_resolves_database_identity(monkeypatch):
    authenticate = Mock(return_value=SimpleNamespace(user_id="synthetic-owner"))
    monkeypatch.setattr(appmod, "svc", lambda: SimpleNamespace(spaces=SimpleNamespace(authenticate=authenticate)))
    for key in ("synthetic-old", "synthetic-new"):
        assert appmod.auth(f"Bearer {key}") == "synthetic-owner"
        authenticate.assert_called_with(key)


def test_invalid_database_key_is_401_and_errors_do_not_echo_key(monkeypatch):
    authenticate = Mock(side_effect=AuthenticationError("synthetic-bad"))
    monkeypatch.setattr(appmod, "svc", lambda: SimpleNamespace(spaces=SimpleNamespace(authenticate=authenticate)))
    response = TestClient(appmod.app).get("/v1/spaces", headers={"Authorization": "Bearer synthetic-bad"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_key"
    assert "synthetic-bad" not in response.text


def test_database_outage_is_live_but_not_ready(monkeypatch):
    ready = Mock(side_effect=DatabaseUnavailable("synthetic connection string"))
    monkeypatch.setattr(appmod, "svc", lambda: SimpleNamespace(ready=ready))
    client = TestClient(appmod.app)
    response = client.get("/health")
    assert response.status_code == 200 and response.json()["ok"] is True
    assert response.json()["ready"] is False
    response = client.get("/ready")
    assert response.status_code == 503
    assert "synthetic connection string" not in response.text
