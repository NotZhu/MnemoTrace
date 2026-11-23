"""验证真库和内存替身的事实审计契约。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from math import inf, nan
from typing import cast

import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.space import Space
from mnemotrace.ports.storage import CutoffTrigger, Storage

from ..ports.fakes import FakeStorage
from .conftest import PgDatabase
from .sample import fact, pool, run, space


@pytest.fixture(
    params=(False, pytest.param(True, marks=pytest.mark.postgres)),
    ids=("memory", "postgres"),
)
def fact_database(request: pytest.FixtureRequest) -> PgDatabase | None:
    """真库用例使用专用连接，内存用例可离线执行。"""
    if request.param:
        return cast(PgDatabase, request.getfixturevalue("database"))
    return None


@asynccontextmanager
async def _fact_storage(
    scope: Space, database: PgDatabase | None
) -> AsyncIterator[Storage]:
    if database is None:
        memory = FakeStorage()
        await memory.create_space(scope)
        yield memory
        return
    async with pool(database.runtime) as connections:
        postgres = PostgresStorage(connections, owner_id=scope.owner_id)
        await postgres.create_space(scope)
        yield postgres


def test_save_facts_rejects_cutoff_triggers_on_unchanged_facts(
    fact_database: PgDatabase | None,
) -> None:
    """只有实际设置或提前截止才能附带触发器，普通重放保持幂等。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        old = replace(fact(subject, "old"), valid_at=10.0)
        cause = replace(fact(subject, "cause"), valid_at=20.0)
        closed = old.close(20.0, 30.0)
        trigger = CutoffTrigger(cause.id, 20.0, ())
        triggers = {old.id: (trigger,)}
        async with _fact_storage(scope, fact_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((old, cause), changed_at=10.0)
                with pytest.raises(ValueError, match="截止"):
                    await writer.save_facts(
                        (old,), changed_at=30.0, cutoff_triggers=triggers
                    )
                (change,) = await writer.save_facts(
                    (closed,), changed_at=30.0, cutoff_triggers=triggers
                )
                assert change.cutoff_triggers == (trigger,)
                with pytest.raises(ValueError, match="截止"):
                    await writer.save_facts(
                        (closed,), changed_at=40.0, cutoff_triggers=triggers
                    )
                assert await writer.save_facts((closed,), changed_at=40.0) == ()
            async with storage.session(scope.id) as reader:
                latest, created = await reader.list_fact_changes(old.id)
                assert latest == change
                assert created.after == old

    run(exercise())


@pytest.mark.parametrize(
    "value", (nan, inf, -inf, -62_135_596_801.0, 253_402_300_800.0)
)
def test_save_facts_rejects_invalid_audit_time(
    fact_database: PgDatabase | None, value: float
) -> None:
    """非法审计时间不能留下事实或变更。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        target = fact(subject, "target")
        async with _fact_storage(scope, fact_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                with pytest.raises(ValueError, match="changed_at"):
                    await writer.save_facts((target,), changed_at=value)
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(target.id) is None
                assert await reader.list_fact_changes(target.id) == ()

    run(exercise())
