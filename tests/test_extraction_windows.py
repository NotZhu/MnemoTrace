"""Native window, provenance and retry contracts; no legacy fixture adapters."""
import json

import pytest

from mnemotrace import Config, ExtractionResponseError, ExtractionTruncatedError, Memory
from mnemotrace.consolidate.processing import PROGRESS_KEY
from mnemotrace.consolidate.windows import WINDOW_PREFIX, ExtractionWindow, SourceSpan
from mnemotrace.llm import Completion, FakeLLM
from mnemotrace.store import StoreFormatError


def payload(prompt):
    return json.JSONDecoder().raw_decode(prompt.split(WINDOW_PREFIX, 1)[1])[0]


def empty(*args):
    return '{"facts":[],"working":[]}'


def rows(data):
    return [{"subject": "user", "predicate": "owns", "object": span["content"],
             "text": span["content"], "source_ids": [span["id"]]} for span in data["pending"]]


def model_for_rows():
    return FakeLLM(handler=lambda prompt, system: Completion(
        json.dumps({"facts": rows(payload(prompt)), "working": []}), "stop",
        {"prompt_tokens": 100 + 4 * len(payload(prompt)["pending"]),
         "completion_tokens": 5 * len(payload(prompt)["pending"])}))


def test_batch_uses_one_call_preserves_all_sources_and_source_times():
    model = model_for_rows()
    mem = Memory(llm=model)
    eps = [mem.add(f"Synthetic item {i}", user_id="u", session_id="s", event_time=i + 1) for i in range(6)]
    stats = mem.consolidate()
    assert stats["model_calls"] == stats["windows_completed"] == len(model.calls) == 1
    assert len(mem.fact_store.values()) == 6 and not mem.ingestor.pending()
    for ep in eps:
        stored = next(f for f in mem.fact_store.values() if f.object == ep.content)
        assert stored.provenance == [ep.id] and stored.valid_at == ep.event_time
        progress = ep.metadata[PROGRESS_KEY]
        assert progress["completed_through"] == len(ep.content)
        assert progress["windows"][0]["usage"] == {"prompt_tokens": 124, "completion_tokens": 30}
    immediate_model = model_for_rows()
    immediate = Memory(llm=immediate_model)
    for i in range(6):
        immediate.add(f"Synthetic item {i}", user_id="u", session_id="s", event_time=i + 1)
        immediate.consolidate()
    assert len(immediate_model.calls) == 6
    assert {f.object for f in immediate.fact_store.values()} == {f.object for f in mem.fact_store.values()}


def test_context_is_bounded_and_isolated_and_not_marked_complete():
    model = FakeLLM(handler=empty)
    mem = Memory(llm=model, config=Config(extraction_context_bytes=18))
    other = mem.add("OTHER_USER_SECRET", user_id="other", session_id="s", event_time=1)
    other_session = mem.add("OTHER_SESSION_SECRET", user_id="u", session_id="other", event_time=1)
    context = mem.add("前文" * 50, user_id="u", session_id="s", event_time=1)
    current = mem.add("新增", user_id="u", session_id="s", event_time=2)
    mem.consolidate([current])
    window = payload(model.calls[0]["prompt"])
    assert {s["episode_id"] for s in window["context"]} == {context.id}
    assert sum(len(s["content"].encode("utf-8")) for s in window["context"]) <= 18
    assert "SECRET" not in model.calls[0]["prompt"]
    assert not context.consolidated and not other.consolidated and not other_session.consolidated
    assert PROGRESS_KEY not in context.metadata and current.consolidated


def test_large_unicode_source_is_split_losslessly_with_previous_segment_context():
    model = FakeLLM(handler=empty)
    mem = Memory(llm=model, config=Config(extraction_context_bytes=32))
    text = "原始🙂内容\n" * 500
    ep = mem.add(text, session_id="s")
    overhead = mem.engine.extractor.window_size(ExtractionWindow((SourceSpan(ep, 0, 1),)))
    mem.config.extraction_max_input_bytes = overhead + 350
    mem.consolidate()
    windows = [payload(c["prompt"]) for c in model.calls]
    assert len(windows) > 1
    assert "".join(w["pending"][0]["content"] for w in windows) == text == ep.content
    assert windows[1]["context"] and windows[1]["context"][-1]["episode_id"] == ep.id
    assert all(len((c["prompt"] + c["system"]).encode("utf-8")) <= mem.config.extraction_max_input_bytes
               for c in model.calls)
    ranges = ep.metadata[PROGRESS_KEY]["windows"]
    assert ranges[0]["start"] == 0 and ranges[-1]["end"] == len(text)
    assert all(a["end"] == b["start"] for a, b in zip(ranges, ranges[1:]))
    assert all(r["usage"] is None and r["finish_reason"] is None for r in ranges)


