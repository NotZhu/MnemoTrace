"""多空间（多 agent / 团队共享）记忆测试（AGENTS.md 「代码导航」）。覆盖 MNEMOTRACE_SPACES 的 ACL 解析、service
层的跨空间读融合，以及 HTTP 层的写入/读取授权。单空间鉴权（MNEMOTRACE_API_KEYS / open 模式）刻意
不做改动，仍由 test_server 覆盖。全部离线（hashing 嵌入器、固定合成模型响应）；环境变量用 monkeypatch，
会自动还原。"""
from __future__ import annotations

import json
import shutil
import tempfile

from tests.model_fixtures import MemoryService
from mnemotrace.spaces import Principal, principal_for_token


# --- ACL 解析 ---------------------------------------------------------------


def test_spaces_config_defaults_write_to_home(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SPACES", json.dumps({"sk": {"home": "h", "read": ["h", "t"]}}))
    p = principal_for_token("sk")
    assert p is not None and p.home == "h"
    assert p.can_read("h") and p.can_read("t")
    assert p.can_write("h") and not p.can_write("t")  # 未写 write -> 默认为 [home]
    assert principal_for_token("other") is None  # 未知 token -> 不是 spaces 主体


def test_malformed_spaces_is_ignored(monkeypatch):
    monkeypatch.setenv("MNEMOTRACE_SPACES", "{not valid json")
    assert principal_for_token("whatever") is None  # 配置损坏时绝不能授予访问权


def test_single_principal_shape():
    p = Principal.single("alice")
    assert p.home == "alice" and p.can_read("alice") and p.can_write("alice")
    assert not p.can_read("bob")


# --- service 层的跨空间融合 ----------------------------------------------------


def test_recall_multi_fuses_spaces():
    d = tempfile.mkdtemp(prefix="mnemotrace_sp_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        svc.remember("agentA", "The deploy key rotates every 90 days.")
        svc.remember("team", "Standup is at 10am daily.")
        out = svc.recall_multi(["agentA", "team"], "standup")
        assert out["spaces"] == ["agentA", "team"]
        # 两个空间都有贡献，且各自带有来源标记
        assert "Space: agentA" in out["context"] and "Space: team" in out["context"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- HTTP 写入/读取授权 --------------------------------------------------------
