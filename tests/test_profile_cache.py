"""Explicit cached profiles share evidence, invalidation and recovery semantics."""
from tests.model_fixtures import seed_timeline
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from mnemotrace import MemoryEngine, Config
from mnemotrace.llm import FakeLLM
from mnemotrace.profile import empty, validate_output
from mnemotrace.consolidate.pipeline import validate_state
from mnemotrace.store.contracts import FactQuery, FactState, RetrievalQuery


def model(callback=None):
    def respond(prompt, system):
        data = json.loads(prompt)
        if callback:
            callback()
        output = data["previous_profile"]
        existing = {key for entries in output.values() for item in entries for key in item["fact_ids"]}
        for fact in data["facts"]:
            if fact["id"] not in existing:
                output["basic"].append(dict(text=fact["text"], fact_ids=[fact["id"], fact["id"]]))
        return json.dumps(output)
    return FakeLLM(handler=respond)


def finish(memory):
    assert memory.flush_messages(timeout=1)["ok"]
    return memory.profile()


def test_reads_and_context_never_generate():
    llm = model()
    memory = MemoryEngine(llm=llm)
    fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    assert memory.profile()["pending_fact_count"] == 1
    assert memory.profile()["updated_at"] is None
    memory.lean_context("tea")
    assert not llm.calls
    operation = memory.refresh_profile()
    assert operation.kind.value == "profile"
    assert memory.refresh_profile().operation_id == operation.operation_id
    cached = finish(memory)
    assert cached["profile"]["basic"][0]["fact_ids"] == [fact.id]
    assert cached["profile"]["basic"][0]["fact_count"] == 1
    assert cached["pending_fact_count"] == 0 and not cached["needs_refresh"]
    count = len(llm.calls)
    memory.profile()
    memory.lean_context("tea")
    assert "USER PROFILE" not in memory.lean_context("tea", redact_sensitive=True)
    assert "USER PROFILE" not in memory.lean_context("tea", facts=FactQuery())
    assert len(llm.calls) == count


@pytest.mark.parametrize("scope", [
    FactQuery(state=FactState.HISTORY), FactQuery(state=FactState.ALL),
    FactQuery(valid_at=100), FactQuery(exclude_sensitive=True),
    FactQuery(categories=("technology",)), FactQuery(subject="Other person"),
    FactQuery(subject="我"), FactQuery(predicate="uses"), FactQuery(source_ids=("source-a",)),
])
def test_postgres_context_does_not_read_profile_outside_unrestricted_current_view(monkeypatch, scope):
    from mnemotrace.retrieve import postgres
    from mnemotrace.store.postgres import profile

    reads = []
    cached = dict(profile=dict(empty(), interests=[dict(text="Synthetic food profile", fact_ids=["a"])]))
    monkeypatch.setattr(profile, "read", lambda view: reads.append(view) or cached)
    monkeypatch.setattr(postgres, "RetrievalGraph", lambda *_: SimpleNamespace(limitations=lambda: ()))
    view = SimpleNamespace(snapshot=SimpleNamespace(context=SimpleNamespace(space_id="synthetic-space")),
                           candidates=lambda *_args, **_kwargs: SimpleNamespace(candidates=(), limitations=()))
    result = postgres.context(view, RetrievalQuery(scope, ""), Config(), n_facts=10,
                              n_digests=0, char_budget=48_000)
    assert "Synthetic food profile" not in result.context
    assert not reads


@pytest.mark.parametrize("scope", [FactQuery(), FactQuery(limit=1)])
def test_postgres_context_omits_profile_for_unrestricted_current_view(monkeypatch, scope):
    from mnemotrace.retrieve import postgres
    from mnemotrace.store.postgres import profile

    cached = dict(profile=dict(empty(), interests=[dict(text="Synthetic food profile", fact_ids=["a"])]))
    monkeypatch.setattr(profile, "read", lambda _: cached)
    monkeypatch.setattr(postgres, "RetrievalGraph", lambda *_: SimpleNamespace(limitations=lambda: ()))
    view = SimpleNamespace(snapshot=SimpleNamespace(context=SimpleNamespace(space_id="synthetic-space")),
                           candidates=lambda *_args, **_kwargs: SimpleNamespace(candidates=(), limitations=()))
    result = postgres.context(view, RetrievalQuery(scope, ""), Config(), n_facts=10,
                              n_digests=0, char_budget=48_000)
    assert "USER PROFILE" not in result.context and "Synthetic food profile" not in result.context


