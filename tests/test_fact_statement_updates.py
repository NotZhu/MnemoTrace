"""整句编辑只适用于会话结论；属性修正必须保留三元组语义。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from tests.model_fixtures import Memory
from mnemotrace.localize import display_of


def test_structured_attribute_edit_preserves_values_graph_and_profile_on_reload(tmp_path):
    mem = Memory()
    mem.add("I live in Beijing.", user_id="synthetic_user", session_id="synthetic_source")
    mem.consolidate()
    fact = mem._all_facts()[0]
    provenance, slot = list(fact.provenance), fact.slot

    mem.update_fact(fact.id, subject=fact.subject, predicate="lives_in", object="Shanghai")

    assert fact.object == "Shanghai"
    assert fact.slot == slot and fact.source == "user" and fact.provenance == provenance
    assert fact.embedding == mem.embedder.embed(fact.text)
    mem.save(str(tmp_path / "snapshot"))
    restored = Memory.open(str(tmp_path / "snapshot"))
    loaded = restored.fact_store.get(fact.id)
    assert loaded.object == "Shanghai"
    assert loaded.provenance == provenance
    assert next(row for row in restored.structured_profile("synthetic_user")["basic"]
                if row["field"] == "home")["value"] == "Shanghai"
    names = {node["name"] for node in restored.graph_data("synthetic_user")["nodes"]}
    assert "Shanghai" in names and "Beijing" not in names and loaded.text not in names


@pytest.mark.parametrize("statement", ["演示用户住在上海。", None])
def test_attribute_statement_edit_is_rejected_without_mutating_fact_or_graph(statement):
    mem = Memory()
    mem.add("I live in Beijing.", user_id="synthetic_user", session_id="synthetic_source")
    mem.consolidate()
    fact = mem._all_facts()[0]
    before = deepcopy(fact.__dict__)
    graph = deepcopy(mem.graph_data("synthetic_user"))
    with pytest.raises(ValueError, match="statement.*session outcome"):
        mem.update_fact(fact.id, statement=display_of(fact) if statement is None else statement,
                        sensitive=True, category="不应保存")
    assert fact.__dict__ == before
    assert mem.graph_data("synthetic_user") == graph


def test_outcome_statement_noop_does_not_reactivate_history_or_change_source():
    mem = Memory()
    old = mem.add_fact("synthetic_session", "lesson", "先检查配置", valid_at=1)
    old.invalid_at = old.expired_at = 2
    old.source = "extracted"
    before = deepcopy(old.__dict__)
    mem.update_fact(old.id, statement=display_of(old))
    assert old.__dict__ == before and not old.is_live()


@pytest.mark.parametrize("patch", [{"statement": " "}, {"statement": "新断言", "object": "取值"}])
def test_invalid_statement_update_leaves_fact_and_graph_unchanged(patch):
    mem = Memory()
    fact = mem.add_fact("synthetic_user", "likes", "阅读")
    before = dict(fact.__dict__)
    graph = mem.graph_data("default")
    with pytest.raises(ValueError):
        mem.update_fact(fact.id, **patch)
    assert fact.__dict__ == before
    assert mem.graph_data("default") == graph


@pytest.mark.parametrize("predicate", ["decision", "finding", "lesson", "open_question"])
def test_statement_edit_retains_outcome_evidence_and_roundtrips(tmp_path, predicate):
    mem = Memory()
    fact = mem.add_fact("synthetic_session", predicate, "先检查配置")
    fact.provenance = ["synthetic_episode"]
    fact.text = "先检查配置 （依据：错误源于缺少配置）"
    mem.update_fact(fact.id, statement="排查连接失败时先检查配置。")
    assert fact.text == "排查连接失败时先检查配置。 （依据：错误源于缺少配置）"
    assert display_of(fact) == fact.object == "排查连接失败时先检查配置。"
    mem.save(str(tmp_path / "snapshot"))
    loaded = Memory.open(str(tmp_path / "snapshot")).fact_store.get(fact.id)
    assert loaded.object == fact.object and loaded.text == fact.text
    assert loaded.slot == fact.slot and loaded.provenance == fact.provenance


def test_http_edit_route_maps_attribute_statement_rejection_to_422(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from mnemotrace.server import app as appmod
    from tests.model_fixtures import MemoryService

    service = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    created = service.add_fact("synthetic_user", "user", "lives_in", "Beijing")
    monkeypatch.setattr(appmod, "svc", lambda: service)
    with pytest.raises(HTTPException) as error:
        appmod.edit_fact(created["id"], appmod.FactEdit(statement="用户住在上海。"), "synthetic_user")
    assert error.value.status_code == 422
    service._hot.clear()
    assert service.get("synthetic_user").fact_store.get(created["id"]).object == "Beijing"
    result = appmod.edit_fact(created["id"], appmod.FactEdit(object="Shanghai"), "synthetic_user")
    assert result["ok"] and "Shanghai" in result["text"]
    with pytest.raises(HTTPException) as missing:
        appmod.edit_fact("missing", appmod.FactEdit(statement="新结论"), "synthetic_user")
    assert missing.value.status_code == 404
