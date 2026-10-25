"""Graph limits apply to matching relations; lookahead must not leak nodes."""
import pytest

from mnemotrace import Memory


def add_relation(mem, index, *, historical=False, sensitive=False):
    fact = mem.add_fact(
        "synthetic_user", f"synthetic_link_{index}", f"synthetic_object_{index}",
        valid_at=1_700_000_000.0, sensitive=sensitive,
    )
    if historical:
        fact.invalid_at = 1_700_000_100.0
        mem.graph.invalidate_relations_for_fact(fact.id, fact.invalid_at)
    return fact.id


@pytest.mark.parametrize("count", [0, 119, 120, 121])
def test_graph_has_more_uses_an_extra_matching_relation(count):
    mem = Memory()
    ids = [add_relation(mem, i) for i in range(count)]
    graph = mem.graph_data(limit=120)
    assert [edge["fact_id"] for edge in graph["edges"]] == ids[:120]
    assert graph["has_more"] is (count > 120)
    assert {node["id"] for node in graph["nodes"]} == {
        endpoint for edge in graph["edges"] for endpoint in (edge["source"], edge["target"])
    }
    assert mem.graph_data()["has_more"] is False


@pytest.mark.parametrize("interleaved", [False, True])
def test_current_graph_limit_is_not_consumed_by_history(interleaved):
    mem = Memory()
    current = []
    for i in range(121):
        add_relation(mem, f"old_{i}", historical=True)
        if interleaved:
            current.append(add_relation(mem, f"current_{i}"))
    if not interleaved:
        current.append(add_relation(mem, "current"))
    graph = mem.graph_data(live_only=True, limit=120)
    assert [edge["fact_id"] for edge in graph["edges"]] == current[:120]
    assert all(edge["live"] for edge in graph["edges"])
    assert graph["has_more"] is (len(current) > 120)


def test_graph_lookahead_obeys_search_privacy_time_and_namespace():
    mem = Memory()
    match = add_relation(mem, "match")
    for i in range(121):
        add_relation(mem, f"sensitive_{i}", sensitive=True)
        add_relation(mem, f"old_{i}", historical=True)
    mem.add_fact("other_user", "synthetic_link", "other_object", user_id="other")
    for params in [
        {"q": "synthetic_object_match"},
        {"live_only": True, "include_sensitive": False},
        {"as_of": 1_700_000_200.0, "include_sensitive": False},
    ]:
        graph = mem.graph_data(limit=1, **params)
        assert [edge["fact_id"] for edge in graph["edges"]] == [match]
        assert graph["has_more"] is False
        assert {node["name"] for node in graph["nodes"]} == {
            "synthetic_user", "synthetic_object_match",
        }
