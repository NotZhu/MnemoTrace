"""敏感标记和分类编辑不得改变事实来源、有效状态或替代历史。"""
from __future__ import annotations

import pytest

from tests.model_fixtures import MemoryEngine


@pytest.mark.parametrize("patch", [{"sensitive": True}, {"categories": ["technology"]}])
@pytest.mark.parametrize("resubmit_claim", [False, True])
def test_metadata_update_preserves_superseded_fact(patch, resubmit_claim):
    mem = MemoryEngine()
    old = mem.add_fact("demo_user", "lives_in", "北京", valid_at=1, object_kind="entity", text='demo_user lives in 北京')
    current = mem.add_fact("demo_user", "lives_in", "上海", valid_at=2, cardinality="single", object_kind="entity", text='demo_user lives in 上海')
    assert not old.is_live() and current.supersedes == old.id
    temporal = (old.valid_at, old.invalid_at, old.created_at, old.expired_at, old.supersedes)
    relations = [(r.fact_id, r.valid_at, r.invalid_at) for r in mem.graph.relations()]
    if resubmit_claim:
        # 控制台保存整个表单，即使只切换敏感开关，三个内容字段也会随请求发送。
        patch = {**patch, "subject": old.subject, "predicate": old.predicate, "object": old.object, "text": old.text}

    updated = mem.update_fact(old.id, **patch)

    assert updated is old
    assert not old.is_live(), "metadata-only edits must not reactivate a historical fact"
    assert (old.valid_at, old.invalid_at, old.created_at, old.expired_at, old.supersedes) == temporal
    assert current.is_live() and current.supersedes == old.id
    assert [(r.fact_id, r.valid_at, r.invalid_at) for r in mem.graph.relations()] == relations
    for field in ("sensitive", "categories"):
        if field in patch:
            assert getattr(old, field) == patch[field]


def test_metadata_update_preserves_active_fact_content_and_classification():
    mem = MemoryEngine()
    fact = mem.add_fact("demo_user", "lives_in", "北京", categories=["personal_details"], sensitive=True, text='demo_user lives in 北京')
    fact.text = "A synthetic source statement with additional context."
    fact.provenance = ["synthetic_episode"]
    content = (fact.text, fact.embedding, list(fact.provenance), fact.categories)

    mem.update_fact(fact.id, subject=fact.subject, predicate=fact.predicate, object=fact.object, sensitive=False, text=fact.text)

    assert fact.is_live() and not fact.sensitive
    assert (fact.text, fact.embedding, fact.provenance, fact.categories) == content


def test_changed_claim_retains_existing_edit_behavior():
    mem = MemoryEngine()
    fact = mem.add_fact("demo_user", "lives_in", "北京", object_kind="entity", text='demo_user lives in 北京')
    mem.update_fact(fact.id, object="上海", object_kind="entity", subject=fact.subject, predicate=fact.predicate, text=' '.join((fact.subject, fact.predicate.replace('_', ' '), '上海')))
    assert fact.object == "上海" and "上海" in fact.text
    assert fact.is_live() and fact.source == "user"
    assert "上海" in str(mem.graph_data("default"))


@pytest.mark.parametrize("source", ["extracted", "user"])
@pytest.mark.parametrize("resubmit_claim", [False, True])
@pytest.mark.parametrize("operation", ["sensitive", "categories", "noop"])
def test_metadata_save_preserves_fact_source(source, resubmit_claim, operation):
    mem = MemoryEngine()
    if source == "extracted":
        mem.add("I live in Beijing.", user_id="demo_user", session_id="synthetic-source")
        mem.consolidate()
        fact = mem._all_facts()[0]
    else:
        fact = mem.add_fact("demo_user", "lives_in", "Beijing", user_id="demo_user", text='demo_user lives in Beijing')
    assert fact.source == source
    provenance = list(fact.provenance)
    claim = {field: getattr(fact, field) for field in ("subject", "predicate", "object", "text")}
    patches = {
        "sensitive": [{"sensitive": True}, {"sensitive": False}],
        "categories": [{"categories": ["technology"]}],
        "noop": [{}],
    }[operation]

    for patch in patches:
        updated = mem.update_fact(fact.id, **({**claim, **patch} if resubmit_claim else patch))
        assert updated is fact
        assert fact.source == source, "metadata saves must not change fact ownership"
        assert fact.provenance == provenance
        for field, value in patch.items():
            assert getattr(fact, field) == value


@pytest.mark.parametrize("patch", [{"subject": "demo_alias"}, {"predicate": "based_in"}, {"object": "Shanghai"}])
def test_content_edit_marks_extracted_fact_as_user_and_keeps_provenance(patch):
    mem = MemoryEngine()
    mem.add("I live in Beijing.", user_id="demo_user", session_id="synthetic-source")
    mem.consolidate()
    fact = mem._all_facts()[0]
    assert fact.source == "extracted" and fact.provenance
    provenance = list(fact.provenance)

    claim = {field: getattr(fact, field) for field in ("subject", "predicate", "object")}
    mem.update_fact(fact.id, **{**claim, **patch, "text": "Updated synthetic fact."})

    assert fact.source == "user"
    assert fact.provenance == provenance
    for field, value in patch.items():
        assert getattr(fact, field) == value