def test_unchanged_large_fact_can_refresh_again_without_duplicate_input():
    def summarize(prompt, system):
        data = json.loads(prompt)
        if data["previous_profile"]["basic"]:
            return json.dumps(data["previous_profile"])
        return json.dumps(dict(empty(), basic=[dict(text="Synthetic short profile", fact_ids=[data["facts"][0]["id"]])]))

    llm = FakeLLM(handler=summarize)
    memory = MemoryEngine(llm=llm)
    fact = memory.add_fact("我", "note", "x" * 14_000, sensitive=False, text=' '.join(('user', 'note', 'x' * 14000)))
    memory.refresh_profile()
    first = finish(memory)
    assert first["profile"]["basic"][0]["fact_ids"] == [fact.id]
    memory.refresh_profile()
    second = finish(memory)
    assert second["profile"] == first["profile"]
    assert second["cache_version"] == first["cache_version"] + 1
    assert len(llm.calls) == 2
    for call in llm.calls:
        data = json.loads(call["prompt"])
        inputs = data["supporting_facts"] + data["facts"]
        ids = [item["id"] for item in inputs]
        assert ids == [fact.id]
        assert inputs[0]["object"] == fact.object
        assert len(call["system"]) + len(call["prompt"]) <= memory.config.profile_max_input_chars


def test_old_support_is_not_repeated_when_batched_refresh_resumes():
    from mnemotrace._profile import run_one, state
    from mnemotrace.types import Fact

    def summarize(prompt, system):
        data = json.loads(prompt)
        return json.dumps(data["previous_profile"] if data["previous_profile"]["basic"] else
                          dict(empty(), basic=[dict(text="Synthetic summary", fact_ids=[data["facts"][0]["id"]])]))

    memory = MemoryEngine(llm=FakeLLM(handler=summarize))
    old = memory.add_fact("我", "note", "x" * 14_000, sensitive=False, text=' '.join(('user', 'note', 'x' * 14000)))
    memory.refresh_profile()
    finish(memory)
    for key in ("zz-1", "zz-2"):
        memory._upsert_fact(Fact("我", "note", "z" * 5_500, id=key, sensitive=False, text=' '.join(('user', 'note', 'z' * 5500))))
    memory.refresh_profile()
    assert run_one(memory, "default")
    assert state(memory, "default")["job"]["phase"] == "generate"
    validate_state(memory._pipeline_state, memory.episodes_doc.values())
    restored = MemoryEngine(llm=FakeLLM(handler=summarize))
    for fact in memory._all_facts():
        restored.fact_store.upsert(fact.id, fact.embedding, deepcopy(fact))
    restored._pipeline_state = deepcopy(memory._pipeline_state)
    assert finish(restored)["pending_fact_count"] == 0
    batches = memory.llm.calls[1:] + restored.llm.calls
    assert len(batches) == 2
    assert [json.loads(call["prompt"])["facts"][0]["id"] for call in batches] == ["zz-1", "zz-2"]
    for call in batches:
        data = json.loads(call["prompt"])
        ids = [item["id"] for item in data["supporting_facts"] + data["facts"]]
        assert ids.count(old.id) == 1 and len(ids) == len(set(ids))


@pytest.mark.parametrize("change", ["delete", "sensitive", "mute"])
def test_changes_scrub_cache_and_saved_input(change):
    memory = MemoryEngine(llm=model())
    fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    finish(memory)
    memory.refresh_profile()
    from mnemotrace._profile import run_one, state
    assert run_one(memory, "default")
    assert state(memory, "default")["job"]
    if change == "delete":
        memory.delete_fact(fact.id)
    elif change == "sensitive":
        memory.set_fact_sensitive(fact.id, True)
    else:
        memory.set_focus(mute=["tea"])
    assert memory.profile()["profile"]["basic"] == []
    assert state(memory, "default")["job"] is None
    assert "Synthetic tea" not in json.dumps(state(memory, "default"))
    finish(memory)
    assert not memory.profile()["needs_refresh"]


def test_retrieval_usage_does_not_invalidate_profile():
    memory = MemoryEngine(llm=model())
    fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    before = finish(memory)
    memory.search("tea")
    memory._upsert_fact(fact)
    assert memory.profile() == before


