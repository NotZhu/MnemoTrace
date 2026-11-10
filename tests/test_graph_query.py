"""Graph limits apply to matching relations; lookahead must not leak nodes."""
import pytest

from tests.model_fixtures import MemoryEngine


def add_relation(mem, index, *, historical=False, sensitive=False):
    fact = mem.add_fact(
        "synthetic_user", f"synthetic_link_{index}", f"synthetic_object_{index}",
        valid_at=1_700_000_000.0, sensitive=sensitive, object_kind="entity",
    )
    if historical:
        fact.invalid_at = 1_700_000_100.0
        mem.graph.invalidate_relations_for_fact(fact.id, fact.invalid_at)
    return fact.id


@pytest.mark.parametrize("count", [0, 119, 120, 121])
def test_graph_has_more_uses_an_extra_matching_relation(count):
    mem = MemoryEngine()
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
    mem = MemoryEngine()
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
    mem = MemoryEngine()
    match = add_relation(mem, "match")
    for i in range(121):
        add_relation(mem, f"sensitive_{i}", sensitive=True)
        add_relation(mem, f"old_{i}", historical=True)
    mem.add_fact("other_user", "synthetic_link", "other_object", user_id="other", object_kind="entity")
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


def test_object_kind_edit_updates_projection_without_changing_fact():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "M. & Co. (Lab)", categories=["professional_details"])
    assert fact.object_kind == "unknown" and not mem.graph_data()["edges"]
    text, created_at = fact.text, fact.created_at

    mem.update_fact(fact.id, object_kind="entity")
    assert [edge["fact_id"] for edge in mem.graph_data()["edges"]] == [fact.id]
    assert mem.graph.get_entity("default", "m. & co. (lab)") is not None

    mem.update_fact(fact.id, object_kind="literal")
    assert mem.graph_data() == {"nodes": [], "edges": [], "has_more": False}
    assert (fact.text, fact.created_at, fact.categories) == (text, created_at, ["professional_details"])
    assert mem.search("M. & Co. (Lab)").top().id == fact.id


@pytest.mark.parametrize("value", ["multi\nline", "tab\tvalue", "vertical\u2028separator", "x" * 513])
def test_invalid_graph_name_keeps_fact(value):
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", value, object_kind="entity")
    assert mem.fact_store.get(fact.id).object == value
    assert not mem.graph_data()["edges"]


def test_graph_recall_uses_explicit_names_instead_of_pronouns():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "lives_in", "Synthetic City", object_kind="entity")
    for query in ("Where do I live?", "Where does his sister live?", "我的住址在哪里"):
        assert not mem.retriever.query_entity_ids(query, "default")
    assert mem.search("Where do I live?").top().id == fact.id
    assert mem.retriever.query_entity_ids("Tell me about Synthetic City", "default")


def test_repeated_projection_does_not_duplicate_edges():
    mem = MemoryEngine()
    fact = mem.add_fact("user", "works_at", "Synthetic Lab", object_kind="entity")
    mem.writer.graph_builder.add_fact(fact)
    mem.writer.graph_builder.add_fact(fact)
    assert [edge.fact_id for edge in mem.graph.relations()] == [fact.id]
    owner = mem.graph.get_entity("default", "user")
    assert [edge.fact_id for edge in mem.graph.neighbors(owner.id)] == [fact.id]