@pytest.mark.parametrize("sources", ["missing", "unknown", "context", "invalid_type"])
def test_invalid_source_is_rejected_before_any_derived_state(sources):
    def reply(prompt, system):
        data = payload(prompt)
        result = rows(data)
        if sources == "missing":
            for row in result:
                row.pop("source_ids")
        elif sources == "unknown":
            result[0]["source_ids"] = ["not-an-input-source"]
        elif sources == "context":
            result[0]["source_ids"] = [data["context"][0]["id"]]
        else:
            result[0]["source_ids"] = "string-is-not-a-list"
        return json.dumps({"facts": result, "working": []})
    mem = Memory(llm=FakeLLM(handler=reply))
    previous = mem.add("context", session_id="s", event_time=1)
    new = [mem.add(str(i), session_id="s", event_time=i + 2) for i in range(2)]
    with pytest.raises(ExtractionResponseError):
        mem.consolidate(new)
    assert not mem.fact_store.values() and not mem._identity and not mem.working_mem
    assert all(not ep.consolidated and PROGRESS_KEY not in ep.metadata for ep in [previous, *new])


def test_cross_event_reference_can_cite_context_but_must_cite_new_input():
    def reply(prompt, system):
        data = payload(prompt)
        source_ids = [data["context"][0]["id"], data["pending"][0]["id"]]
        return json.dumps({"facts": [{"subject": "user", "predicate": "works_at", "object": "SyntheticCo",
                                      "source_ids": source_ids}], "working": []})
    mem = Memory(llm=FakeLLM(handler=reply))
    prior = mem.add("Synthetic reference", session_id="s", event_time=1)
    current = mem.add("Synthetic continuation", session_id="s", event_time=2)
    mem.consolidate([current])
    fact, = mem.fact_store.values()
    assert fact.provenance == [prior.id, current.id] and fact.valid_at == 2
    assert not prior.consolidated and current.consolidated


@pytest.mark.parametrize("reason", ["length", "max_tokens", "max_output_tokens", "incomplete"])
def test_valid_json_with_truncation_is_failure_and_can_retry(reason):
    model = FakeLLM(responses=[Completion(empty(), reason), Completion(empty(), "stop")])
    mem = Memory(llm=model)
    ep = mem.add("Synthetic input")
    with pytest.raises(ExtractionTruncatedError) as error:
        mem.consolidate()
    assert error.value.code == "extraction_truncated" and error.value.stored_raw
    assert not ep.consolidated and PROGRESS_KEY not in ep.metadata
    assert mem.consolidate()["facts_added"] == 0
    assert ep.consolidated and "extraction_error" not in ep.metadata


def test_restart_only_processes_remaining_ranges_after_checkpoint_failure(tmp_path, monkeypatch):
    model = FakeLLM(handler=empty)
    mem = Memory(llm=model, config=Config(extraction_context_bytes=20))
    ep = mem.add("Unicode🙂" * 180, session_id="s")
    mem.config.extraction_max_input_bytes = mem.engine.extractor.window_size(
        ExtractionWindow((SourceSpan(ep, 0, 1),))) + 200
    original_write = mem.engine.processing.write
    attempts = []
    def interrupted(updated):
        attempts.append(updated.metadata[PROGRESS_KEY]["completed_through"])
        if len(attempts) == 2:
            raise OSError("Synthetic checkpoint interruption")
        original_write(updated)
    monkeypatch.setattr(mem.engine.processing, "write", interrupted)
    with pytest.raises(OSError):
        mem.consolidate()
    offset = ep.metadata[PROGRESS_KEY]["completed_through"]
    assert offset == attempts[0] and not ep.consolidated
    mem.save(str(tmp_path))
    retry = FakeLLM(handler=empty)
    restored = Memory.open(str(tmp_path), llm=retry, config=mem.config)
    restored.consolidate()
    pending = [s for call in retry.calls for s in payload(call["prompt"])["pending"]]
    assert pending[0]["start"] == offset
    assert "".join(s["content"] for s in pending) == ep.content[offset:]
    assert not restored.ingestor.pending()
    before = len(retry.calls)
    restored.consolidate(restored.episodes_doc.values())
    assert len(retry.calls) == before


def test_graph_updates_use_source_time_not_ingestion_or_window_end():
    def reply(prompt, system):
        data = payload(prompt)
        return json.dumps({"facts": [{"subject": "user", "predicate": "works_at", "object": s["content"],
                                      "source_ids": [s["id"]]} for s in data["pending"]], "working": []})
    mem = Memory(llm=FakeLLM(handler=reply))
    for time, company in [(1, "SyntheticOld"), (2, "SyntheticNew")]:
        mem.add(company, session_id="s", event_time=time)
    mem.consolidate()
    subject = mem.graph.get_entity("default", "default")
    assert len(mem.graph.neighbors(subject.id, as_of=3)) == 1
    history = mem.history("default", "works_at")
    old = next(f for f in history if f.object == "SyntheticOld")
    assert old.invalid_at == 2 and old.expired_at is not None


def test_corrupt_progress_is_rejected_on_restore(tmp_path):
    mem = Memory(llm=FakeLLM(handler=empty))
    mem.add("Synthetic content")
    mem.consolidate()
    mem.save(str(tmp_path))
    path = tmp_path / "episodes.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["metadata"][PROGRESS_KEY]["completed_through"] = True
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(StoreFormatError, match="processing record"):
        Memory.open(str(tmp_path))