def test_model_output_must_cite_supplied_facts():
    for value in ({}, dict(empty(), basic=[dict(text="Unsupported", fact_ids=["missing"])]),
                  dict(empty(), basic=[dict(text="Count", fact_ids=["a"], fact_count=1)])):
        with pytest.raises(Exception, match="invalid extraction response"):
            validate_output(value, {"a"})


def test_zero_facts_publishes_empty_cache_without_model_call():
    llm = model()
    memory = MemoryEngine(llm=llm)
    memory.refresh_profile()
    cached = finish(memory)
    assert cached["profile"] == empty() and cached["updated_at"] is not None
    assert not llm.calls


def test_batch_checkpoint_resumes_after_restart():
    def compact(prompt, system):
        data = json.loads(prompt)
        output = empty()
        if data["facts"]:
            last = data["facts"][-1]
            output["basic"] = [dict(text=last["text"], fact_ids=[last["id"]])]
        return json.dumps(output)
    memory = MemoryEngine(config=Config(profile_max_input_chars=2600), llm=FakeLLM(handler=compact))
    for index in range(8):
        memory.add_fact("我", "note", "Synthetic evidence " * 8 + str(index), sensitive=False, text=' '.join(('user', 'note', 'Synthetic evidence ' * 8 + str(index))))
    memory.refresh_profile()
    from mnemotrace._profile import run_one
    assert run_one(memory, "default")
    validate_state(memory._pipeline_state, memory.episodes_doc.values())
    restored = MemoryEngine(config=memory.config, llm=FakeLLM(handler=compact))
    for fact in memory._all_facts():
        restored.fact_store.upsert(fact.id, fact.embedding, deepcopy(fact))
    restored._pipeline_state = deepcopy(memory._pipeline_state)
    cached = finish(restored)
    assert cached["cache_version"] == 1
    assert cached["pending_fact_count"] == 0
    assert cached["profile"]["basic"] and len(restored.llm.calls) < 8


def test_missing_model_refresh_fails_before_creating_task():
    memory = MemoryEngine()
    with pytest.raises(Exception, match="Configure an LLM"):
        memory.refresh_profile()
    assert memory._pipeline_state is None


def test_v2_fact_import_without_model_starts_with_pending_profile():
    source = MemoryEngine()
    fact = source.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory = MemoryEngine()
    result = memory.import_snapshot({"format_version": 6, "facts": [asdict(fact)], "episodes": []})
    assert result["facts_restored"] == 1
    assert memory.profile()["updated_at"] is None
    assert memory.profile()["pending_fact_count"] == 1
    assert memory.profile()["needs_refresh"]
    memory.llm = model()
    memory.refresh_profile()
    assert finish(memory)["profile"]["basic"]


def test_generation_concurrent_deletion_cannot_publish_obsolete_text():
    memory = MemoryEngine()
    fact = memory.add_fact("我", "likes", "Synthetic old tea", sensitive=False, text='user likes Synthetic old tea')
    changed = False

    def intervene():
        nonlocal changed
        if not changed:
            changed = True
            memory.delete_fact(fact.id)

    memory.llm = model(intervene)
    memory.refresh_profile()
    from mnemotrace._profile import run_one, state
    assert run_one(memory, "default")
    assert memory.profile()["profile"] == empty()
    assert "Synthetic old tea" not in json.dumps(state(memory, "default"))
    assert memory.flush_messages(timeout=1, retry_failed=True)["ok"]
    assert "Synthetic old tea" not in json.dumps(memory.profile())


def test_time_transitions_hide_cache_and_preserve_pending_changes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mnemotrace.memory_engine.now", lambda: clock[0])
    monkeypatch.setattr("mnemotrace._profile.time.time", lambda: clock[0])
    memory = MemoryEngine(llm=model())
    old = memory.add_fact("我", "lives_in", "Synthetic Paris", valid_at=10, sensitive=False, text='user lives in Synthetic Paris')
    seed_timeline(memory, "我", "lives_in", "Synthetic Oslo", valid_at=200, sensitive=False, text='user lives in Synthetic Oslo')
    memory.refresh_profile()
    cached = finish(memory)
    assert cached["profile"]["basic"][0]["fact_ids"] == [old.id]
    assert cached["pending_fact_count"] == 0
    clock[0] = 250.0
    assert memory.profile()["profile"] == empty()
    assert memory.profile()["pending_fact_count"] == 2
    memory.refresh_profile()
    assert "Synthetic Oslo" in json.dumps(finish(memory))


