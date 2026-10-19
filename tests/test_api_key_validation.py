"""Exercise real authentication entry points without a network server or provider keys."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException

from mnemotrace.server import app as appmod
from mnemotrace import spaces


INVALID_KEYS = ["中文密钥", "caf\u00e9-key", "a\x00b", "a\tb", "a\nb", "a\x7fb"]


@pytest.mark.parametrize("key", [key for key in INVALID_KEYS if "\x00" not in key])
def test_api_key_configuration_rejects_invalid_characters(monkeypatch, key):
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", f"tenant-a:synthetic-good,tenant-b:{key}")
    with pytest.raises(appmod.AuthConfigError) as caught:
        appmod._load_keys()
    assert "entry 2" in str(caught.value)
    assert key not in str(caught.value)


@pytest.mark.parametrize("key", [*INVALID_KEYS, "", "   "])
def test_spaces_configuration_rejects_invalid_keys(monkeypatch, key):
    monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({
        "synthetic-good": {"home": "tenant-a"}, key: {"home": "tenant-b"},
    }))
    with pytest.raises(ValueError):
        spaces.load_spaces()


@pytest.mark.parametrize("entrypoint", [appmod.auth, appmod.auth_principal])
@pytest.mark.parametrize("key", INVALID_KEYS)
def test_invalid_request_key_is_unauthorized(monkeypatch, entrypoint, key):
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "tenant-a:synthetic-good")
    with pytest.raises(HTTPException) as caught:
        entrypoint(f"Bearer {key}")
    assert caught.value.status_code == 401
    assert key not in caught.value.detail


@pytest.mark.parametrize("mode", ["open", "anonymous", "spaces"])
@pytest.mark.parametrize("key", ["中文密钥", "a\tb", "a\x7fb"])
def test_invalid_key_is_rejected_before_mode_specific_auth(monkeypatch, mode, key):
    if mode == "spaces":
        monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({"synthetic-good": {"home": "home"}}))
    else:
        monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
        monkeypatch.setenv("MNEMOTRACE_ALLOW_ANONYMOUS", "1" if mode == "anonymous" else "")
    with pytest.raises(HTTPException) as caught:
        appmod.auth_principal(f"Bearer {key}")
    assert caught.value.status_code == 401


@pytest.mark.parametrize("configuration", ["api_keys", "spaces"])
def test_invalid_config_is_503_even_after_a_valid_key_match(monkeypatch, configuration):
    if configuration == "api_keys":
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", "tenant-a:synthetic-good,tenant-b:中文密钥")
    else:
        monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({
            "synthetic-good": {"home": "tenant-a"}, "中文密钥": {"home": "tenant-b"},
        }))
    with pytest.raises(HTTPException) as caught:
        appmod.auth_principal("Bearer synthetic-good")
    assert caught.value.status_code == 503
    assert "synthetic-good" not in caught.value.detail
    assert "中文密钥" not in caught.value.detail


def test_valid_rotation_and_chinese_tenant_names_are_preserved(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "示例租户:synthetic-new,示例租户:synthetic-old,其他租户:synthetic-other")
    for key in ("synthetic-new", "synthetic-old"):
        assert appmod.auth(f"Bearer {key}") == "示例租户"
        assert appmod.auth_principal(f"Bearer {key}").home == "示例租户"
    with pytest.raises(HTTPException) as caught:
        appmod.auth("Bearer synthetic-wrong")
    assert caught.value.status_code == 401


def test_valid_spaces_acl_and_single_space_fallback(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({
        "synthetic-team": {"home": "示例空间", "read": ["共享空间"]},
    }))
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "普通租户:synthetic-single")
    principal = appmod.auth_principal("Bearer synthetic-team")
    assert principal.home == "示例空间"
    assert principal.can_read("共享空间") and principal.can_write("示例空间")
    assert not principal.can_write("共享空间")
    assert appmod.auth_principal("Bearer synthetic-single").home == "普通租户"


def test_successful_match_still_compares_all_configured_keys(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", "tenant-a:synthetic-first,tenant-b:synthetic-last")
    compare = Mock(wraps=appmod.hmac.compare_digest)
    monkeypatch.setattr(appmod.hmac, "compare_digest", compare)
    assert appmod.auth("Bearer synthetic-first") == "tenant-a"
    assert compare.call_count == 2


@pytest.mark.parametrize("token", ["1", "test", "mnemotrace-test", "synthetic-key_+=./: value"])
def test_printable_ascii_namespace_is_unchanged(monkeypatch, token):
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    assert appmod.auth(f"Bearer {token}") == token


def test_anonymous_requires_explicit_enablement(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    with pytest.raises(HTTPException) as caught:
        appmod.auth("")
    assert caught.value.status_code == 401
    monkeypatch.setenv("MNEMOTRACE_ALLOW_ANONYMOUS", "1")
    assert appmod.auth("") == "anonymous"


@pytest.mark.parametrize("configuration", ["api_keys", "spaces"])
def test_invalid_configuration_is_live_but_not_ready(monkeypatch, tmp_path, configuration):
    monkeypatch.setenv("MNEMOTRACE_OPEN", "1")
    if configuration == "api_keys":
        monkeypatch.setenv("MNEMOTRACE_API_KEYS", "示例租户:中文密钥")
    else:
        monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({"中文密钥": {"home": "示例空间"}}))
    service = SimpleNamespace(
        data_dir=str(tmp_path), embedder=object(), llm=None, answerer=None,
        config=SimpleNamespace(storage="memory", max_hot_facts=100), hot_count=0, max_hot_users=10,
    )
    monkeypatch.setattr(appmod, "svc", lambda: service)
    health = appmod.health()
    assert health["ok"] is True and health["ready"] is False
    assert health["auth_mode"] == "invalid"
    assert appmod.ready().status_code == 503
    assert "中文密钥" not in json.dumps(health, ensure_ascii=False)
