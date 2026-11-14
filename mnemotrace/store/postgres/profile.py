"""用户配置管理。"""

from ... import profile
from ...extraction import require_llm
from ..contracts import OperationKind, OperationSpec, SpaceSettings
from .errors import DataCorruption, VersionConflict
from .records import FACT, SCOPE, domain
from .dao import _space_row, new_operation_id
from .writes import enqueue


def settings_for(settings):
    if isinstance(settings, dict):
        settings = SpaceSettings(**{**settings, "track": tuple(settings["track"]), "mute": tuple(settings["mute"])})
    return {key: list(getattr(settings, key)) if key in ("track", "mute") else getattr(settings, key)
            for key in ("track", "mute", "profile_system")}


def load(sql):
    row = sql.run(f"SELECT profile_data FROM mnemotrace.profile_states WHERE {SCOPE}", sql.scope).fetchone()
    state = row[0] if row else profile.new_state()
    try:
        return profile.validate_stored(state)
    except (ValueError, TypeError, KeyError) as exc:
        raise DataCorruption("Invalid profile cache or checkpoint") from exc


def save(sql, state):
    sql.insert("profile_states", {"profile_data": state},
               suffix="ON CONFLICT (user_id, space_id) DO UPDATE SET profile_data = excluded.profile_data")


def synchronize(sql, changed_ids, settings_changed=False):
    ids = set(changed_ids)
    if not ids and not settings_changed:
        return
    state = load(sql)
    settings = settings_for(_space_row(sql)[2])
    if settings_changed:
        ids.update(row[0] for row in sql.run(f"SELECT id FROM mnemotrace.facts WHERE {SCOPE}", sql.scope))
    if not ids and not settings_changed:
        return
    facts = {key: domain(record) if (record := sql.get(FACT, key)) else None for key in ids}
    if profile.synchronize(state, facts, settings):
        save(sql, state)


def read(view):
    state = load(view._sql)
    result = profile.read(state, view.at.timestamp())
    row = view._sql.run(f"SELECT operation_id,state,error_code FROM mnemotrace.operations WHERE {SCOPE}"
                       " AND kind = 'profile' ORDER BY (state IN ('pending','running','retry_wait')) DESC,"
                       " ready_at DESC,operation_id DESC LIMIT 1",
                       view._sql.scope).fetchone()
    if row:
        result.update(operation_id=row[0], refresh_state=row[1], refresh_error=row[2])
    return result


def enqueue_refresh(memory):
    require_llm(memory._processing.llm)
    with memory.dao._transaction() as sql:
        profile.fingerprint(settings_for(_space_row(sql)[2]),
                            memory._config, memory._processing.llm)
        row = sql.run(f"SELECT operation_id FROM mnemotrace.operations WHERE {SCOPE}"
                      " AND kind = 'profile' AND state IN ('pending','running','retry_wait') LIMIT 1",
                      sql.scope).fetchone()
        if row:
            operation_id = row[0]
        else:
            operation_id = new_operation_id()
            generation = _space_row(sql)[1]
            enqueue(sql, OperationSpec(operation_id, OperationKind.PROFILE, None), generation,
                    ready_at=sql.run("SELECT clock_timestamp()").fetchone()[0])
    return memory.operation(operation_id)


def run(pipeline, lease):
    from ..._postgres_pipeline import _heartbeat, _failure
    tasks, dao = pipeline.tasks, pipeline.dao
    phase = "generate"
    with _heartbeat(tasks, lease):
        try:
            require_llm(pipeline.llm)
            with dao._transaction() as sql:
                row, now = tasks._leased(sql, lease)
                state = load(sql)
                settings = settings_for(_space_row(sql)[2])
                fingerprint = profile.fingerprint(settings, pipeline.config, pipeline.llm)
                job = state["job"]
                at = now.timestamp()
                if not profile.current(state, job, fingerprint, at):
                    facts = {record.ref.record_id: domain(record)
                             for record in sql.select(FACT, (), ())}
                    # The initial empty space has no fact mutation to initialize its settings.
                    profile.synchronize(state, facts, settings)
                    job = profile.create_job(state, facts, settings, fingerprint, at)
                    state["job"] = job
                    save(sql, state)
                phase = job["phase"]
                if phase == "commit":
                    profile.publish(state, job, at)
                    save(sql, state)
                    tasks._release(sql, row, "completed", phase="done", core_committed=True,
                                   extraction_saved=True)
                    sql.insert("operation_receipts", dict(operation_id=lease.operation_id,
                               core_revision=_space_row(sql)[0], fact_ids=[], completed_at=now),
                               suffix="ON CONFLICT DO NOTHING")
                    return True
            updated = profile.generate(job, pipeline.config, pipeline.llm)
            with dao._transaction() as sql:
                row, now = tasks._leased(sql, lease)
                state = load(sql)
                settings = settings_for(_space_row(sql)[2])
                fingerprint = profile.fingerprint(settings, pipeline.config, pipeline.llm)
                if not profile.current(state, state["job"], fingerprint, now.timestamp()) or state["job"] != job:
                    raise VersionConflict("Profile inputs changed")
                state["job"] = updated
                save(sql, state)
                tasks._release(sql, row, "pending", phase=updated["phase"], extraction_saved=True,
                               generation_attempts=0 if updated["phase"] == "generate" else row["generation_attempts"],
                               ready_at=now)
        except Exception as exc:
            code, retryable = _failure(exc, phase)
            code = getattr(exc, "code", code)
            try:
                tasks.fail(lease, code, retryable and not isinstance(exc, profile.ProfileInputTooLarge))
            except VersionConflict:
                pass
    return True
