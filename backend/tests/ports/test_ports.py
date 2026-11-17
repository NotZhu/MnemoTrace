"""验证会话记忆端口可以通过内存替身组合使用。"""

import asyncio
from dataclasses import replace
from typing import assert_type

import pytest

from mnemotrace_next.application.salience import reinforce_context_facts
from mnemotrace_next.domain.digest import Digest
from mnemotrace_next.domain.entity import Entity
from mnemotrace_next.domain.enums import Role, Source
from mnemotrace_next.domain.fact import Fact
from mnemotrace_next.domain.ids import (
    DigestId,
    EntityId,
    FactId,
    MessageId,
    SessionId,
    SpaceId,
)
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.profile import Profile, ProfileEntry
from mnemotrace_next.domain.salience import FactSalience
from mnemotrace_next.domain.space import Space
from mnemotrace_next.domain.values import EntityRef, Label, LiteralValue, Predicate
from mnemotrace_next.ports.inference import LLM, Embedder
from mnemotrace_next.ports.queries import FactCursor, FactFilter, FactQuery
from mnemotrace_next.ports.reranker import Reranker
from mnemotrace_next.ports.runtime import Clock, IdGenerator
from mnemotrace_next.ports.storage import SpaceReader, Storage

from .fakes import (
    FakeClock,
    FakeEmbedder,
    FakeIdGenerator,
    FakeLLM,
    FakeReranker,
    FakeStorage,
)

_SPACE = Space(SpaceId("space"), "owner", "测试空间", 0.0)
_SUBJECT = Entity(_SPACE.id, "用户")
_FACT = Fact(
    id=FactId("fact"),
    space_id=_SPACE.id,
    subject=EntityRef(_SUBJECT.id),
    predicate=Predicate("喜欢"),
    object=LiteralValue("茶"),
    text="喜欢喝茶",
    valid_at=None,
    invalid_at=None,
    recorded_at=10.0,
    expired_at=None,
    provenance=(),
    labels=(),
    sensitive=False,
    source=Source.MANUAL,
)


def test_ports_smoke() -> None:
    """通过协议类型组装模型调用、空间写入、原文读取与检索。"""

    async def exercise(write: bool) -> None:
        storage: Storage = FakeStorage()
        llm: LLM = FakeLLM()
        embedder: Embedder = FakeEmbedder()
        reranker: Reranker = FakeReranker()
        clock: Clock = FakeClock()
        ids: IdGenerator = FakeIdGenerator()
        now = clock.now()
        space = Space(ids.new_space_id(), "owner", "测试空间", now)
        session_id = ids.new_session_id()
        completion = await llm.complete(system="返回原文", prompt="喜欢喝茶")
        structured = await llm.complete_json(
            system="返回结构化字段",
            prompt=completion.text,
            schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )
        message = Message(
            ids.new_message_id(),
            space.id,
            session_id,
            Role.USER,
            completion.text,
            None,
            now,
        )
        fact = replace(
            _FACT,
            id=ids.new_fact_id(),
            text=completion.text,
            recorded_at=now,
            provenance=(message.id,),
            source=Source.EXTRACTED,
        )
        digest = Digest(
            ids.new_digest_id(), space.id, session_id, "摘要", (message.id,), now, now
        )
        (vector,) = await embedder.embed((fact.text,))
        assert structured == {"text": message.content}
        assert completion.finish_reason is None
        assert len(vector) == embedder.dim
        assert await embedder.embed(()) == ()

        await storage.create_space(space)
        async with storage.session(space.id, write=True) as session:
            assert session.space_id == space.id
            await session.append_messages((message, message))
            assert await session.upsert_entity(_SUBJECT) == _SUBJECT
            await session.save_facts((fact,), changed_at=now)
            await session.upsert_vectors(
                ((fact.id, vector),), embedding_space=embedder.embedding_space
            )
            await session.save_digest(digest)

        async def check_reads(reader: SpaceReader) -> None:
            assert await reader.get_space() == space
            assert await reader.last_messages(session_id, limit=2) == (message,)
            assert await reader.get_fact(fact.id) == fact
            assert await reader.get_entities((_SUBJECT.id, EntityId("absent"))) == {
                _SUBJECT.id: _SUBJECT
            }
            assert await reader.get_messages((message.id, MessageId("absent"))) == {
                message.id: message
            }
            assert await reader.get_digest(digest.id) == digest
            assert (
                await reader.get_digest_for_group(session_id, (message.id,)) == digest
            )
            assert await reader.get_latest_digest(session_id) == digest

            filters = FactFilter()
            vector_hits = await reader.search_vectors(
                vector,
                1,
                filters=filters,
                reference_time=now,
                embedding_space=embedder.embedding_space,
            )
            lexical_hits = await reader.search_lexical(
                "喝茶", 1, filters=filters, reference_time=now
            )
            assert vector_hits == lexical_hits == ((fact.id, 1.0),)
            candidate_ids = (vector_hits[0][0], lexical_hits[0][0], FactId("absent"))
            assert await reader.get_facts(candidate_ids) == {fact.id: fact}
            assert await reader.get_salience(candidate_ids) == {fact.id: FactSalience()}

        async with storage.session(space.id, write=write) as reader_session:
            assert_type(reader_session, SpaceReader)
            await check_reads(reader_session)

        assert await reranker.rerank(
            "喝茶", ((FactId("other"), "看书"), (fact.id, fact.text)), 1
        ) == (fact.id,)

    asyncio.run(exercise(write=False))


