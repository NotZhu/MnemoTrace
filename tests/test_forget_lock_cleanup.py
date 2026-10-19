from __future__ import annotations

from pathlib import Path

import pytest

from mnemotrace import Memory
from mnemotrace.service import MemoryService


@pytest.mark.parametrize("include_pickle_directory", [False, True])
def test_forget_removes_digest_store_lock_and_preserves_legacy_directories(
    tmp_path, include_pickle_directory
):
    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    user = "synthetic-user"
    secure = Path(service._secure_path(user))
    legacy = [tmp_path / user]
    if include_pickle_directory:
        legacy.append(tmp_path / f"{user}.pkl")

    # 清空仅处理摘要目录中的旧服务锁，不接管旧名字的目录或其内容。
    for directory in [secure, *legacy]:
        memory = Memory.open(str(directory))
        memory.add_fact("project", "marker", "before-forget", user_id=user)
        memory.save()
        (directory / ".service.lock").touch()
    assert all((directory / ".service.lock").is_file() for directory in [secure, *legacy])
    before = {
        directory: {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
        for directory in legacy
    }

    other_user = "synthetic-other-user"
    service.add_fact(other_user, "project", "marker", "keep-other-namespace")
    assert service.memories(user)["facts"]

    assert service.forget(user)["ok"] is True

    assert not secure.exists()
    for directory in legacy:
        assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == before[directory]
    reloaded = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    assert reloaded.memories(user)["facts"] == []
    assert [fact["object"] for fact in reloaded.memories(other_user)["facts"]] == [
        "keep-other-namespace"
    ]
