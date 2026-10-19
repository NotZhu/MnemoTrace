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
    assert after == {"kind": "user", "count": 1, "source_count": 1}
    assert fact.provenance == [episode.id]


@pytest.mark.parametrize("source", ["extracted", "user"])
def test_source_count_counts_distinct_records(source):
    fact = Fact(subject="user", predicate="prefers", object="synthetic-option",
                source=source, provenance=["synthetic-a", "synthetic-b", "synthetic-a"])
    evidence = _preference(build_structured_profile([fact], "user"))["evidence"]
    assert evidence["source_count"] == 2
    assert evidence["kind"] == ("user" if source == "user" else "mentions")
    assert evidence["count"] == (1 if source == "user" else 2)


def test_reinforcement_does_not_inflate_source_count():
    fact = Fact(subject="user", predicate="prefers", object="synthetic-option",
                provenance=["synthetic-a"], access_count=7)
    evidence = _preference(build_structured_profile([fact], "user"))["evidence"]
    assert evidence == {"kind": "reinforced", "count": 7, "source_count": 1}


def test_manual_fact_without_sources_does_not_invent_count():
    mem = Memory()
    fact = mem.add_fact("user", "prefers", "synthetic-option", user_id="synthetic-user")
    evidence = _preference(mem.structured_profile("synthetic-user"))["evidence"]
    assert evidence == {"kind": "user", "count": 1, "source_count": 0}
    assert fact.provenance == []


def test_resolved_name_without_fact_has_no_direct_source_count():
    profile = build_structured_profile([], subject="Synthetic Person")
    assert profile["basic"][0]["evidence"] == {
        "kind": "mentions", "count": 1, "source_count": 0,
    }