def test_reinforce_context_facts_reads_latest_eligible_states() -> None:
    """重复调用累计强化，仅计入在请求时点有效且写回时可见的事实。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        clock = FakeClock()
        now = clock.now()
        reference_time = now - 10.0
        closed = replace(_FACT, id=FactId("closed")).close(
            reference_time, reference_time
        )
        future = replace(_FACT, id=FactId("future"), valid_at=reference_time + 5.0)
        sensitive = replace(_FACT, id=FactId("sensitive"), sensitive=True)
        missing = FactId("missing")
        profile = Profile(
            _SPACE.id, {"偏好": (ProfileEntry("喝茶", (_FACT.id,)),)}, now
        )
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((_FACT, closed, future, sensitive), changed_at=now)
            await session.save_profile(profile)
            token = await session.get_purge_token()

        fact_ids = (_FACT.id, closed.id, future.id, sensitive.id, missing)
        for expected in (1.5, 2.0):
            await reinforce_context_facts(
                storage,
                clock,
                _SPACE.id,
                (*fact_ids, _FACT.id),
                reference_time=reference_time,
                expected_purge_token=token,
                exclude_sensitive=True,
            )
            async with storage.session(_SPACE.id) as session:
                states = await session.get_salience(fact_ids)
                assert states == {
                    _FACT.id: FactSalience(expected, now),
                    closed.id: FactSalience(),
                    future.id: FactSalience(),
                    sensitive.id: FactSalience(),
                }
                assert await session.get_salience(fact_ids, exclude_sensitive=True) == {
                    fact_id: state
                    for fact_id, state in states.items()
                    if fact_id != sensitive.id
                }
                (created,) = await session.list_fact_changes(_FACT.id)
                assert created.before is None
                assert created.after == await session.get_fact(_FACT.id) == _FACT
                assert await session.get_profile() == profile

        async with storage.session(_SPACE.id, write=True) as session:
            with pytest.raises(ValueError, match="绑定空间"):
                await session.save_salience(
                    {_FACT.id: FactSalience(3.0, now), missing: FactSalience()}
                )
            assert await session.get_salience((_FACT.id,)) == {
                _FACT.id: FactSalience(2.0, now)
            }

    asyncio.run(exercise())


def test_reinforce_context_facts_after_effective_cutoff() -> None:
    """请求时选中的事实可以在有效期结束后写回显著度。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        clock = FakeClock()
        write_time = clock.now()
        reference_time = write_time - 10.0
        fact = _FACT.close(valid_end=reference_time + 5.0, closed_at=reference_time)
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((fact,), changed_at=reference_time)

        async with storage.session(_SPACE.id) as session:
            selected = await session.query_facts(
                FactQuery(), reference_time=reference_time
            )
            assert selected == (fact,)
            token = await session.get_purge_token()

        await reinforce_context_facts(
            storage,
            clock,
            _SPACE.id,
            tuple(selected_fact.id for selected_fact in selected),
            reference_time=reference_time,
            expected_purge_token=token,
        )
        async with storage.session(_SPACE.id) as session:
            assert await session.get_salience((fact.id,)) == {
                fact.id: FactSalience(1.5, write_time)
            }

    asyncio.run(exercise())


