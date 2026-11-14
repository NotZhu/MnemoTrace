"""Explicit profile refresh reaches the persistent task and product interfaces."""
import json

import pytest

from mnemotrace.memory import Memory
from tests.postgres.fact_fixtures import seed_fact, seed_note
from mnemotrace.llm import FakeLLM
from mnemotrace.profile import empty
from mnemotrace.store.contracts import FactQuery, FactState, RecordKind, RecordRef
from mnemotrace.store.postgres.profile import load
from mnemotrace.types import Fact
from tests.postgres.test_record_business import commit
from tests.test_profile_cache import model


@pytest.fixture
def memory(runtime, spaces, user):
    context = spaces.authorize(user, spaces.create(user, "Profiles").space_id)
    return Memory(runtime, context, llm=model())


def finish(memory):
    assert memory.flush_processing(timeout=3)["ok"]
    return memory.profile()


def test_profile_task_roundtrip_and_read_only_cache(memory):
    fact = seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    assert memory.profile()["pending_fact_count"] == 1
    assert memory.profile()["updated_at"] is None
    operation = memory.refresh_profile()
    assert memory.refresh_profile().operation_id == operation.operation_id
    result = finish(memory)
    assert result["profile"]["basic"][0]["fact_ids"] == [fact.id]
    assert result["pending_fact_count"] == 0
    count = len(memory._processing.llm.calls)
    assert Memory(memory._database, memory.context).profile() == result
    memory.lean_context("tea")
    assert len(memory._processing.llm.calls) == count


