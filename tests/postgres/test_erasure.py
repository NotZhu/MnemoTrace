"""验证画像、访问强化及擦除路径。"""

from dataclasses import replace

import pytest

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.application.salience import reinforce_context_facts
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.ids import FactId, SessionId
from mnemotrace.domain.profile import Profile, ProfileEntry
from mnemotrace.domain.salience import FactSalience
from mnemotrace.domain.values import Label
from mnemotrace.ports.queries import FactFilter
from mnemotrace.ports.storage import CutoffTrigger, SessionUnavailable

from .conftest import PgDatabase
from .sample import fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_profile_requires_local_sources_and_monotonic_updates(
    database: PgDatabase,
) -> None:
    """拒绝缺失或跨空间的来源及旧快照，允许同刻修订。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            subject, sibling_subject = Entity(scope.id, "甲"), Entity(sibling.id, "乙")
            local = fact(subject, "local")
            foreign = fact(sibling_subject, "foreign")
            async with storage.session(sibling.id, write=True) as writer:
                await writer.upsert_entity(sibling_subject)
                await writer.save_facts((foreign,), changed_at=20.0)
            saved = Profile(
                scope.id, {"组": (ProfileEntry("画像", (local.id,)),)}, 30.0
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((local,), changed_at=20.0)
                await writer.save_profile(saved)
                for missing in (FactId("absent"), foreign.id):
                    invalid = saved.update(
                        {"组": (ProfileEntry("非法来源", (local.id, missing)),)}, 40.0
                    )
                    with pytest.raises(ValueError, match="事实须在绑定空间内已存在"):
                        await writer.save_profile(invalid)
            newer = saved.update({"组": (ProfileEntry("新版画像", (local.id,)),)}, 40.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.save_profile(newer)
            revised = newer.update(saved.data, newer.updated_at)
            async with storage.session(scope.id, write=True) as writer:
                with pytest.raises(ValueError, match="更新时间不能倒退"):
                    await writer.save_profile(saved)
                assert await writer.get_profile() == newer
                await writer.save_profile(revised)
            async with storage.session(scope.id) as reader:
                assert await reader.get_profile() == revised

    run(exercise())


@pytest.mark.parametrize("change", ["sensitive", "cutoff", "labels"])
def test_fact_changes_prune_only_dependent_profile_entries(
    database: PgDatabase,
    change: str,
) -> None:
    async def exercise() -> None:
        scope = space()
        subject = Entity.space_user(scope.id)
        first = fact(subject, "changed")
        second = fact(subject, "erased")
        third = fact(subject, "kept")
        changed = ProfileEntry("撤下共同引用", (first.id, third.id))
        erased = ProfileEntry("撤下硬删引用", (second.id,))
        kept = ProfileEntry("保留独立条目", (third.id,))
        profile = Profile(scope.id, {"basic": (changed, erased, kept)}, 30.0)
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id=scope.owner_id)
            await storage.create_space(scope)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((first, second, third), changed_at=20.0)
                await writer.save_profile(profile)
            async with storage.session(scope.id, write=True) as writer:
                revised = {
                    "sensitive": replace(first, sensitive=True),
                    "cutoff": first.close(35.0, 40.0),
                    "labels": replace(first, labels=(Label("technology"),)),
                }[change]
                await writer.save_facts((revised,), changed_at=40.0)
            async with storage.session(scope.id) as reader:
                assert await reader.get_profile() == profile.update(
                    {"basic": (erased, kept)}, 30.0
                )
            async with storage.session(scope.id, write=True) as writer:
                await writer.delete_facts((second.id,))
            async with storage.session(scope.id) as reader:
                assert await reader.get_profile() == profile.update(
                    {"basic": (kept,)}, 30.0
                )

    run(exercise())


def test_session_erasure_cleans_indexes_changes_and_dependent_profile(
    database: PgDatabase,
) -> None:
    """跨会话来源撤下整条事实，曾引用该来源的幸存历史同时清理。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            subject = Entity(scope.id, "甲")
            removed = message(scope, "same")
            kept = message(scope, "keep", session=SessionId("keep"))
            async with storage.session(sibling.id, write=True) as writer:
                await writer.register_session(removed.session_id)
                await writer.append_messages((replace(removed, space_id=sibling.id),))
            old = fact(
                subject,
                "old",
                text="删除标记",
                provenance=(removed.id, kept.id),
            ).close(18.0, 20.0)
            future = replace(
                fact(subject, "future", text="删除标记", provenance=(removed.id,)),
                valid_at=120.0,
            )
            previous = fact(
                subject, "survivor", text="保留标记", provenance=(removed.id,)
            )
            corrected = replace(previous, provenance=(kept.id,))
            survivor = replace(corrected, sensitive=True)
            manual = fact(subject, "manual", text="手工擦除")
            profile = Profile(
                scope.id,
                {
                    "较长的分组": (ProfileEntry("旧事实", (old.id,)),),
                    "短": (ProfileEntry("保留事实", (survivor.id,)),),
                },
                40.0,
            )
            reinforced = FactSalience().reinforce(25.0)
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.register_session(removed.session_id)
                await writer.register_session(kept.session_id)
                await writer.append_messages((removed, kept))
                await writer.save_facts((old, future, manual), changed_at=25.0)
                await writer.save_facts((previous,), changed_at=25.0)
                await writer.save_facts(
                    (corrected,),
                    changed_at=30.0,
                    support_groups={corrected.id: ((kept.id,),)},
                )
                await writer.save_facts((survivor,), changed_at=40.0)
                await writer.save_profile(profile)
                await writer.upsert_vectors(
                    tuple(
                        (item.id, (1.0,)) for item in (old, future, manual, survivor)
                    ),
                    embedding_space="v1",
                )
                assert await writer.get_salience((old.id, FactId("missing"))) == {
                    old.id: FactSalience(),
                }
                with pytest.raises(ValueError):
                    await writer.save_salience(
                        {old.id: reinforced, FactId("missing"): reinforced}
                    )
                assert await writer.get_salience((old.id,)) == {old.id: FactSalience()}
                await writer.save_salience(
                    {old.id: reinforced, survivor.id: reinforced, manual.id: reinforced}
                )
                assert len(await writer.list_fact_changes(old.id)) == 1
                assert await writer.get_profile() == profile
                assert await writer.get_profile(exclude_sensitive=True) is None
                assert (
                    await writer.get_salience((survivor.id,), exclude_sensitive=True)
                    == {}
                )
            async with storage.session(scope.id, write=True) as writer:
                deleted = await writer.delete_session_data(removed.session_id)
                assert deleted == frozenset((old.id, future.id))
                assert (
                    await writer.delete_session_data(removed.session_id) == frozenset()
                )
                assert await writer.get_profile() == profile.update(
                    {"较长的分组": (), "短": profile.data["短"]}, 40.0
                )
                await writer.delete_profile()
                await writer.delete_facts((manual.id, FactId("missing")))
                await writer.delete_facts((manual.id,))
                assert await writer.get_salience((old.id, future.id, manual.id)) == {}
                assert await writer.list_fact_changes(manual.id) == ()
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((removed.id, kept.id)) == {
                    kept.id: kept
                }
                assert await reader.get_profile() is None
                assert await reader.get_facts(
                    (old.id, future.id, manual.id, survivor.id)
                ) == {
                    survivor.id: survivor,
                }
                assert await reader.get_salience((survivor.id,)) == {
                    survivor.id: reinforced
                }
                assert await reader.list_fact_changes(old.id) == ()
                (change,) = await reader.list_fact_changes(survivor.id)
                assert (change.before, change.after) == (corrected, survivor)
                assert (
                    await reader.search_lexical(
                        "删除",
                        5,
                        filters=FactFilter(state="all"),
                        reference_time=50.0,
                    )
                    == ()
                )
                assert await reader.search_vectors(
                    (1.0,),
                    10,
                    filters=FactFilter(state="all"),
                    reference_time=50.0,
                    embedding_space="v1",
                ) == ((survivor.id, 1.0),)
            async with storage.session(sibling.id) as reader:
                assert await reader.get_messages((removed.id,)) == {
                    removed.id: replace(removed, space_id=sibling.id),
                }

    run(exercise())