def test_message_replay_retains_first_recorded_at_and_append_order() -> None:
    """同 ID 重试沿用原收录时刻，原文字段冲突时拒绝整批追加。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        session_id = SessionId("session")
        original = Message(
            MessageId("original"),
            _SPACE.id,
            session_id,
            Role.USER,
            "记得喝茶",
            5.0,
            10.0,
        )
        following = replace(
            original, id=MessageId("following"), content="记下了", recorded_at=20.0
        )
        retry = replace(original, recorded_at=30.0)
        rejected = replace(following, id=MessageId("rejected"))
        async with storage.session(_SPACE.id, write=True) as session:
            await session.append_messages((original, following))
        async with storage.session(_SPACE.id, write=True) as session:
            await session.append_messages((retry,))
            for conflicting in (
                replace(retry, content="另一段原文"),
                replace(retry, event_time=6.0),
            ):
                with pytest.raises(ValueError, match="冲突"):
                    await session.append_messages((rejected, conflicting))

        async with storage.session(_SPACE.id) as session:
            assert await session.get_messages((original.id, rejected.id)) == {
                original.id: original
            }
            assert await session.last_messages(session_id, limit=3) == (
                original,
                following,
            )

    asyncio.run(exercise())


def test_fact_query_time_filters_and_cursor() -> None:
    """校验时间边界、过滤组合和同收录时刻的续页。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        message = Message(
            MessageId("source"),
            _SPACE.id,
            SessionId("session"),
            Role.USER,
            "今天开始喝茶",
            None,
            10.0,
        )
        food = Label("food")
        preferences = Label("user_preferences")
        ended = replace(
            _FACT,
            id=FactId("a"),
            valid_at=20.0,
            labels=(food,),
            provenance=(message.id,),
        ).close(100.0, 100.0)
        current = replace(
            _FACT,
            id=FactId("b"),
            valid_at=100.0,
            labels=(food, preferences),
            provenance=(message.id,),
        )
        unknown_end = replace(_FACT, id=FactId("c")).close(90.0, 100.0)
        future = replace(
            _FACT,
            id=FactId("d"),
            valid_at=120.0,
            labels=(Label("music"),),
            provenance=(message.id,),
        )
        unknown = replace(_FACT, id=FactId("e"), recorded_at=50.0)
        async with storage.session(_SPACE.id, write=True) as session:
            await session.append_messages((message,))
            await session.save_facts(
                (future, current, unknown, unknown_end, ended), changed_at=100.0
            )

        async with storage.session(_SPACE.id) as session:
            assert await session.query_facts(FactQuery(), reference_time=100.0) == (
                unknown,
                current,
            )
            assert await session.query_facts(
                FactQuery(filters=FactFilter(state="history")), reference_time=100.0
            ) == (ended, unknown_end)
            history_at = FactFilter(state="history", valid_at=100.0)
            assert await session.query_facts(
                FactQuery(filters=history_at), reference_time=70.0
            ) == (ended,)
            assert await session.facts_in_slot(
                _FACT.subject, _FACT.predicate, valid_at=100.0, limit=10
            ) == (current,)
            filters = FactFilter(state="all")
            first = await session.query_facts(
                FactQuery(filters=filters, limit=2), reference_time=100.0
            )
            assert first == (unknown, ended)
            cursor = FactCursor(first[-1].recorded_at, first[-1].id)
            assert await session.query_facts(
                FactQuery(filters=filters, after=cursor), reference_time=100.0
            ) == (current, unknown_end, future)
            scoped = FactFilter(
                state="all",
                subject=_FACT.subject,
                predicate=_FACT.predicate,
                labels=(food, preferences),
                provenance=(MessageId("other"), message.id),
            )
            assert await session.query_facts(
                FactQuery(filters=scoped), reference_time=100.0
            ) == (ended, current)

    asyncio.run(exercise())


