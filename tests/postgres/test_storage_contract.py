"""验证 PostgreSQL 和内存替身共同遵守的存储契约。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from math import inf, nan
from typing import cast

import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import ProfileGroup
from mnemotrace.domain.ids import FactId
from mnemotrace.domain.space import Space
from mnemotrace.domain.values import EntityRef, Label, LiteralValue, Predicate
from mnemotrace.ports.queries import FactFilter
from mnemotrace.ports.storage import CutoffTrigger, SpaceUnavailable, Storage

from ..ports.fakes import FakeStorage
from .conftest import PgDatabase
from .sample import SESSION, fact, message, pool, run, space


@pytest.fixture(
    params=(False, pytest.param(True, marks=pytest.mark.postgres)),
    ids=("memory", "postgres"),
)
def storage_database(request: pytest.FixtureRequest) -> PgDatabase | None:
    """真库用例使用专用连接，内存用例可离线执行。"""
    if request.param:
        return cast(PgDatabase, request.getfixturevalue("database"))
    return None


@asynccontextmanager
async def _storage(scope: Space, database: PgDatabase | None) -> AsyncIterator[Storage]:
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
    storage_database: PgDatabase | None,
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
        async with _storage(scope, storage_database) as storage:
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
    storage_database: PgDatabase | None, value: float
) -> None:
    """非法审计时间不能留下事实或变更。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        target = fact(subject, "target")
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                with pytest.raises(ValueError, match="changed_at"):
                    await writer.save_facts((target,), changed_at=value)
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(target.id) is None
                assert await reader.list_fact_changes(target.id) == ()

    run(exercise())


def test_save_facts_records_profile_group_updates(
    storage_database: PgDatabase | None,
) -> None:
    """同一事实可以改变画像分组，变更记录保留前后快照。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity.space_user(scope.id)
        source = message(scope, "profile-source")
        original = replace(
            fact(subject, "profile-fact", provenance=(source.id,)),
            profile_groups=(ProfileGroup.PREFERENCES,),
        )
        updated = replace(original, profile_groups=(ProfileGroup.CONSTRAINTS,))
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(SESSION)
                await writer.append_messages((source,))
                await writer.upsert_entity(subject)
                (creation,) = await writer.save_facts((original,), changed_at=10.0)
                (change,) = await writer.save_facts((updated,), changed_at=20.0)
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(original.id) == updated
                assert (change.before, change.after) == (original, updated)
                assert await reader.list_fact_changes(original.id) == (change, creation)

    run(exercise())


def test_save_facts_rejects_invalid_updates_before_writing_batch(
    storage_database: PgDatabase | None,
) -> None:
    """缺少截止触发事实或固定字段冲突时，整批保存失败。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        other = Entity(scope.id, "云海项目")
        original = replace(fact(subject, "original"), valid_at=20.0)
        incoming = replace(original, id=FactId("incoming"))
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.upsert_entity(other)
                with pytest.raises(ValueError):
                    await writer.save_facts(
                        (original.close(25.0, 30.0), incoming),
                        changed_at=30.0,
                        cutoff_triggers={
                            original.id: (CutoffTrigger(FactId("missing"), 25.0),)
                        },
                    )
                assert await writer.get_facts((original.id, incoming.id)) == {}

                (creation,) = await writer.save_facts((original,), changed_at=30.0)
                for changed in (
                    replace(original, subject=EntityRef(other.id)),
                    replace(original, predicate=Predicate("偏好")),
                    replace(original, object=LiteralValue("咖啡")),
                    replace(original, text="修改原正文"),
                    replace(original, valid_at=21.0),
                    replace(original, recorded_at=11.0),
                ):
                    with pytest.raises(ValueError):
                        await writer.save_facts((incoming, changed), changed_at=40.0)

            async with storage.session(scope.id) as reader:
                assert await reader.get_facts((original.id, incoming.id)) == {
                    original.id: original
                }
                assert await reader.list_fact_changes(original.id) == (creation,)
                assert await reader.list_fact_changes(incoming.id) == ()

    run(exercise())


