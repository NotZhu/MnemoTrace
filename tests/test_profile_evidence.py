"""画像来源数量应独立于用户设定和访问强化状态。"""
from __future__ import annotations

import pytest

from mnemotrace import Memory
from mnemotrace.consolidate.structured import build_structured_profile
from mnemotrace.types import Fact


def _preference(profile: dict) -> dict:
    return next(item for items in profile["preferences"].values() for item in items)


def test_content_edit_retains_source_count_and_marks_user():
    mem = Memory()
    episode = mem.add("I like jazz.", user_id="synthetic-user")
    mem.consolidate()
    fact = next(f for f in mem._all_facts() if episode.id in f.provenance)
    before = _preference(mem.structured_profile("synthetic-user"))["evidence"]
    assert before["kind"] == "mentions" and before["source_count"] == 1

    mem.update_fact(fact.id, object="classical music")

    after = _preference(mem.structured_profile("synthetic-user"))["evidence"]
    assert after == {"kind": "user", "source_count": 1, "reinforcement_count": 0}
    assert fact.provenance == [episode.id]


@pytest.mark.parametrize("source", ["extracted", "user"])
@pytest.mark.parametrize("access_count", [0, 7])
def test_source_count_counts_distinct_records(source, access_count):
    fact = Fact(subject="user", predicate="prefers", object="synthetic-option",
                source=source, provenance=["synthetic-a", "synthetic-b", "synthetic-a"],
                access_count=access_count)
    evidence = _preference(build_structured_profile([fact], "user"))["evidence"]
    assert evidence == {
        "kind": "user" if source == "user" else "mentions",
        "source_count": 2, "reinforcement_count": access_count,
    }
    assert fact.provenance == ["synthetic-a", "synthetic-b", "synthetic-a"]
    assert fact.access_count == access_count


@pytest.mark.parametrize("provenance", [[], ["synthetic-a"]])
def test_reinforcement_does_not_inflate_source_count(provenance):
    fact = Fact(subject="user", predicate="prefers", object="synthetic-option",
                provenance=provenance.copy(), access_count=7)
    evidence = _preference(build_structured_profile([fact], "user"))["evidence"]
    assert evidence == {
        "kind": "reinforced", "source_count": len(provenance), "reinforcement_count": 7,
    }
    assert fact.provenance == provenance
    assert fact.access_count == 7


def test_manual_fact_without_sources_does_not_invent_count():
    mem = Memory()
    fact = mem.add_fact("user", "prefers", "synthetic-option", user_id="synthetic-user")
    evidence = _preference(mem.structured_profile("synthetic-user"))["evidence"]
    assert evidence == {"kind": "user", "source_count": 0, "reinforcement_count": 0}
    assert fact.provenance == []


def test_resolved_name_without_fact_has_no_direct_source_count():
    profile = build_structured_profile([], subject="Synthetic Person")
    assert profile["basic"][0]["evidence"] == {
        "kind": "mentions", "source_count": 0, "reinforcement_count": 0,
    }


@pytest.mark.parametrize("predicate, section", [
    ("occupation", "basic"), ("likes", "preferences"), ("habit", "habits"),
])
@pytest.mark.parametrize("source", ["user", "extracted"])
def test_reinforcement_count_is_available_in_all_profile_sections(predicate, section, source):
    fact = Fact("user", predicate, "synthetic-value", source=source, access_count=11)

    profile = build_structured_profile([fact], "user")
    item = _preference(profile) if section == "preferences" else profile[section][0]

    assert item["evidence"] == {
        "kind": "user" if source == "user" else "reinforced",
        "source_count": 0, "reinforcement_count": 11,
    }
    assert fact.access_count == 11
    assert fact.provenance == []
