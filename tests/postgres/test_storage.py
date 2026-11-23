"""验证账号隔离、原文顺序、空间锁与连接生命周期。"""

import asyncio
from dataclasses import replace

import psycopg
import pytest
from psycopg import errors

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres.reader import PostgresReader
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.enums import Role
from mnemotrace.domain.ids import MessageId, SessionId, SpaceId
from mnemotrace.domain.profile import Profile, ProfileEntry
from mnemotrace.ports.storage import SessionUnavailable, Storage

from .conftest import PgDatabase
from .sample import SESSION, fact, message, pool, run, space

pytestmark = pytest.mark.postgres


def test_space_scope_message_replay_and_entity_identity(database: PgDatabase) -> None:
    """账号检查和所有关联查询使用同一个空间作用域。"""

    async def exercise() -> None:
        async with pool(database.runtime, max_size=1) as connections:
            owner: Storage = PostgresStorage(connections, owner_id="owner")
            other: Storage = PostgresStorage(connections, owner_id="other")
            primary, sibling, foreign = space(), space(), space("other")
            await owner.create_space(primary)
            await owner.create_space(sibling)
            await other.create_space(foreign)
            with pytest.raises(PermissionError):
                await other.create_space(space())
            with pytest.raises(ValueError):
                await owner.create_space(primary)
            for unavailable in (primary.id, SpaceId("missing")):
                with pytest.raises(LookupError, match="空间不可访问"):
                    async with other.session(unavailable, write=True):
                        pytest.fail("越权写会话已打开")
                with pytest.raises(LookupError, match="空间不可访问"):
                    async with other.session(unavailable):
                        pytest.fail("越权读会话已打开")

            first = message(primary, "m", at=20.0)
            replay = replace(first, recorded_at=90.0)
            second = message(primary, "second", at=10.0)
            new = message(primary, "uncommitted")
            other_session = message(primary, "elsewhere", session=SessionId("else"))
            original = Entity(primary.id, " Ａlice ", "person")
            renamed = Entity(primary.id, "ALICE")
            async with owner.session(primary.id, write=True) as writer:
                await writer.register_session(first.session_id)
                await writer.register_session(other_session.session_id)
                await writer.append_messages((first, replay, other_session, second))
                assert await writer.upsert_entity(original) == original
                assert await writer.upsert_entity(renamed) == renamed
                assert original.id == renamed.id
            async with owner.session(primary.id, write=True) as writer:
                await writer.append_messages((replay,))
                for conflict in (
                    replace(replay, content="改写"),
                    replace(replay, event_time=None),
                    replace(replay, role=Role.ASSISTANT),
                    replace(replay, session_id=other_session.session_id),
                    replace(replay, space_id=sibling.id),
                ):
                    with pytest.raises(ValueError):
                        await writer.append_messages((new, conflict))
                with pytest.raises(ValueError):
                    await writer.append_messages(
                        (new, replace(new, content="同批冲突"))
                    )
                with pytest.raises(ValueError):
                    await writer.upsert_entity(Entity(sibling.id, "Alice"))
            async with owner.session(primary.id) as reader:
                assert reader.space_id == primary.id
                assert await reader.get_space() == primary
                assert await reader.last_messages(SESSION, 2) == (first, second)
                assert await reader.last_messages(SESSION, 1) == (second,)
                assert await reader.get_messages(
                    (first.id, new.id, MessageId("missing"))
                ) == {
                    first.id: first,
                }
                assert await reader.get_entities(
                    (original.id, Entity(sibling.id, "Alice").id)
                ) == {
                    renamed.id: renamed,
                }
            for adapter, scope in ((owner, sibling), (other, foreign)):
                local = replace(first, space_id=scope.id, content="该空间原文")
                async with adapter.session(scope.id, write=True) as writer:
                    await writer.register_session(local.session_id)
                    await writer.append_messages((local,))
                async with adapter.session(scope.id) as reader:
                    assert await reader.get_messages((first.id, second.id)) == {
                        first.id: local
                    }
                    assert await reader.get_entities((original.id,)) == {}

    run(exercise())