def test_failed_generation_keeps_cache_and_can_be_retried():
    memory = MemoryEngine(llm=model())
    memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    before = finish(memory)
    memory.llm = FakeLLM(responses=["{}"] * 3)
    memory.refresh_profile()
    from mnemotrace._profile import run_one, state
    for _ in range(3):
        assert run_one(memory, "default")
        for operation in state(memory, "default")["operations"].values():
            operation["next_attempt_at"] = None
    failed = memory.profile()
    assert failed["refresh_state"] == "failed"
    assert failed["profile"] == before["profile"]
    memory.llm = model()
    memory.refresh_profile()
    assert finish(memory)["refresh_state"] == "completed"


def test_input_too_large_is_explicit_and_preserves_existing_cache():
    memory = MemoryEngine(llm=model())
    memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    before = finish(memory)
    memory.config.profile_max_input_chars = 10
    operation = memory.refresh_profile()
    flushed = memory.flush_messages(timeout=1)
    assert not flushed["ok"] and flushed["status"] == "failed"
    assert flushed["counts"]["failed"] == 1
    job, = flushed["jobs"]
    assert job["operation_id"] == operation.operation_id
    assert job["error_code"] == "profile_input_too_large"
    assert memory.profile()["profile"] == before["profile"]


def test_profile_filters_non_owner_and_sensitive_facts_and_counts_ids():
    memory = MemoryEngine(llm=model())
    fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    fact.provenance = ["message-a", "message-b"]
    memory._upsert_fact(fact)
    memory.add_fact("Other person", "likes", "Synthetic foreign", sensitive=False, text='Other person likes Synthetic foreign')
    memory.add_fact("我", "likes", "Synthetic private", sensitive=True, text='user likes Synthetic private')
    memory.refresh_profile()
    entry, = finish(memory)["profile"]["basic"]
    assert entry["fact_ids"] == [fact.id] and entry["fact_count"] == 1


def test_corrupted_profile_checkpoint_is_rejected():
    memory = MemoryEngine(llm=model())
    memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    from mnemotrace._profile import run_one
    assert run_one(memory, "default")
    saved = deepcopy(memory._pipeline_state)
    saved["profiles"]["default"]["job"]["offset"] = 9999
    with pytest.raises(ValueError):
        validate_state(saved, memory.episodes_doc.values())


def test_new_profile_policy_reconsiders_previously_omitted_facts():
    memory = MemoryEngine(llm=FakeLLM(responses=[json.dumps(empty())]))
    fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    assert finish(memory)["profile"] == empty()
    assert not memory.profile()["needs_refresh"]
    memory.set_policy(profile_system="Include supported interests")
    assert memory.profile()["needs_refresh"]
    memory.llm = model()
    memory.refresh_profile()
    assert finish(memory)["profile"]["basic"][0]["fact_ids"] == [fact.id]


def test_prompt_update_is_visible_without_calling_model(monkeypatch):
    memory = MemoryEngine(llm=model())
    memory.refresh_profile()
    finish(memory)
    monkeypatch.setattr("mnemotrace.profile.SYSTEM", "Updated profile schema guidance")
    assert memory.profile()["needs_refresh"]
    assert not memory.llm.calls


def test_refresh_uses_cold_facts_and_historical_context_skips_current_cache():
    memory = MemoryEngine(llm=model())
    cold = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.add_fact("我", "likes", "Synthetic coffee", sensitive=False, text='user likes Synthetic coffee')
    cold.salience = 0
    assert memory.evict_cold(max_hot=1) == 1
    memory.refresh_profile()
    assert any(cold.id in entry["fact_ids"] for entry in finish(memory)["profile"]["basic"])
    assert "USER PROFILE" not in memory.lean_context("tea")
    assert "USER PROFILE" not in memory.lean_context("tea", as_of=100)


def test_profile_entry_counts_distinct_facts():
    memory = MemoryEngine()
    first = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    second = memory.add_fact("我", "likes", "Synthetic coffee", sensitive=False, text='user likes Synthetic coffee')
    output = dict(empty(), interests=[dict(text="Synthetic interests", fact_ids=[first.id, second.id, first.id])])
    memory.llm = FakeLLM(responses=[json.dumps(output)])
    memory.refresh_profile()
    entry, = finish(memory)["profile"]["interests"]
    assert entry["fact_count"] == 2


