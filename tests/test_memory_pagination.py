"""Browse complete filtered collections without mixing committed versions."""
import pytest

from mnemotrace.service import MemoryService


def service(path):
    return MemoryService(data_dir=str(path), embedder_name="hashing", llm_name="")


def seed(svc, count=601, episodes=241):
    with svc.write_lock("synthetic-user"):
        mem = svc.get("synthetic-user")
        for i in range(count):
            mem.add_fact("synthetic-session", "decision" if i else "lesson", f"Synthetic marker {i}",
                         user_id="synthetic-user", valid_at=100, sensitive=False, category="synthetic")
        for i in range(episodes):
            mem.add(f"Synthetic episode {i}", user_id="synthetic-user", event_time=100)
        svc._save("synthetic-user", mem)
    return mem


def traverse(svc, collection, limit):
    offset, revision, evaluated_at, ids = 0, None, None, []
    while True:
        params = {"facts_limit": 0, "episodes_limit": 0, "include_sensitive": True,
                  f"{collection}_limit": limit, f"{collection}_offset": offset}
        if revision is not None:
            params["expected_revision"] = revision
            params["evaluated_at"] = evaluated_at
        response = svc.memories("synthetic-user", **params)
        revision = response["data_revision"]
        evaluated_at = response["evaluated_at"]
        page = response[f"{collection}_page"]
        ids.extend(row["id"] for row in response[collection])
        if not page["has_more"]:
            assert page["next_offset"] is None
            assert len(ids) == page["total"]
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(set(ids)) == len(ids)
    return ids


def test_complete_collections_beyond_caps_and_equal_timestamps(tmp_path):
    svc = service(tmp_path)
    mem = seed(svc)
    facts = traverse(svc, "facts", 60)
    episodes = traverse(svc, "episodes", 20)
    assert facts == sorted((f.id for f in mem.fact_store.values()), reverse=True)
    assert episodes == sorted((e.id for e in mem.episodes_doc.values()), reverse=True)
    capped = svc.memories("synthetic-user", facts_limit=900, episodes_limit=900, include_sensitive=True)
    assert len(capped["facts"]) == 500 and capped["facts_page"]["limit"] == 500
    assert len(capped["episodes"]) == 200 and capped["episodes_page"]["limit"] == 200
    assert traverse(service(tmp_path), "facts", 500) == facts


def test_outcome_filter_precedes_pagination_and_combines_conditions(tmp_path):
    svc = service(tmp_path)
    seed(svc, count=62, episodes=0)
    rows = svc.memories("synthetic-user", kind="outcomes", outcome_kind="lesson", facts_limit=1,
                        status="live", q="marker", category="synthetic", episodes_limit=0)
    assert len(rows["facts"]) == 1 and rows["facts"][0]["predicate"] == "lesson"
    assert rows["facts_page"]["total"] == 1 and not rows["facts_page"]["has_more"]
    assert rows["counts"]["facts_outcomes"] == 62
    assert svc.memories("synthetic-user", outcome_kind="lesson", q="absent")["facts_page"]["total"] == 0
    with pytest.raises(ValueError, match="outcome_kind"):
        svc.memories("synthetic-user", outcome_kind="invalid")


@pytest.mark.parametrize("operation", ["add", "update", "delete", "forget"])
def test_revision_change_between_service_instances_rejects_next_page(tmp_path, operation):
    reader, writer = service(tmp_path), service(tmp_path)
    mem = seed(writer, count=3, episodes=0)
    first = reader.memories("synthetic-user", facts_limit=1)
    fact_id = mem.fact_store.values()[0].id
    if operation == "add":
        writer.add_fact("synthetic-user", "synthetic-session", "finding", "Synthetic addition")
    elif operation == "update":
        writer.update_fact("synthetic-user", fact_id, object="Synthetic edit")
    elif operation == "delete":
        writer.delete_fact("synthetic-user", fact_id)
    else:
        writer.forget("synthetic-user")
    from mnemotrace.service import MemoryRevisionChanged
    with pytest.raises(MemoryRevisionChanged):
        reader.memories("synthetic-user", facts_offset=1, facts_limit=1, expected_revision=first["data_revision"], evaluated_at=first["evaluated_at"])
    new = reader.memories("synthetic-user", facts_limit=1)
    assert new["data_revision"] != first["data_revision"]
    traverse(reader, "facts", 1)


def test_empty_revision_restart_and_other_namespace_writes(tmp_path):
    reader, writer = service(tmp_path), service(tmp_path)
    empty = reader.memories("synthetic-user", facts_limit=1)
    assert empty["facts_page"]["total"] == 0 and empty["data_revision"]
    assert service(tmp_path).memories("synthetic-user")["data_revision"] == empty["data_revision"]
    writer.add_fact("synthetic-other", "synthetic", "marker", "Synthetic other space")
    assert reader.memories("synthetic-user", expected_revision=empty["data_revision"], evaluated_at=empty["evaluated_at"])["facts"] == []
    writer.add_fact("synthetic-user", "synthetic", "marker", "Synthetic first entry")
    from mnemotrace.service import MemoryRevisionChanged
    with pytest.raises(MemoryRevisionChanged):
        reader.memories("synthetic-user", expected_revision=empty["data_revision"], evaluated_at=empty["evaluated_at"])