def test_restricted_reads_hide_sensitive_facts_and_profile() -> None:
    """受限读取隐藏敏感事实和画像，图与索引在截取前完成过滤。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        embedder: Embedder = FakeEmbedder()
        await storage.create_space(_SPACE)
        target = Entity(_SPACE.id, "茶")
        edge = replace(_FACT, object=EntityRef(target.id), valid_at=20.0)
        sensitive = replace(
            edge, id=FactId("sensitive"), text="茶 茶", valid_at=40.0, sensitive=True
        )
        literal = replace(_FACT, id=FactId("literal"), valid_at=30.0)
        profile = Profile(
            _SPACE.id, {"偏好": (ProfileEntry(edge.text, (edge.id,)),)}, 50.0
        )
        async with storage.session(_SPACE.id, write=True) as session:
            await session.upsert_entity(_SUBJECT)
            await session.upsert_entity(target)
            await session.save_facts((edge, sensitive, literal), changed_at=50.0)
            await session.upsert_vectors(
                ((edge.id, (0.5, 0.0)), (sensitive.id, (1.0, 0.0))),
                embedding_space=embedder.embedding_space,
            )
            await session.save_profile(profile)
        async with storage.session(_SPACE.id) as session:
            assert await session.get_fact(sensitive.id) == sensitive
            assert await session.get_fact(sensitive.id, exclude_sensitive=True) is None
            assert await session.get_fact(edge.id, exclude_sensitive=True) == edge
            assert await session.get_facts(
                (edge.id, sensitive.id, FactId("absent")), exclude_sensitive=True
            ) == {edge.id: edge}
            assert await session.get_profile() == profile
            assert await session.get_profile(exclude_sensitive=True) is None
            assert await session.edges(
                _SUBJECT.id,
                reference_time=50.0,
                direction="out",
                exclude_sensitive=True,
                limit=1,
            ) == (edge,)
            assert (
                await session.edges(
                    target.id, reference_time=50.0, direction="out", limit=1
                )
                == ()
            )

            filters = FactFilter(exclude_sensitive=True)
            assert await session.search_lexical(
                "茶", 1, filters=filters, reference_time=50.0, min_score=1.0
            ) == ((edge.id, 1.0),)
            assert await session.search_vectors(
                (1.0, 0.0),
                1,
                filters=filters,
                reference_time=50.0,
                embedding_space=embedder.embedding_space,
                min_score=0.5,
            ) == ((edge.id, 0.5),)
            assert (
                await session.search_vectors(
                    (1.0, 0.0),
                    1,
                    filters=filters,
                    reference_time=50.0,
                    embedding_space=embedder.embedding_space,
                    min_score=0.6,
                )
                == ()
            )

    asyncio.run(exercise())


def test_fact_replacement_keeps_each_body_for_its_effective_time() -> None:
    """旧事实截止后保留原索引，新事实从同一时点生效。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        embedder: Embedder = FakeEmbedder()
        await storage.create_space(_SPACE)
        cutoff, published_at = 40.0, 50.0
        original = replace(_FACT, valid_at=20.0)
        closed = original.close(valid_end=cutoff, closed_at=published_at)
        successor = replace(
            original,
            id=FactId("successor"),
            object=LiteralValue("咖啡"),
            text="喜欢喝咖啡",
            valid_at=cutoff,
            recorded_at=published_at,
            supersedes=(original.id,),
        )
        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((original,), changed_at=10.0)
            await session.upsert_vectors(
                ((original.id, (1.0, 0.0)),), embedding_space=embedder.embedding_space
            )
        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((closed, successor), changed_at=published_at)
            await session.upsert_vectors(
                ((successor.id, (1.0, 0.0)),), embedding_space=embedder.embedding_space
            )
            await session.save_facts((closed, successor), changed_at=60.0)

        async with storage.session(_SPACE.id) as session:
            assert await session.get_facts(
                successor.supersedes, exclude_sensitive=True
            ) == {original.id: closed}
            (latest,) = await session.list_fact_changes(original.id, limit=1)
            (created,) = await session.list_fact_changes(
                original.id, after=latest.cursor
            )
            assert (latest.before, latest.after, latest.changed_at) == (
                original,
                closed,
                published_at,
            )
            assert (created.before, created.after, created.changed_at) == (
                None,
                original,
                10.0,
            )
            (successor_created,) = await session.list_fact_changes(successor.id)
            assert (successor_created.before, successor_created.after) == (
                None,
                successor,
            )

            for filters, expected in (
                (FactFilter(valid_at=cutoff - 1.0), closed),
                (FactFilter(valid_at=cutoff), successor),
                (FactFilter(state="history", valid_at=cutoff), closed),
            ):
                assert await session.query_facts(
                    FactQuery(filters=filters), reference_time=published_at
                ) == (expected,)
                assert await session.search_lexical(
                    "喜欢喝", 2, filters=filters, reference_time=published_at
                ) == ((expected.id, 1.0),)
                assert await session.search_vectors(
                    (1.0, 0.0),
                    2,
                    filters=filters,
                    reference_time=published_at,
                    embedding_space=embedder.embedding_space,
                ) == ((expected.id, 1.0),)

        async with storage.session(_SPACE.id, write=True) as session:
            await session.delete_facts((original.id,))
            assert await session.get_fact(successor.id) == replace(
                successor, supersedes=()
            )

    asyncio.run(exercise())