def test_fact_cutoff_can_only_move_earlier(storage_database: PgDatabase | None) -> None:
    """提前截止可以伴随元数据更新，首次获知时间保持固定。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        original = replace(fact(subject, "original"), valid_at=20.0)
        closed = original.close(valid_end=50.0, closed_at=60.0)
        shortened = replace(
            closed.shorten_validity(40.0), labels=(Label("food"),), sensitive=True
        )
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                (creation,) = await writer.save_facts((original,), changed_at=30.0)
                (cutoff,) = await writer.save_facts((closed,), changed_at=60.0)
                (latest,) = await writer.save_facts((shortened,), changed_at=70.0)
                for changed in (original, closed, replace(shortened, expired_at=70.0)):
                    with pytest.raises(ValueError):
                        await writer.save_facts((changed,), changed_at=80.0)
            async with storage.session(scope.id) as reader:
                assert await reader.get_fact(original.id) == shortened
                assert await reader.list_fact_changes(original.id) == (
                    latest,
                    cutoff,
                    creation,
                )
                assert (latest.before, latest.after) == (closed, shortened)
                assert latest.after.expired_at == closed.expired_at == 60.0

    run(exercise())


@pytest.mark.parametrize(
    ("query_vector", "candidate", "orthogonal_vector", "min_score"),
    (
        pytest.param((1.0, 0.0), (1.0, 0.0), (0.0, 1.0), 1.0, id="axis"),
        pytest.param((1.0, 1.0), (1.0, 1.0), (1.0, -1.0), 1.0, id="diagonal"),
        pytest.param(
            (1.0, 1.0, 0.0),
            (1.0, 0.0, 1.0),
            (0.0, 0.0, 1.0),
            0.5,
            id="recall-threshold",
        ),
    ),
)
def test_vector_search_uses_inclusive_cosine_scores_and_tie_order(
    storage_database: PgDatabase | None,
    query_vector: tuple[float, ...],
    candidate: tuple[float, ...],
    orthogonal_vector: tuple[float, ...],
    min_score: float,
) -> None:
    """等比例缩放不影响分数，阈值相等时命中，同分按事实标识排序。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        first = fact(subject, "a")
        last = fact(subject, "z")
        orthogonal = fact(subject, "orthogonal")
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((last, orthogonal, first), changed_at=10.0)
                await writer.upsert_vectors(
                    (
                        (last.id, tuple(0.5 * value for value in candidate)),
                        (orthogonal.id, orthogonal_vector),
                        (first.id, tuple(2.0 * value for value in candidate)),
                    ),
                    embedding_space="test-embedding",
                )
            async with storage.session(scope.id) as reader:
                for scale in (1.0, 3.0):
                    assert await reader.search_vectors(
                        tuple(scale * value for value in query_vector),
                        3,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space="test-embedding",
                        min_score=min_score,
                    ) == ((first.id, min_score), (last.id, min_score))

    run(exercise())


def test_vector_write_validates_batch_and_purge_preserves_dimension(
    storage_database: PgDatabase | None,
) -> None:
    """无效向量整批拒绝，清空空间后仍校验已登记维度。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "星河项目")
        first, second = fact(subject, "first"), fact(subject, "second")
        embedding_space = "test-embedding"
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((first, second), changed_at=10.0)
                await writer.upsert_vectors(
                    ((first.id, (1.0, 0.0)),), embedding_space=embedding_space
                )
                for invalid in ((), (0.0, 0.0), (nan, 1.0), (inf, 1.0), (1.0,)):
                    with pytest.raises(ValueError):
                        await writer.upsert_vectors(
                            ((first.id, (0.0, 1.0)), (second.id, invalid)),
                            embedding_space=embedding_space,
                        )
                assert await writer.search_vectors(
                    (1.0, 0.0),
                    2,
                    filters=FactFilter(),
                    reference_time=20.0,
                    embedding_space=embedding_space,
                ) == ((first.id, 1.0),)
                await writer.purge_space()
            async with storage.session(scope.id) as reader:
                with pytest.raises(ValueError):
                    await reader.search_vectors(
                        (1.0,),
                        2,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space=embedding_space,
                    )

    run(exercise())


def test_vector_dimension_limit_applies_before_registration(
    storage_database: PgDatabase | None,
) -> None:
    """超限向量无法登记嵌入空间，边界维度仍可写入并检索。"""

    async def exercise() -> None:
        scope = space()
        subject = Entity(scope.id, "甲")
        item = fact(subject, "vector-limit")
        at_limit = (1.0,) + (0.0,) * 15_999
        too_large = (*at_limit, 0.0)
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((item,), changed_at=20.0)
                with pytest.raises(ValueError, match="16000"):
                    await writer.upsert_vectors(
                        ((item.id, too_large),), embedding_space="limit"
                    )
                await writer.upsert_vectors(
                    ((item.id, at_limit),), embedding_space="limit"
                )
            async with storage.session(scope.id) as reader:
                with pytest.raises(ValueError, match="16000"):
                    await reader.search_vectors(
                        too_large,
                        1,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space="unregistered",
                    )
                assert await reader.search_vectors(
                    at_limit,
                    1,
                    filters=FactFilter(),
                    reference_time=20.0,
                    embedding_space="limit",
                ) == ((item.id, 1.0),)

    run(exercise())


def test_duplicate_create_preserves_space_and_missing_space_raises(
    storage_database: PgDatabase | None,
) -> None:
    """同 ID 创建失败时保留原数据，缺失空间在进入会话时报错。"""

    async def exercise() -> None:
        scope = space()
        async with _storage(scope, storage_database) as storage:
            async with storage.session(scope.id, write=True) as writer:
                registration = await writer.register_session(SESSION)
            with pytest.raises(ValueError):
                await storage.create_space(replace(scope, name="重复创建"))
            async with storage.session(scope.id) as reader:
                assert await reader.get_space() == scope
                assert await reader.get_session(SESSION) == registration
            with pytest.raises(SpaceUnavailable):
                async with storage.session(space().id):
                    pytest.fail("缺失空间进入了读会话")

    run(exercise())
