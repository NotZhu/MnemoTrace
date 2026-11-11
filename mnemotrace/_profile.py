"""Explicit offline profile operations using the same protocol as PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time
from uuid import uuid4

from . import profile
from .extraction import require_llm
from .llm.failures import ATTEMPT_LIMIT, RETRY_DELAYS, retryable
from .store.contracts import OperationKind, OperationResult, ProcessingPhase, ProcessingState


def state(memory, user, *, create=False):
    if memory._pipeline_state is None:
        if not create:
            return profile.new_state()
        from .consolidate.pipeline import VERSION
        memory._pipeline_state = dict(version=VERSION, namespaces={}, profiles={})
    profiles = memory._pipeline_state["profiles"]
    if create:
        return profiles.setdefault(user, dict(profile.new_state(), operations={}))
    return profiles.get(user, profile.new_state())


def settings(memory):
    return dict(track=list(memory.focus["track"]), mute=list(memory.focus["mute"]),
                profile_system=memory.policy["profile_system"])


def sync(memory, facts):
    with memory._pipeline_lock:
        users = {fact.user_id for fact in facts.values() if fact is not None}
        for user in users:
            profile.synchronize(state(memory, user, create=True),
                                {key: fact for key, fact in facts.items() if fact.user_id == user}, settings(memory))


def sync_settings(memory):
    with memory._pipeline_lock:
        for user in (memory._pipeline_state or {}).get("profiles", {}):
            profile.synchronize(state(memory, user), {fact.id: fact for fact in memory._all_facts(include_unpublished=True)
                                                     if fact.user_id == user}, settings(memory))


def result(operation):
    return OperationResult(operation["operation_id"], OperationKind.PROFILE,
                           ProcessingState(operation["state"]), ProcessingPhase(operation["phase"]),
                           True, operation["saved"], operation["state"] == "completed",
                           operation["attempts"], operation["next_attempt_at"], operation["error_code"])


def enqueue(memory, user):
    if memory._pipeline_closed or memory._pipeline_broken:
        raise RuntimeError("Memory is closed or its checkpoint failed")
    require_llm(memory.llm)
    profile.fingerprint(settings(memory), memory.config, memory.llm)
    with memory._pipeline_lock:
        stored = state(memory, user, create=True)
        for operation in stored["operations"].values():
            if operation["state"] in {"pending", "running", "retry_wait"}:
                return result(operation)
        key = "op_" + uuid4().hex
        operation = dict(operation_id=key, state="pending", phase="generate", saved=False,
                         attempts=0, stage_attempts=0, next_attempt_at=None, error_code=None, sequence=len(stored["operations"]) + 1)
        stored["operations"][key] = operation
        stored["job"] = None
        return result(operation)


def run_one(memory, user):
    if memory._pipeline_closed or memory._pipeline_broken:
        return False
    if not memory._pipeline_step_lock.acquire(blocking=False):
        return False
    try:
        with memory._pipeline_lock:
            stored = state(memory, user)
            operation = next((op for op in stored.get("operations", {}).values()
                              if op["state"] in {"pending", "retry_wait", "running"}), None)
            if operation is None or (operation["next_attempt_at"] or 0) > time.time():
                return False
            if operation["stage_attempts"] >= ATTEMPT_LIMIT:
                operation.update(state="failed", error_code="attempts_exhausted")
                return False
            operation["attempts"] += 1
            operation["stage_attempts"] += 1
            operation.update(state="running", error_code=None, next_attempt_at=None)
            options = settings(memory)
            fingerprint = profile.fingerprint(options, memory.config, memory.llm)
            job = stored["job"]
            at = time.time()
            if not profile.current(stored, job, fingerprint, at):
                facts = {fact.id: fact for fact in memory._all_facts(include_unpublished=True)
                         if fact.user_id == user}
                profile.synchronize(stored, facts, options)
                job = profile.create_job(stored, facts, options, fingerprint, at)
                stored["job"] = job
            job = deepcopy(job)
            operation["phase"] = job["phase"]
            if job["phase"] == "commit":
                profile.publish(stored, job, at)
                operation.update(state="completed", phase="done", saved=True)
                return True
        try:
            require_llm(memory.llm)
            updated = profile.generate(job, memory.config, memory.llm)
            with memory._pipeline_lock:
                options = settings(memory)
                fingerprint = profile.fingerprint(options, memory.config, memory.llm)
                if memory._pipeline_closed or not profile.current(stored, stored["job"], fingerprint, time.time()) or stored["job"] != job:
                    raise RuntimeError("Profile inputs changed")
                stored["job"] = updated
                operation.update(state="pending", phase=updated["phase"], saved=True, stage_attempts=0)
        except Exception as exc:
            with memory._pipeline_lock:
                code = getattr(exc, "code", "profile_generation_failed")
                failed = operation["stage_attempts"] >= ATTEMPT_LIMIT or not retryable(exc) or isinstance(exc, profile.ProfileInputTooLarge)
                operation.update(state="failed" if failed else "retry_wait", error_code=code,
                                 next_attempt_at=None if failed else time.time() + RETRY_DELAYS[operation["stage_attempts"] - 1])
        return True
    finally:
        memory._pipeline_step_lock.release()


def flush(memory, user, deadline, retry_failed):
    with memory._pipeline_lock:
        operations = state(memory, user).get("operations", {})
        latest = max(operations.values(), key=lambda op: op["sequence"], default=None)
        if latest is None:
            return None
        if retry_failed and latest["state"] in {"failed", "retry_wait"}:
            latest.update(state="pending", next_attempt_at=None, stage_attempts=0)
    executor = ThreadPoolExecutor(max_workers=1)
    future = None
    try:
        while True:
            if future is not None and future.done():
                future.result()
                future = None
            with memory._pipeline_lock:
                outcome = result(latest)
            remaining = deadline - time.monotonic()
            if (outcome.state in (ProcessingState.COMPLETED, ProcessingState.FAILED) or remaining <= 0
                    or memory._pipeline_closed or memory._pipeline_broken):
                return outcome
            if future is None:
                future = executor.submit(run_one, memory, user)
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