def test_save_facts_rejects_fixed_field_changes_before_writing_batch() -> None:
    """前驱须已存在，固定字段冲突时整批保存失败。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        original = replace(_FACT, valid_at=20.0)
        incoming = replace(original, id=FactId("incoming"), supersedes=(original.id,))
        async with storage.session(_SPACE.id, write=True) as session:
            with pytest.raises(ValueError, match="supersedes"):
                await session.save_facts((original, incoming), changed_at=30.0)
            assert await session.get_facts((original.id, incoming.id)) == {}

            await session.save_facts((original,), changed_at=30.0)
            for field, changed in (
                ("subject", replace(original, subject=EntityRef(EntityId("other")))),
                ("predicate", replace(original, predicate=Predicate("偏好"))),
                ("object", replace(original, object=LiteralValue("咖啡"))),
                ("text", replace(original, text="修改原正文")),
                ("valid_at", replace(original, valid_at=21.0)),
                ("recorded_at", replace(original, recorded_at=11.0)),
                ("supersedes", replace(original, supersedes=(incoming.id,))),
            ):
                with pytest.raises(ValueError, match=field):
                    await session.save_facts((incoming, changed), changed_at=40.0)

        async with storage.session(_SPACE.id) as session:
            assert await session.get_facts((original.id, incoming.id)) == {
                original.id: original
            }
            assert await session.list_fact_changes(incoming.id) == ()
            (creation,) = await session.list_fact_changes(original.id)
            assert (creation.before, creation.after) == (None, original)

    asyncio.run(exercise())


def test_fact_cutoff_can_only_move_earlier() -> None:
    """提前截止可以伴随元数据更新，首次获知时间保持固定。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        original = replace(_FACT, valid_at=20.0)
        closed = original.close(valid_end=50.0, closed_at=60.0)
        shortened = replace(
            closed.shorten_validity(40.0), labels=(Label("food"),), sensitive=True
        )
        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((original,), changed_at=30.0)
            await session.save_facts((closed,), changed_at=60.0)
            await session.save_facts((shortened,), changed_at=70.0)
            for field, changed in (
                ("invalid_at", original),
                ("invalid_at", closed),
                ("expired_at", replace(shortened, expired_at=70.0)),
            ):
                with pytest.raises(ValueError, match=field):
                    await session.save_facts((changed,), changed_at=80.0)

        async with storage.session(_SPACE.id) as session:
            assert await session.get_fact(original.id) == shortened
            latest, cutoff, created = await session.list_fact_changes(original.id)
            assert (latest.before, latest.after) == (closed, shortened)
            assert (cutoff.before, cutoff.after) == (original, closed)
            assert created.before is None

    asyncio.run(exercise())


