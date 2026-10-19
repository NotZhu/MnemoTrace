"""Only digest directories belong to namespaces; old paths are never adopted or erased."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mnemotrace import Memory
from mnemotrace.service import MemoryService


def service_at(directory):
    return MemoryService(data_dir=str(directory), embedder_name="hashing", llm_name="")


def objects(service, user):
    return {fact["object"] for fact in service.memories(user)["facts"]}


def seed_legacy(directory, user, *, suffix=""):
    memory = Memory.open(str(directory / (user + suffix)))
    memory.add_fact("project", "marker", "SYNTHETIC_OWNER_ONLY", user_id=user)
    memory.save()


@pytest.mark.parametrize("layout", ["secure", "legacy.pkl"])
@pytest.mark.parametrize("other_has_store", [False, True])
def test_reserved_legacy_name_cannot_read_write_or_erase_owner(tmp_path, layout, other_has_store):
    owner = "synthetic-owner"
    service = service_at(tmp_path)
    if layout == "secure":
        service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
        other = service._safe_user(owner)
    else:
        service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
        seed_legacy(tmp_path, owner, suffix=".pkl")
        other = owner + ".pkl"
    if other_has_store:
        memory = Memory.open(service._secure_path(other))
        memory.add_fact("project", "marker", "SYNTHETIC_OTHER_ONLY", user_id=other)
        memory.save()

    owner_path = Path(service._path(owner))
    before = {path.name: path.read_bytes() for path in owner_path.iterdir() if path.is_file()}
    assert objects(service, other) == ({"SYNTHETIC_OTHER_ONLY"} if other_has_store else set())
    assert service._path(owner) != service._path(other)
    service.add_fact(other, "project", "second_marker", "SYNTHETIC_OTHER_WRITE")
    assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}
    assert {path.name: path.read_bytes() for path in owner_path.iterdir() if path.is_file()} == before
    assert service.forget(other)["ok"] is True
    assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}
    assert objects(service_at(tmp_path), other) == set()


@pytest.mark.parametrize("other_has_store", [False, True])
def test_forget_alone_never_removes_reserved_alias(tmp_path, other_has_store):
    service = service_at(tmp_path)
    owner = "synthetic-owner"
    service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
    other = service._safe_user(owner)
    if other_has_store:
        memory = Memory.open(service._secure_path(other))
        memory.add_fact("project", "marker", "SYNTHETIC_OTHER_ONLY", user_id=other)
        memory.save()
    assert service.forget(other)["ok"] is True
    assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}


@pytest.mark.parametrize("other_has_store", [False, True])
def test_legacy_pickle_file_is_not_owned_by_namespace_with_pkl_suffix(tmp_path, other_has_store):
    service = service_at(tmp_path)
    user = "synthetic-owner.pkl"
    old_snapshot = tmp_path / user
    # An inert marker file: neither the service nor the test unpickles it.
    marker = b"SYNTHETIC_LEGACY_SNAPSHOT"
    old_snapshot.write_bytes(marker)
    if other_has_store:
        memory = Memory.open(service._secure_path(user))
        memory.add_fact("project", "marker", "SYNTHETIC_OTHER_ONLY", user_id=user)
        memory.save()
    assert objects(service, user) == ({"SYNTHETIC_OTHER_ONLY"} if other_has_store else set())
    assert service.forget(user)["ok"] is True
    assert old_snapshot.read_bytes() == marker


@pytest.mark.parametrize("same_service", [True, False])
def test_alias_operations_do_not_modify_owner_while_owner_is_locked(tmp_path, same_service):
    service = service_at(tmp_path)
    owner = "synthetic-owner"
    service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
    other = service._safe_user(owner)
    other_service = service if same_service else service_at(tmp_path)

    def operate_on_other():
        assert objects(other_service, other) == set()
        other_service.add_fact(other, "project", "marker", "SYNTHETIC_OTHER_ONLY")
        other_service.forget(other)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with service.write_lock(owner):
            executor.submit(operate_on_other).result(timeout=10)
            assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}


@pytest.mark.parametrize("user", [
    "synthetic-owner--0123456789abcdef",
    "SYNTHETIC-OWNER--0123456789ABCDEF",
    "synthetic-owner--0123456789abcdef.pkl",
    "synthetic-owner.pkl",
    "synthetic-owner.PKL",
    "synthetic-owner.pkl.pkl",
])
def test_names_only_use_their_own_secure_store(tmp_path, user):
    service = service_at(tmp_path)
    assert service._path(user) == service._secure_path(user)


@pytest.mark.parametrize("user", ["synthetic-user", "synthetic.user", "SYNTHETIC-USER", "合成用户"])
@pytest.mark.parametrize("suffix", ["", ".pkl"])
def test_legacy_directories_are_never_read_written_or_erased(tmp_path, user, suffix):
    seed_legacy(tmp_path, user, suffix=suffix)
    legacy = tmp_path / (user + suffix)
    before = {path.name: path.read_bytes() for path in legacy.iterdir() if path.is_file()}
    service = service_at(tmp_path)
    assert objects(service, user) == set()
    assert service._path(user) == service._secure_path(user)
    service.add_fact(user, "project", "marker", "SYNTHETIC_NEW_STORE")
    assert objects(service_at(tmp_path), user) == {"SYNTHETIC_NEW_STORE"}
    assert service.forget(user)["ok"] is True
    assert not Path(service._secure_path(user)).exists()
    assert objects(service_at(tmp_path), user) == set()
    assert {path.name: path.read_bytes() for path in legacy.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("slot", ["secure", "legacy"])
def test_top_level_link_cannot_alias_a_sibling_namespace(tmp_path, slot):
    service = service_at(tmp_path)
    owner, other = "synthetic-owner", "synthetic-other"
    service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
    target = Path(service._path(owner))
    alias = tmp_path / (service._safe_user(other) if slot == "secure" else other)
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(alias))
        else:
            alias.symlink_to(target, target_is_directory=True)
        if slot == "secure":
            with pytest.raises(ValueError, match="namespace path"):
                service.memories(other)
            with pytest.raises(ValueError, match="namespace path"):
                service.forget(other)
        else:
            assert objects(service, other) == set()
            service.add_fact(other, "project", "marker", "SYNTHETIC_OTHER_ONLY")
            assert service.forget(other)["ok"] is True
        assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}
    finally:
        if alias.exists():
            if os.name == "nt":
                alias.rmdir()
            else:
                alias.unlink()


@pytest.mark.parametrize("alias", ["SYNTHETIC-OWNER", "synthetic-owner."])
def test_logical_name_aliases_have_independent_digest_stores(tmp_path, alias):
    owner = "synthetic-owner"
    seed_legacy(tmp_path, owner)
    service = service_at(tmp_path)
    service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
    assert objects(service, alias) == set()
    service.add_fact(alias, "project", "marker", "SYNTHETIC_OTHER_ONLY")
    assert service.forget(alias)["ok"] is True
    assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}


@pytest.mark.parametrize("user", ["synthetic-owner--0123456789abcdef", "synthetic-owner.pkl"])
def test_operator_can_copy_confirmed_reserved_legacy_store_without_changing_source(tmp_path, user):
    seed_legacy(tmp_path, user)
    source = tmp_path / user
    before = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
    service = service_at(tmp_path)
    assert objects(service, user) == set()
    destination = Path(service._secure_path(user))
    assert not destination.exists()
    # Only an operator who has confirmed ownership may explicitly choose this source.
    Memory.open(str(source)).save(str(destination))
    assert objects(service_at(tmp_path), user) == {"SYNTHETIC_OWNER_ONLY"}
    assert {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()} == before


def test_authenticated_http_namespace_alias_stays_isolated(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from mnemotrace.server import app as appmod

    service = service_at(tmp_path)
    owner = "synthetic-owner"
    other = service._safe_user(owner)
    service.add_fact(owner, "project", "marker", "SYNTHETIC_OWNER_ONLY")
    monkeypatch.setattr(appmod, "_svc", service)
    monkeypatch.setenv("MNEMOTRACE_API_KEYS", f"{owner}:synthetic-key-a,{other}:synthetic-key-b")
    client = TestClient(appmod.app)
    headers = {"Authorization": "Bearer synthetic-key-b"}
    try:
        response = client.get("/v1/memories", headers=headers)
        assert response.status_code == 200
        assert response.json()["facts"] == []
        written = client.post("/v1/facts", headers=headers, json={
            "subject": "project", "predicate": "marker", "object": "SYNTHETIC_OTHER_ONLY",
        })
        assert written.status_code == 200
        assert client.post("/v1/forget", headers=headers, json={"confirm": True}).status_code == 200
        assert objects(service_at(tmp_path), owner) == {"SYNTHETIC_OWNER_ONLY"}
        owner_response = client.get("/v1/memories", headers={"Authorization": "Bearer synthetic-key-a"})
        assert owner_response.status_code == 200
        assert {fact["object"] for fact in owner_response.json()["facts"]} == {"SYNTHETIC_OWNER_ONLY"}
    finally:
        client.close()