@pytest.mark.parametrize("scope", [
    FactQuery(state=FactState.HISTORY), FactQuery(state=FactState.ALL),
    FactQuery(valid_at=100),
    FactQuery(categories=("technology",)), FactQuery(subject="Other person"),
    FactQuery(subject="我"), FactQuery(predicate="uses"), FactQuery(source_ids=("source-a",)),
])
def test_context_profile_does_not_bypass_fact_conditions(memory, scope):
    seed_fact(memory, "我", "likes", "Synthetic tea", categories=["food"], sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    finish(memory)
    count = len(memory._processing.llm.calls)
    assert "USER PROFILE" not in memory.lean_context("tea", n_digests=0).context
    restricted = memory.lean_context("tea", facts=scope, n_digests=0)
    assert "USER PROFILE" not in restricted.context
    assert len(memory._processing.llm.calls) == count


def test_unchanged_large_fact_can_refresh_again(memory):
    fact = seed_fact(memory, "我", "note", "x" * 14_000, sensitive=False, text=' '.join(('user', 'note', 'x' * 14000)))
    output = json.dumps(dict(empty(), basic=[dict(text="Synthetic short profile", fact_ids=[fact.id])]))
    llm = FakeLLM(responses=[output, output])
    memory._processing.llm = llm
    memory.refresh_profile()
    before = finish(memory)
    memory.refresh_profile()
    after = finish(memory)
    assert after["profile"] == before["profile"]
    assert after["cache_version"] == before["cache_version"] + 1
    assert len(llm.calls) == 2
    for call in llm.calls:
        data = json.loads(call["prompt"])
        ids = [item["id"] for item in data["supporting_facts"] + data["facts"]]
        assert ids == [fact.id]


@pytest.mark.parametrize("action", ["delete", "sensitive", "mute"])
def test_profile_invalidates_and_scrubs_stored_product(memory, action):
    fact = seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    memory.refresh_profile()
    finish(memory)
    operation = memory.refresh_profile()
    assert memory._processing.run_one(operation.operation_id)
    if action == "delete":
        memory.delete_fact(fact.id)
    elif action == "sensitive":
        memory.set_fact_sensitive(fact.id, True)
    else:
        memory.set_settings(mute=("tea",))
    assert memory.profile()["profile"]["basic"] == []
    with memory.dao.read_snapshot() as view:
        state = load(view._sql)
        assert state["job"] is None
        assert "Synthetic tea" not in json.dumps(state)
    finish(memory)
    assert not memory.profile()["needs_refresh"]


def test_rejected_source_erasure_preserves_profile_cache_and_checkpoint(memory):
    removed = seed_note(memory, "Synthetic source to erase", "chat")
    remaining = seed_note(memory, "Synthetic source to retain", "chat")
    fact = Fact("我", "likes", "Synthetic tea", user_id=memory.context.user_id, sensitive=False,
                text="User likes synthetic tea.", provenance=[removed.id, remaining.id])
    commit(memory, "synthetic-fact", (fact,))
    memory.refresh_profile()
    assert finish(memory)["profile"]["basic"][0]["fact_ids"] == [fact.id]
    operation = memory.refresh_profile()
    assert memory._processing.run_one(operation.operation_id)
    with memory.dao.read_snapshot() as view:
        before = load(view._sql)
        assert removed.id in json.dumps(before["job"])

    with pytest.raises(ValueError, match="only supports facts"):
        memory.erase(RecordRef(memory.context, RecordKind.SOURCE, removed.id))

    assert memory.get_fact(fact.id).provenance == [removed.id, remaining.id]
    assert memory.get_episode(removed.id) is not None
    with memory.dao.read_snapshot() as view:
        assert load(view._sql) == before
    assert finish(memory)["profile"]["basic"][0]["fact_ids"] == [fact.id]


def test_profile_checkpoint_resume_without_regenerating(memory):
    seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    operation = memory.refresh_profile()
    assert memory._processing.run_one(operation.operation_id)
    count = len(memory._processing.llm.calls)
    fresh = Memory(memory._database, memory.context, llm=memory._processing.llm)
    assert fresh._processing.run_one(operation.operation_id)
    assert fresh.operation(operation.operation_id).core_committed
    assert len(memory._processing.llm.calls) == count
    assert fresh.profile()["profile"]["basic"]


def test_profile_api_and_acl(product_client, product_service, product_scope):
    context, key = product_scope("profile-owner")
    headers = {"Authorization": "Bearer " + key}
    params = {"space_id": context.space_id}
    product_service.llm = model()
    seed_fact(product_service.memory(context), "我", "likes", "Synthetic tea", text="User likes synthetic tea.")
    result = product_client.get("/v1/profile", headers=headers, params=params)
    assert result.json()["updated_at"] is None
    refreshed = product_client.post("/v1/profile/refresh", headers=headers, json=params)
    assert refreshed.status_code == 202
    operation_id = refreshed.json()["operation_id"]
    memory = product_service.memory(context)
    assert memory.flush_processing(timeout=3)["ok"]
    assert product_client.get("/v1/profile", headers=headers, params=params).json()["profile"]["basic"]
    current = product_client.post("/v1/recall", headers=headers, json=dict(params, query="tea", n_digests=0))
    assert current.is_success and "USER PROFILE" not in current.json()["context"]
    for state in ("history", "all"):
        historical = product_client.post("/v1/recall", headers=headers,
                                         json=dict(params, query="tea", n_digests=0, state=state))
        assert historical.is_success and "USER PROFILE" not in historical.json()["context"]
    other, _ = product_scope("profile-outsider")
    assert product_client.get("/v1/operations/" + operation_id,
                              headers=headers, params={"space_id": other.space_id}).status_code == 404


def test_inflight_generation_is_fenced_by_fact_deletion(memory):
    fact = seed_fact(memory, "我", "likes", "Synthetic old tea", sensitive=False, text='user likes Synthetic old tea')
    changed = False

    def intervene():
        nonlocal changed
        if not changed:
            changed = True
            memory.delete_fact(fact.id)

    memory._processing.llm = model(intervene)
    operation = memory.refresh_profile()
    assert memory._processing.run_one(operation.operation_id)
    assert not memory.operation(operation.operation_id).core_committed
    with memory.dao.read_snapshot() as view:
        assert "Synthetic old tea" not in json.dumps(load(view._sql))
    finish(memory)
    assert "Synthetic old tea" not in json.dumps(memory.profile())
    assert memory.profile()["pending_fact_count"] == 0


def test_worker_discovers_space_profile_without_session(memory, cluster):
    from mnemotrace import Config
    from mnemotrace.store.postgres import Database
    from mnemotrace.worker import run_worker

    seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    operation = memory.refresh_profile()
    with Database(cluster.discovery, purpose="discovery") as discovery:
        assert (memory.context, operation.operation_id) in discovery.discover_processing()
        for _ in range(3):
            run_worker(discovery, memory._database, Config(), memory._processing.llm, None, "unused", once=True)
    assert memory.operation(operation.operation_id).core_committed
    assert memory.profile()["profile"]["basic"]


def test_retry_old_failed_refresh_while_another_is_active(memory):
    from mnemotrace.store.postgres.errors import VersionConflict

    failed = memory.refresh_profile()
    tasks = memory._processing.tasks
    lease = tasks.claim(failed.operation_id)
    tasks.fail(lease, "synthetic_failure", False)
    active = memory.refresh_profile()
    with pytest.raises(VersionConflict, match="Another profile"):
        memory.retry_operation(failed.operation_id)
    assert memory.refresh_profile().operation_id == active.operation_id


def test_retry_old_refresh_is_reported_as_the_current_operation(memory):
    failed = memory.refresh_profile()
    tasks = memory._processing.tasks
    lease = tasks.claim(failed.operation_id)
    tasks.fail(lease, "synthetic_failure", False)
    memory.refresh_profile()
    finish(memory)
    memory.retry_operation(failed.operation_id)
    assert memory.profile()["operation_id"] == failed.operation_id
    assert memory.profile()["refresh_state"] == "pending"
    finish(memory)
    assert memory.profile()["operation_id"] == failed.operation_id


def test_clear_space_removes_profile_and_tasks(memory):
    seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    operation = memory.refresh_profile()
    finish(memory)
    memory.clear_space()
    assert memory.profile()["updated_at"] is None
    assert memory.profile()["pending_fact_count"] == 0
    assert memory.operation(operation.operation_id) is None


def test_profile_without_model_reads_cache_and_refresh_has_configuration_error(product_client, product_service, product_scope):
    target, key = product_scope("profile-no-model")
    headers = {"Authorization": "Bearer " + key}
    product_service.llm = None
    seed_fact(product_service.memory(target), "我", "likes", "Synthetic tea", text="User likes synthetic tea.")
    result = product_client.get("/v1/profile", headers=headers, params={"space_id": target.space_id}).json()
    assert result["updated_at"] is None and result["pending_fact_count"] == 1
    refreshed = product_client.post("/v1/profile/refresh", headers=headers, json={"space_id": target.space_id})
    assert refreshed.status_code == 503
    assert refreshed.json()["detail"]["code"] == "llm_not_configured"


def test_access_reinforcement_does_not_load_profile_state(memory, monkeypatch):
    from mnemotrace.store.postgres import profile as storage

    fact = seed_fact(memory, "我", "likes", "Synthetic tea", sensitive=False, text='user likes Synthetic tea')
    reads = []
    monkeypatch.setattr(storage, "load", lambda sql: reads.append(sql) or load(sql))
    assert memory.search("Synthetic tea").top().id == fact.id
    assert memory.get_fact(fact.id).access_count == 1
    assert not reads