def test_delete_session_data_removes_facts_and_source_history() -> None:
    """删除来源时同时撤下派生读模型和仍含该来源的修改记录。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        removed = Message(
            MessageId("removed"),
            _SPACE.id,
            SessionId("removed"),
            Role.USER,
            "喜欢喝茶、看书，计划参加下次活动",
            30.0,
            10.0,
        )
        kept = replace(
            removed,
            id=MessageId("kept"),
            session_id=SessionId("kept"),
            content="喜欢喝茶，也喜欢看书",
        )
        removed_digest = Digest(
            DigestId("removed"),
            _SPACE.id,
            removed.session_id,
            "茶",
            (removed.id,),
            10.0,
            20.0,
        )
        kept_digest = replace(
            removed_digest,
            id=DigestId("kept"),
            session_id=kept.session_id,
            text="喝茶、看书偏好",
            source_message_ids=(kept.id,),
        )
        withdrawn = replace(
            _FACT, source=Source.EXTRACTED, provenance=(removed.id, kept.id)
        ).close(valid_end=18.0, closed_at=18.0)
        scheduled = replace(
            _FACT,
            id=FactId("scheduled"),
            object=LiteralValue("活动"),
            text="计划参加下次活动",
            valid_at=120.0,
            source=Source.EXTRACTED,
            provenance=(removed.id,),
        )
        old = replace(
            _FACT,
            id=FactId("kept"),
            object=LiteralValue("书"),
            text="喜欢看书",
            source=Source.EXTRACTED,
            provenance=(removed.id,),
        )
        correction = replace(old, provenance=(kept.id,))
        current = replace(correction, labels=(Label("hobbies"),))
        profile = Profile(
            _SPACE.id, {"偏好": (ProfileEntry("喝茶", (withdrawn.id,)),)}, 25.0
        )
        reinforced = FactSalience().reinforce(25.0)

        async with storage.session(_SPACE.id, write=True) as session:
            await session.append_messages((removed, kept))
            await session.save_digest(removed_digest)
            await session.save_digest(kept_digest)
            await session.save_facts((withdrawn, scheduled, old), changed_at=20.0)
            await session.save_facts((correction,), changed_at=20.0)
            await session.save_facts((current,), changed_at=25.0)
            await session.save_salience(
                {withdrawn.id: reinforced, current.id: reinforced}
            )
            await session.save_profile(profile)

        async with storage.session(_SPACE.id, write=True) as session:
            assert await session.get_profile() == profile
            deleted_ids = await session.delete_session_data(removed.session_id)
            assert deleted_ids == frozenset({withdrawn.id, scheduled.id})
            assert await session.delete_session_data(removed.session_id) == frozenset()
            assert deleted_ids.intersection(profile.source_fact_ids)
            await session.delete_profile()

        async with storage.session(_SPACE.id) as session:
            assert await session.get_profile() is None
            assert await session.get_messages((removed.id, kept.id)) == {kept.id: kept}
            assert await session.get_latest_digest(removed.session_id) is None
            assert await session.get_latest_digest(kept.session_id) == kept_digest
            assert await session.get_facts(
                (withdrawn.id, scheduled.id, current.id)
            ) == {current.id: current}
            assert await session.get_salience((withdrawn.id, current.id)) == {
                current.id: reinforced
            }
            assert await session.list_fact_changes(withdrawn.id) == ()
            (retained_change,) = await session.list_fact_changes(current.id)
            assert (retained_change.before, retained_change.after) == (
                correction,
                current,
            )
            assert (
                await session.search_lexical(
                    "茶",
                    10,
                    filters=FactFilter(state="all"),
                    reference_time=30.0,
                )
                == ()
            )

        async with storage.session(_SPACE.id, write=True) as session:
            restored = replace(withdrawn, provenance=(kept.id,))
            await session.save_facts((restored,), changed_at=30.0)
            assert await session.get_salience((restored.id,)) == {
                restored.id: FactSalience()
            }
            await session.purge_space()
            assert await session.get_space() == _SPACE
            assert await session.get_messages((kept.id,)) == {}
            assert await session.get_fact(current.id) is None
            assert await session.get_latest_digest(kept.session_id) is None
            assert await session.list_fact_changes(current.id) == ()

    asyncio.run(exercise())


def test_purge_space_replaces_token_even_when_empty() -> None:
    """清空使先前保存的标记失效，普通事实写入保持标记稳定。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        async with storage.session(_SPACE.id) as session:
            original_token = await session.get_purge_token()

        async with storage.session(_SPACE.id, write=True) as session:
            await session.purge_space()

        async with storage.session(_SPACE.id) as session:
            token_after_empty_purge = await session.get_purge_token()
            assert token_after_empty_purge != original_token
            assert await session.get_space() == _SPACE

        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((_FACT,), changed_at=10.0)
            await session.save_salience({_FACT.id: FactSalience().reinforce(10.0)})
            assert await session.get_purge_token() == token_after_empty_purge
            await session.purge_space()

        async with storage.session(_SPACE.id) as session:
            assert await session.get_purge_token() not in {
                original_token,
                token_after_empty_purge,
            }
            assert await session.get_fact(_FACT.id) is None

        async with storage.session(_SPACE.id, write=True) as session:
            await session.save_facts((_FACT,), changed_at=11.0)
        await reinforce_context_facts(
            storage,
            FakeClock(),
            _SPACE.id,
            (_FACT.id,),
            reference_time=10.0,
            expected_purge_token=token_after_empty_purge,
        )
        async with storage.session(_SPACE.id) as session:
            assert await session.get_salience((_FACT.id,)) == {_FACT.id: FactSalience()}

    asyncio.run(exercise())