def test_interleaved_outcomes_match_search_status_category_and_sensitive_scope(tmp_path):
    svc = service(tmp_path)
    with svc.write_lock("synthetic-user"):
        mem = svc.get("synthetic-user")
        for i in range(20):
            fact = mem.add_fact(f"synthetic-session-{i}", "lesson" if i % 2 == 0 else "decision",
                                f"Synthetic {'needle' if i % 4 == 0 else 'other'} {i}",
                                user_id="synthetic-user", valid_at=100, sensitive=i == 4,
                                category="excluded" if i == 8 else "synthetic")
            if i == 12:
                fact.invalid_at = 200
        svc._save("synthetic-user", mem)
    params = dict(kind="outcomes", outcome_kind="lesson", q="needle", category="synthetic",
                  facts_limit=1, episodes_limit=0, include_sensitive=False)
    first = svc.memories("synthetic-user", **params, status="live")
    second = svc.memories("synthetic-user", **params, status="live", facts_offset=first["facts_page"]["next_offset"],
                          expected_revision=first["data_revision"], evaluated_at=first["evaluated_at"])
    assert {row["object"] for page in (first, second) for row in page["facts"]} == {"Synthetic needle 0", "Synthetic needle 16"}
    assert first["facts_page"]["total"] == 2 and not second["facts_page"]["has_more"]
    history = svc.memories("synthetic-user", **params, status="superseded")
    assert [row["object"] for row in history["facts"]] == ["Synthetic needle 12"]
    assert svc.memories("synthetic-user", **params)["facts_page"]["total"] == 3
    assert svc.memories("synthetic-user", **{**params, "include_sensitive": True})["facts_page"]["total"] == 4


def seed_temporal(svc):
    with svc.write_lock("synthetic-user"):
        mem = svc.get("synthetic-user")
        for name, valid in [("upcoming", 1100), ("invalidating", 900), ("expiring", 800), ("older", 700)]:
            fact = mem.add_fact(name, "decision", name, user_id="synthetic-user", valid_at=valid)
            if name == "invalidating":
                fact.invalid_at = 1100
            if name == "expiring":
                fact.expired_at = 1100
        svc._save("synthetic-user", mem)


def test_time_transitions_keep_the_original_collection_status_and_counts(tmp_path, monkeypatch):
    svc = service(tmp_path)
    seed_temporal(svc)
    clock = [1000]
    monkeypatch.setattr("mnemotrace.util.time.time", lambda: clock[0])
    first = svc.memories("synthetic-user", status="live", facts_limit=1)
    assert first["evaluated_at"] == 1000
    clock[0] = 1200
    pages = [first]
    while pages[-1]["facts_page"]["has_more"]:
        pages.append(svc.memories("synthetic-user", status="live", facts_limit=1,
                     facts_offset=pages[-1]["facts_page"]["next_offset"],
                     expected_revision=first["data_revision"], evaluated_at=first["evaluated_at"]))
    assert [f["object"] for page in pages for f in page["facts"]] == ["invalidating", "expiring", "older"]
    assert len({f["id"] for page in pages for f in page["facts"]}) == 3
    for page in pages:
        assert page["data_revision"] == first["data_revision"]
        assert page["evaluated_at"] == 1000 and page["facts_page"]["total"] == 3
        assert all(f["status"] == "live" for f in page["facts"])
        assert {k: page["counts"][k] for k in ("facts_live", "facts_superseded", "facts_outcomes")} == {
            "facts_live": 3, "facts_superseded": 1, "facts_outcomes": 3}
    frozen_history = svc.memories("synthetic-user", status="superseded",
                                 expected_revision=first["data_revision"], evaluated_at=1000)
    assert [f["object"] for f in frozen_history["facts"]] == ["upcoming"]
    refreshed = svc.memories("synthetic-user", status="live")
    assert refreshed["evaluated_at"] == 1200
    assert [f["object"] for f in refreshed["facts"]] == ["upcoming", "older"]
    assert refreshed["counts"]["facts_live"] == 2 and refreshed["counts"]["facts_superseded"] == 2


def test_one_response_uses_the_captured_time_for_rows_and_each_statistic(tmp_path, monkeypatch):
    svc = service(tmp_path)
    seed_temporal(svc)
    monkeypatch.setattr("mnemotrace.util.time.time", lambda: 1000)
    # A later wall-clock read inside Fact would disagree with the captured request time.
    monkeypatch.setattr("mnemotrace.types.now", lambda: 1200)
    response = svc.memories("synthetic-user")
    assert response["evaluated_at"] == 1000
    assert {f["object"]: f["status"] for f in response["facts"]} == {
        "upcoming": "superseded", "invalidating": "live", "expiring": "live", "older": "live"}
    assert response["counts"]["facts_live"] == 3
    assert response["counts"]["facts_superseded"] == 1
    assert response["counts"]["facts_outcomes"] == 3


@pytest.mark.parametrize("params", [
    {"expected_revision": "empty"}, {"evaluated_at": 1000},
    *[{"expected_revision": "empty", "evaluated_at": value}
      for value in [-1, float("nan"), float("inf"), float("-inf")]],
])
def test_invalid_browse_context_is_rejected(tmp_path, params):
    with pytest.raises(ValueError, match="evaluated_at"):
        service(tmp_path).memories("synthetic-user", **params)