@pytest.mark.parametrize("corruption", ["availability", "fact_input", "entry_version", "operation"])
def test_corrupted_profile_dependencies_and_operations_are_rejected(corruption):
    memory = MemoryEngine(llm=model())
    memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    finish(memory)
    memory.refresh_profile()
    from mnemotrace._profile import run_one
    assert run_one(memory, "default")
    saved = deepcopy(memory._pipeline_state)
    state = saved["profiles"]["default"]
    key = next(iter(state["facts"]))
    if corruption == "availability":
        state["job"]["availability"][key] = "false"
    elif corruption == "fact_input":
        state["job"]["facts"][0]["text"] = "Changed without a revision"
    elif corruption == "entry_version":
        state["cache"]["basic"][0]["versions"][key] = "1"
    else:
        next(iter(state["operations"].values()))["sequence"] = False
    with pytest.raises(ValueError):
        validate_state(saved, memory.episodes_doc.values())


def test_profile_flush_uses_remaining_message_timeout(monkeypatch):
    from mnemotrace import _pipeline

    memory = MemoryEngine(llm=model())
    memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    elapsed = 0

    def finish_messages(*args):
        nonlocal elapsed
        elapsed = 1
        return {"ok": True, "status": "completed", "jobs": [], "counts": {"pending": 0}}

    monkeypatch.setattr(_pipeline, "flush_memory", finish_messages)
    monkeypatch.setattr("time.monotonic", lambda: elapsed)
    result = memory.flush_messages(timeout=1)
    assert result["status"] == "timed_out" and not result["ok"]
    assert result["counts"]["pending"] == 1
    assert not memory.llm.calls


def test_profile_flush_times_out_while_model_is_still_running():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(3), "The blocked model was not released"

    with MemoryEngine(llm=model(block)) as memory, ThreadPoolExecutor(max_workers=1) as caller:
        fact = memory.add_fact("我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
        operation = memory.refresh_profile()
        pending = caller.submit(memory.flush_messages, timeout=0.05)
        try:
            assert entered.wait(2)
            result = pending.result(timeout=0.5)
            assert not result["ok"] and result["status"] == "timed_out"
            assert memory.operation(operation.operation_id).state.value == "running"
            assert memory.profile()["updated_at"] is None
            again = memory.flush_messages(timeout=0.01)
            assert again["status"] == "timed_out"
            assert len(memory.llm.calls) == 1
        finally:
            release.set()
        assert memory.flush_messages(timeout=2)["ok"]
        cached = memory.profile()
        assert cached["profile"]["basic"][0]["fact_ids"] == [fact.id]
        assert cached["cache_version"] == 1
        assert len(memory.llm.calls) == 1


@pytest.mark.parametrize("change", ["delete", "close"])
def test_profile_result_after_timeout_obeys_invalidation(change):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()

    def block():
        entered.set()
        assert release.wait(3)

    with MemoryEngine(llm=model(block)) as memory, ThreadPoolExecutor(max_workers=1) as caller:
        fact = memory.add_fact("我", "likes", "Synthetic old tea", sensitive=False, text='user likes Synthetic old tea')
        operation = memory.refresh_profile()
        pending = caller.submit(memory.flush_messages, timeout=0.05)
        try:
            assert entered.wait(2)
            assert pending.result(timeout=0.5)["status"] == "timed_out"
            if change == "delete":
                memory.delete_fact(fact.id)
            else:
                memory.close()
        finally:
            release.set()
        # The step releases its lease after checking the result against current inputs.
        with memory._pipeline_step_lock:
            pass
        assert not memory.operation(operation.operation_id).core_committed
        assert memory.profile()["profile"] == empty()
        if change != "close":
            assert memory.flush_messages(timeout=2, retry_failed=True)["ok"]
            cached = json.dumps(memory.profile())
            assert "Synthetic old tea" not in cached


def test_profile_flush_propagates_background_step_errors(monkeypatch):
    from mnemotrace import _profile

    def fail(*args):
        raise RuntimeError("Synthetic runner failure")

    with MemoryEngine(llm=model()) as memory:
        memory.refresh_profile()
        monkeypatch.setattr(_profile, "run_one", fail)
        with pytest.raises(RuntimeError, match="Synthetic runner failure"):
            memory.flush_messages(timeout=1)