def test_latest_digest_uses_append_order_and_skips_missing_sources() -> None:
    """按追加位置选择摘要，来源缺失时整份摘要不可读。"""

    async def exercise() -> None:
        storage: Storage = FakeStorage()
        await storage.create_space(_SPACE)
        session_id = SessionId("session")
        first = Message(
            MessageId("first"), _SPACE.id, session_id, Role.USER, "先记这条", 30.0, 30.0
        )
        second = replace(
            first,
            id=MessageId("second"),
            content="后来补一句",
            event_time=20.0,
            recorded_at=20.0,
        )
        first_digest = Digest(
            DigestId("first"), _SPACE.id, session_id, "第一段", (first.id,), 30.0, 100.0
        )
        latest = replace(
            first_digest,
            id=DigestId("a"),
            text="后一段",
            source_message_ids=(second.id,),
            updated_at=50.0,
        )
        tied = replace(
            latest, id=DigestId("b"), source_message_ids=(first.id, second.id)
        )
        stale = replace(tied, id=DigestId("0"), updated_at=40.0)
        orphaned = replace(
            latest,
            id=DigestId("orphaned"),
            text="含已缺失原文",
            source_message_ids=(second.id, MessageId("missing")),
            updated_at=200.0,
        )
        async with storage.session(_SPACE.id, write=True) as session:
            await session.append_messages((first, second, first))
            await session.save_digest(first_digest)
            await session.save_digest(tied)
            await session.save_digest(stale)
            await session.save_digest(latest)
            await session.save_digest(orphaned)
        async with storage.session(_SPACE.id) as session:
            assert await session.last_messages(session_id, limit=2) == (first, second)
            assert await session.last_messages(session_id, limit=1) == (second,)
            assert await session.get_digest(orphaned.id) is None
            assert (
                await session.get_digest_for_group(
                    session_id, orphaned.source_message_ids
                )
                is None
            )
            assert await session.get_latest_digest(session_id) == latest

    asyncio.run(exercise())