def test_erasure_removes_joint_group_and_audit_while_trigger_fact_survives(
    database: PgDatabase,
) -> None:
    """触发事实有独立来源时仍存续，已擦除的截止审计和不完整证据组一起清理。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            original = message(scope, "original", session=SessionId("original"))
            erased = message(scope, "erased")
            joint = message(scope, "joint", session=SessionId("kept"))
            independent = message(scope, "independent", session=joint.session_id)
            old = replace(
                fact(subject, "old", provenance=(original.id,)), valid_at=10.0
            )
            closed = old.close(20.0, 25.0)
            cause = replace(
                fact(
                    subject, "cause", provenance=(erased.id, joint.id, independent.id)
                ),
                valid_at=23.0,
            )
            successor = replace(fact(subject, "successor"), valid_at=20.0)
            triggers = (
                CutoffTrigger(cause.id, 23.0, (erased.id, joint.id)),
                CutoffTrigger(successor.id, 20.0),
            )
            groups = ((erased.id, joint.id), (independent.id,))
            async with storage.session(scope.id, write=True) as writer:
                for session_id in (
                    original.session_id,
                    erased.session_id,
                    joint.session_id,
                ):
                    await writer.register_session(session_id)
                await writer.append_messages((original, erased, joint, independent))
                await writer.upsert_entity(subject)
                await writer.save_facts((old, cause), changed_at=23.0)
                await writer.save_facts(
                    (closed, cause, successor),
                    changed_at=25.0,
                    support_groups={cause.id: groups},
                    cutoff_triggers={old.id: triggers},
                )
                await writer.save_facts(
                    (cause,), changed_at=26.0, support_groups={cause.id: groups}
                )
                assert len(await writer.list_fact_changes(cause.id)) == 2
                assert await writer.get_fact_support_groups((cause.id,)) == {
                    cause.id: groups
                }
                assert (await writer.list_fact_changes(old.id))[
                    0
                ].cutoff_triggers == triggers
                await writer.upsert_vectors(((cause.id, (1.0,)),), embedding_space="v1")

            async with storage.session(scope.id, write=True) as writer:
                assert (
                    await writer.delete_session_data(erased.session_id) == frozenset()
                )
                assert await writer.get_fact_support_groups((cause.id,)) == {
                    cause.id: ((independent.id,),),
                }
                assert await writer.get_fact(cause.id) == replace(
                    cause, provenance=(independent.id,)
                )
                assert await writer.get_fact(old.id) == closed
                assert await writer.get_fact(successor.id) == successor
                assert await writer.get_messages(
                    (erased.id, joint.id, independent.id)
                ) == {
                    joint.id: joint,
                    independent.id: independent,
                }
                (remaining_change,) = await writer.list_fact_changes(old.id)
                assert remaining_change.before is None
                assert remaining_change.after == old
                hits = await writer.search_vectors(
                    (1.0,),
                    1,
                    filters=FactFilter(),
                    reference_time=25.0,
                    embedding_space="v1",
                )
                assert hits == ((cause.id, 1.0),)

    run(exercise())


class _Clock:
    def now(self) -> float:
        return 30.0


def test_session_erasure_rejects_late_reinforcement_after_new_session(
    database: PgDatabase,
) -> None:
    """删除和代际一起提交或回滚，旧会话及旧派生写回仍失效。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            source = message(scope, "source")
            item = fact(subject, "fact", provenance=(source.id,))
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.save_facts((item,), changed_at=15.0)
            async with storage.session(scope.id) as reader:
                original_token = await reader.get_erasure_token()
                assert await reader.get_messages((source.id,)) == {source.id: source}
                assert await reader.get_fact(item.id) == item

            with pytest.raises(RuntimeError, match="撤销会话删除"):
                async with storage.session(scope.id, write=True) as writer:
                    await writer.delete_session_data(source.session_id)
                    raise RuntimeError("撤销会话删除")
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_erasure_token() == original_token
                assert await writer.get_fact(item.id) == item
                await writer.delete_session_data(source.session_id)
                new_token = await writer.get_erasure_token()
                assert (
                    await writer.delete_session_data(source.session_id) == frozenset()
                )
                assert await writer.get_erasure_token() == new_token
            async with storage.session(scope.id, write=True) as writer:
                with pytest.raises(SessionUnavailable):
                    await writer.append_messages((source,))
                fresh_source = replace(source, session_id=SessionId("new-session"))
                await writer.register_session(fresh_source.session_id)
                await writer.append_messages((fresh_source,))
                await writer.save_facts((item,), changed_at=25.0)

            await reinforce_context_facts(
                storage,
                _Clock(),
                scope.id,
                (item.id,),
                reference_time=25.0,
                expected_erasure_token=original_token,
            )
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((source.id,)) == {
                    source.id: fresh_source
                }
                assert await reader.get_fact(item.id) == item
                assert await reader.get_salience((item.id,)) == {
                    item.id: FactSalience()
                }
                assert await reader.get_erasure_token() == new_token != original_token

    run(exercise())


