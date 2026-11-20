"""验证摘要、画像、访问强化及擦除路径。"""

from dataclasses import replace

import psycopg
import pytest

from mnemotrace_next.adapters.postgres import PostgresStorage
from mnemotrace_next.application.salience import reinforce_context_facts
from mnemotrace_next.domain.digest import Digest
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.ids import DigestId, FactId, MessageId, SessionId
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.profile import Profile, ProfileEntry
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.ports.queries import FactFilter
from mnemotrace_next.ports.storage import CutoffTrigger, SessionUnavailable

from .conftest import PgDatabase
from .sample import SESSION, fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def _digest(identifier: str, source: Message, *, updated_at: float = 30.0) -> Digest:
    return Digest(
        DigestId(identifier),
        source.space_id,
        source.session_id,
        "摘要",
        (source.id,),
        10.0,
        updated_at,
    )


def test_digest_validates_sources_and_selects_by_append_position(
    database: PgDatabase,
) -> None:
    """摘要组保序，最近候选优先覆盖后追加的原文。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            first, second = (
                message(scope, "first", at=30.0),
                message(scope, "second", at=20.0),
            )
            out_of_session = message(scope, "wrong-session", session=SessionId("other"))
            foreign = message(sibling, "foreign")
            async with storage.session(sibling.id, write=True) as writer:
                await writer.register_session(foreign.session_id)
                await writer.append_messages((foreign,))
            first_digest = _digest("first", first, updated_at=100.0)
            latest = _digest("a", second, updated_at=50.0)
            tied = replace(
                latest, id=DigestId("b"), source_message_ids=(first.id, second.id)
            )
            stale = replace(latest, id=DigestId("0"), updated_at=40.0)
            same_group = replace(latest, id=DigestId("z"))
            orphaned = replace(
                latest,
                id=DigestId("orphan"),
                updated_at=1000.0,
                source_message_ids=(second.id, MessageId("missing")),
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(first.session_id)
                await writer.register_session(out_of_session.session_id)
                await writer.append_messages((first, second, first, out_of_session))
                for digest in (first_digest, latest, tied, stale, same_group):
                    await writer.save_digest(digest)
                for invalid in (
                    orphaned,
                    replace(latest, source_message_ids=(out_of_session.id,)),
                    replace(latest, source_message_ids=(foreign.id,)),
                    replace(latest, space_id=sibling.id),
                ):
                    with pytest.raises(ValueError):
                        await writer.save_digest(invalid)
                latest = latest.update("修订摘要", 50.0)
                await writer.save_digest(latest)
            # 模拟已有的来源残缺记录，验证三种读取在数据库侧省略它。
            async with await psycopg.AsyncConnection.connect(database.admin) as admin:
                await admin.execute(
                    """
                    INSERT INTO mnemotrace_next.digests
                        (space_id, digest_id, session_id, text, source_message_ids,
                         last_message_seq, created_at, updated_at)
                    VALUES (%s, %s, %s, '残缺摘要', %s, 10000000, 10, 1000)
                    """,
                    (
                        scope.id.value,
                        orphaned.id.value,
                        SESSION.value,
                        [item.value for item in orphaned.source_message_ids],
                    ),
                )
            async with storage.session(scope.id) as reader:
                assert await reader.get_digest(latest.id) == latest
                assert (
                    await reader.get_digest_for_group(SESSION, (second.id, second.id))
                    == latest
                )
                assert (
                    await reader.get_digest_for_group(SESSION, (first.id, second.id))
                    == tied
                )
                assert (
                    await reader.get_digest_for_group(SESSION, (second.id, first.id))
                    is None
                )
                assert await reader.get_digest(orphaned.id) is None
                assert (
                    await reader.get_digest_for_group(
                        SESSION, orphaned.source_message_ids
                    )
                    is None
                )
                assert await reader.get_latest_digest(SESSION) == latest

    run(exercise())


def test_digest_same_id_preserves_group_and_rejects_stale_updates(
    database: PgDatabase,
) -> None:
    """摘要同 ID 固定分组，允许同刻修订并拒绝旧快照。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            first = message(scope, "first")
            second = message(scope, "second")
            other = message(scope, "other", session=SessionId("other"))
            initial = replace(
                _digest("stable", first), source_message_ids=(first.id, second.id)
            )
            latest = initial.update("新摘要", 50.0)
            revised = latest.update("同刻修订", latest.updated_at)
            reverse_group = replace(
                latest, id=DigestId("reverse"), source_message_ids=(second.id, first.id)
            )
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(first.session_id)
                await writer.register_session(other.session_id)
                await writer.append_messages((first, second, other))
                await writer.save_digest(initial)
                await writer.save_digest(latest)
                for invalid in (
                    initial,
                    replace(latest, created_at=20.0),
                    replace(
                        latest, source_message_ids=reverse_group.source_message_ids
                    ),
                    replace(
                        latest,
                        session_id=other.session_id,
                        source_message_ids=(other.id,),
                    ),
                ):
                    with pytest.raises(ValueError, match="同 ID 的摘要"):
                        await writer.save_digest(invalid)
                assert await writer.get_digest(latest.id) == latest
                await writer.save_digest(revised)
                await writer.save_digest(reverse_group)
            async with storage.session(scope.id) as reader:
                assert (
                    await reader.get_digest_for_group(
                        SESSION, initial.source_message_ids
                    )
                    == revised
                )
                assert (
                    await reader.get_digest_for_group(
                        SESSION, reverse_group.source_message_ids
                    )
                    == reverse_group
                )

    run(exercise())


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
                await writer.save_digest(_digest("removed", removed))
                await writer.save_digest(_digest("kept", kept))
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
                assert await writer.get_profile() is None
                await writer.delete_profile()
                await writer.delete_facts((manual.id, FactId("missing")))
                await writer.delete_facts((manual.id,))
                assert await writer.get_salience((old.id, future.id, manual.id)) == {}
                assert await writer.list_fact_changes(manual.id) == ()
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((removed.id, kept.id)) == {
                    kept.id: kept
                }
                assert await reader.get_latest_digest(removed.session_id) is None
                assert await reader.get_latest_digest(kept.session_id) == _digest(
                    "kept", kept
                )
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
            digest = _digest("digest", source)
            profile = Profile(
                scope.id, {"组": (ProfileEntry("画像", (item.id,)),)}, 12.0
            )
            async with storage.session(scope.id, write=True) as writer:
                original_token = await writer.get_erasure_token()
                await writer.upsert_entity(subject)
                await writer.register_session(source.session_id)
                await writer.append_messages((source,))
                await writer.save_facts((item,), changed_at=15.0)
                await writer.save_digest(digest)
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
                assert await reader.get_entity(subject.id) is None
                assert await reader.get_messages((source.id,)) == {}
                assert await reader.get_digest(digest.id) is None
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