def test_session_read_only_closes_and_rolls_back_on_cancel(
    database: PgDatabase,
) -> None:
    """检查数据库只读模式、异常退出和取消后的池复用。"""

    async def exercise() -> None:
        async with pool(database.runtime, max_size=1) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            async with storage.session(scope.id) as reader:
                assert isinstance(reader, PostgresReader)
                with pytest.raises(errors.ReadOnlySqlTransaction):
                    await reader._conn.execute(
                        "UPDATE mnemotrace.spaces SET purge_generation = 4 WHERE space_id = %s",
                        (scope.id.value,),
                    )
            with pytest.raises(RuntimeError, match="会话已关闭"):
                await reader.get_space()

            aborted = message(scope, "aborted")
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(aborted.session_id)
            with pytest.raises(RuntimeError, match="事务异常"):
                async with storage.session(scope.id, write=True) as writer:
                    await writer.append_messages((aborted,))
                    raise RuntimeError("事务异常")
            written = asyncio.Event()
            subject = Entity(scope.id, "甲")
            unpublished = fact(subject, "cancelled", provenance=(aborted.id,))

            async def cancellable() -> None:
                async with storage.session(scope.id, write=True) as writer:
                    await writer.append_messages((aborted,))
                    await writer.upsert_entity(subject)
                    await writer.save_facts((unpublished,), changed_at=20.0)
                    await writer.upsert_vectors(
                        ((unpublished.id, (1.0,)),), embedding_space="v1"
                    )
                    written.set()
                    await asyncio.Future[None]()

            task = asyncio.create_task(cancellable())
            await asyncio.wait_for(written.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_messages((aborted.id,)) == {}
                assert await writer.get_fact(unpublished.id) is None
                assert await writer.get_fact_support_groups((unpublished.id,)) == {}
                assert await writer.list_fact_changes(unpublished.id) == ()
                assert await writer.get_erasure_token() == "0"
                await writer.append_messages((aborted,))
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((aborted.id,)) == {aborted.id: aborted}

    run(exercise())


@pytest.mark.parametrize("purge", (False, True), ids=("erase-session", "purge-space"))
def test_empty_session_retirement_blocks_first_in_flight_write(
    database: PgDatabase, purge: bool
) -> None:
    """对话撤销阻止已准备但未提交的首次消息，空间仍可开新对话。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)
            pending = message(scope, "first")
            fresh = replace(pending, session_id=SessionId("fresh"))
            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(pending.session_id)
                before = await writer.get_erasure_token()
            async with storage.session(sibling.id, write=True) as writer:
                assert not await writer.has_session(pending.session_id)
                with pytest.raises(SessionUnavailable):
                    await writer.append_messages(
                        (replace(pending, space_id=sibling.id),)
                    )

            ready, erased = asyncio.Event(), asyncio.Event()

            async def delayed_publish() -> None:
                async with storage.session(scope.id) as reader:
                    assert await reader.has_session(pending.session_id)
                ready.set()
                await erased.wait()
                async with storage.session(scope.id, write=True) as writer:
                    assert not await writer.has_session(pending.session_id)
                    with pytest.raises(SessionUnavailable):
                        await writer.append_messages((pending,))

            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                tasks.create_task(delayed_publish())
                await ready.wait()
                async with storage.session(scope.id, write=True) as writer:
                    if purge:
                        await writer.purge_space()
                    else:
                        assert (
                            await writer.delete_session_data(pending.session_id)
                            == frozenset()
                        )
                    assert (await writer.get_erasure_token() != before) == purge
                erased.set()

            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_messages((pending.id,)) == {}
                await writer.register_session(fresh.session_id)
                await writer.append_messages((fresh,))
            async with storage.session(scope.id) as reader:
                assert await reader.last_messages(fresh.session_id, 1) == (fresh,)
                assert not await reader.has_session(pending.session_id)

    run(exercise())


def test_write_session_reports_failure_after_caught_sql_error(
    database: PgDatabase,
) -> None:
    """写会话报告被中止的事务，回滚后连接可复用。"""

    async def exercise() -> None:
        async with pool(database.runtime, max_size=1) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope = space()
            await storage.create_space(scope)
            subject = Entity(scope.id, "甲")
            source = fact(subject, "profile-source")
            async with storage.session(scope.id, write=True) as writer:
                await writer.upsert_entity(subject)
                await writer.save_facts((source,), changed_at=10.0)
                await writer.register_session(SESSION)

            entry = ProfileEntry("不可存储的字符\x00", (source.id,))
            invalid = Profile(scope.id, {"组": (entry,)}, 20.0)
            pending = message(scope, "pending")
            with pytest.raises(RuntimeError, match="写事务已中止"):
                async with storage.session(scope.id, write=True) as writer:
                    await writer.append_messages((pending,))
                    with pytest.raises(errors.UntranslatableCharacter):
                        await writer.save_profile(invalid)

            async with storage.session(scope.id, write=True) as writer:
                assert await writer.get_messages((pending.id,)) == {}
                await writer.append_messages((pending,))
            async with storage.session(scope.id) as reader:
                assert await reader.get_messages((pending.id,)) == {pending.id: pending}

    run(exercise())


def test_purge_waits_for_writer_without_blocking_sibling_space(
    database: PgDatabase,
) -> None:
    """数据库确认清空等待同空间写锁，兄弟空间可独立提交。"""

    async def exercise() -> None:
        async with pool(database.runtime) as connections:
            storage = PostgresStorage(connections, owner_id="owner")
            scope, sibling = space(), space()
            await storage.create_space(scope)
            await storage.create_space(sibling)

            async def purge() -> None:
                async with storage.session(scope.id, write=True) as writer:
                    await writer.purge_space()

            async with storage.session(scope.id, write=True) as writer:
                await writer.register_session(SESSION)
                await writer.append_messages((message(scope, "to-purge"),))
                token = await writer.get_erasure_token()
                clearing = asyncio.create_task(purge())
                async with await psycopg.AsyncConnection.connect(
                    database.admin, autocommit=True
                ) as observer:
                    async with asyncio.timeout(5):
                        while True:
                            waiting = await observer.execute("""
                                SELECT EXISTS (
                                    SELECT 1 FROM pg_stat_activity
                                    WHERE datname = current_database()
                                      AND wait_event_type = 'Lock' AND query LIKE '%FOR UPDATE%'
                                      AND pid <> pg_backend_pid()
                                )
                                """)
                            if await waiting.fetchone() == (True,):
                                break
                            if clearing.done():
                                await clearing
                                pytest.fail("清空未等待正在提交的写会话")
                            await asyncio.sleep(0.01)
                async with asyncio.timeout(5):
                    async with storage.session(sibling.id, write=True) as other:
                        await other.register_session(SESSION)
                        await other.append_messages((message(sibling, "kept"),))
            await asyncio.wait_for(clearing, timeout=5)
            async with storage.session(scope.id) as reader:
                assert await reader.last_messages(SESSION, 10) == ()
                assert await reader.get_erasure_token() != token
            async with storage.session(sibling.id) as reader:
                assert await reader.last_messages(SESSION, 10) == (
                    message(sibling, "kept"),
                )

    run(exercise())