def test_graph_projection_failure_can_retry_without_duplicate_facts(monkeypatch):
    mem = Memory(llm=model_for_rows())
    ep = mem.add("Synthetic item", session_id="s")
    original = mem.engine.graph_builder.add_fact
    monkeypatch.setattr(mem.engine.graph_builder, "add_fact", lambda f: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        mem.consolidate()
    assert not ep.consolidated and len(mem.fact_store.values()) == 1
    monkeypatch.setattr(mem.engine.graph_builder, "add_fact", original)
    mem.consolidate()
    assert ep.consolidated and len(mem.fact_store.values()) == len(mem.graph.relations()) == 1


def test_mixed_output_has_per_source_working_and_fact_timestamps():
    def reply(prompt, system):
        data = payload(prompt)
        first, second = data["pending"]
        return json.dumps({"facts": [{**rows(data)[0], "source_ids": [first["id"]]}],
                           "working": [{"content": "Synthetic task", "kind": "task",
                                        "source_ids": [second["id"]]}]})
    mem = Memory(llm=FakeLLM(handler=reply))
    ids = [mem.remember(s, session_id="s")["episode_id"] for s in ("Synthetic asset", "Synthetic task")]
    mem.episodes_doc.get(ids[0]).event_time = 1
    mem.episodes_doc.get(ids[1]).event_time = 2
    mem.consolidate()
    fact, = mem.fact_store.values()
    working, = mem.working_mem.values()
    assert fact.valid_at == 1 and fact.provenance == [ids[0]]
    assert working.event_time == 2 and working.metadata["provenance"] == [ids[1]]
    assert mem.episodes_doc.get(ids[0]).metadata["memory_result"] == "long"
    assert mem.episodes_doc.get(ids[1]).metadata["memory_result"] == "working"


def test_window_planner_keeps_users_sessions_and_route_policies_separate():
    model = FakeLLM(handler=empty)
    mem = Memory(llm=model)
    mem.add("long", user_id="u", session_id="s", event_time=1)
    mem.remember("auto", user_id="u", session_id="s")
    mem.add("other", user_id="other", session_id="s")
    mem.consolidate()
    assert len(model.calls) == 3
    assert all(len(payload(call["prompt"])["pending"]) == 1 for call in model.calls)


@pytest.mark.parametrize("setting,value", [("extraction_max_input_bytes", 0),
                                         ("extraction_max_output_tokens", -1),
                                         ("extraction_context_bytes", -1),
                                         ("extraction_max_input_bytes", True)])
def test_bad_window_budget_is_rejected_before_processing(setting, value):
    with pytest.raises(ValueError, match=setting):
        Memory(config=Config(**{setting: value}))


def test_budget_too_small_for_metadata_retains_raw_and_reports_failure():
    from mnemotrace import ExtractionError
    model = FakeLLM(handler=empty)
    mem = Memory(llm=model, config=Config(extraction_max_input_bytes=1))
    ep = mem.add("Synthetic raw input")
    with pytest.raises(ExtractionError) as error:
        mem.consolidate()
    assert error.value.stored_raw and error.value.episode_ids == [ep.id]
    assert ep.metadata["extraction_error"] == "extraction_failed" and not ep.consolidated
    assert not model.calls and PROGRESS_KEY not in ep.metadata


def test_equal_timestamps_keep_source_order_for_identity_even_if_model_reorders_items():
    from mnemotrace import Episode
    def reply(prompt, system):
        spans = payload(prompt)["pending"]
        return json.dumps({"facts": [{"subject": "user", "predicate": "name", "object": s["content"],
                                      "source_ids": [s["id"]]} for s in reversed(spans)], "working": []})
    mem = Memory(llm=FakeLLM(handler=reply))
    first = Episode("Synthetic Alpha", id="ep_z", event_time=1, ingested_at=2)
    second = Episode("Synthetic Beta", id="ep_a", event_time=1, ingested_at=2)
    mem.consolidate([first, second])
    assert mem.engine.self_name("default") == "Synthetic Alpha"


@pytest.mark.parametrize("edit", ["content", "metadata"])
def test_unfinished_window_retry_preserves_intervening_user_edits(monkeypatch, edit):
    mem = Memory(llm=model_for_rows())
    ep = mem.add("Synthetic original", session_id="s")
    original = mem.engine.processing.write
    def interrupted(updated):
        raise OSError("Synthetic checkpoint failure")
    monkeypatch.setattr(mem.engine.processing, "write", interrupted)
    with pytest.raises(OSError):
        mem.consolidate()
    stored, = mem.fact_store.values()
    if edit == "content":
        mem.update_fact(stored.id, object="Synthetic manual correction")
    else:
        mem.update_fact(stored.id, sensitive=True, category="Synthetic manual category")
    monkeypatch.setattr(mem.engine.processing, "write", original)
    mem.consolidate()
    result = mem.fact_store.get(stored.id)
    assert ep.consolidated and len(mem.fact_store.values()) == 1
    if edit == "content":
        assert result.source == "user" and result.object == "Synthetic manual correction"
    else:
        assert result.sensitive and result.category == "Synthetic manual category"