def test_purge_rotates_generation_and_rejects_late_reinforcement(
    database: PgDatabase,
) -> None:
    """清空推进代际并保留维度登记，旧派生写入失效。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            source = message(scope, "source")
            item = fact(subject, "fact", provenance=(source.id,))
            profile = Profile(
                scope.id, {"组": (ProfileEntry("画像", (item.id,)),)}, 12.0
            )
            async with storage.session(scope.id, write=True) as writer:
                original_token = await writer.get_erasure_token()
                await writer.upsert_entity(subject)
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.save_facts((item,), changed_at=15.0)
                await writer.save_profile(profile)
                await writer.upsert_vectors(((item.id, (1.0,)),), embedding_space="v1")
                await writer.save_salience({item.id: FactSalience().reinforce(20.0)})
            with pytest.raises(RuntimeError, match="撤销清空"):
                async with storage.session(scope.id, write=True) as writer:
                    await writer.purge_space()
                    raise RuntimeError("撤销清空")
            async with storage.session(scope.id) as old:
                assert await old.get_erasure_token() == original_token
                assert await old.get_fact(item.id) == item
                async with storage.session(scope.id, write=True) as writer:
                    await writer.purge_space()
                assert await old.get_fact(item.id) == item
                assert await old.get_erasure_token() == original_token
            async with storage.session(scope.id) as reader:
                new_token = await reader.get_erasure_token()
                assert new_token != original_token
                assert await reader.get_space() == scope
                assert await reader.get_fact(item.id) is None
                assert await reader.get_entities((subject.id,)) == {}
                assert await reader.get_messages((source.id,)) == {}
                assert await reader.get_profile() is None
                assert await reader.list_fact_changes(item.id) == ()
                assert await reader.get_salience((item.id,)) == {}
                with pytest.raises(ValueError, match="维度不一致"):
                    await reader.search_vectors(
                        (1.0, 0.0),
                        1,
                        filters=FactFilter(),
                        reference_time=20.0,
                        embedding_space="v1",
                    )
            async with storage.session(scope.id, write=True) as writer:
                await writer.purge_space()
                assert await writer.get_erasure_token() not in (
                    original_token,
                    new_token,
                )
                await writer.upsert_entity(subject)
                with pytest.raises(SessionUnavailable):
                    await writer.append_messages((source,))
                fresh_source = replace(source, session_id=SessionId("new-session"))
                await writer.register_session(fresh_source.session_id)
                await writer.append_messages((fresh_source,))
                await writer.save_facts((item,), changed_at=25.0)
                with pytest.raises(ValueError, match="维度不一致"):
                    await writer.upsert_vectors(
                        ((item.id, (1.0, 0.0)),), embedding_space="v1"
                    )
                assert (
                    await writer.search_vectors(
                        (1.0,),
                        1,
                        filters=FactFilter(),
                        reference_time=25.0,
                        embedding_space="v1",
                    )
                    == ()
                )
            await reinforce_context_facts(
                storage,
                _Clock(),
                scope.id,
                (item.id,),
                reference_time=25.0,
                expected_erasure_token=original_token,
            )
            async with storage.session(scope.id) as reader:
                assert await reader.get_salience((item.id,)) == {
                    item.id: FactSalience()
                }

    run(exercise())
