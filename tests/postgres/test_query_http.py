"""验证任务、事实审计和会话原文的查询闭环。"""

from dataclasses import replace

import pytest
from psycopg.errors import ForeignKeyViolation

from mnemotrace.adapters.postgres import PostgresStorage
from mnemotrace.adapters.postgres.operations import PostgresOperations
from mnemotrace.application.operations import ClaimedOperation
from mnemotrace.application.remember import RememberResult
from mnemotrace.domain.entity import Entity
from mnemotrace.domain.ids import SessionId
from mnemotrace.ports.storage import CutoffTrigger, FactChange
from mnemotrace.server.v1.audit_schemas import FactChangeResponse

from .conftest import PgDatabase
from .sample import fact, message, resource_id, run, space
from .test_operations import _scope

pytestmark = pytest.mark.postgres


def test_operations_page_is_bound_to_space_and_filters(database: PgDatabase) -> None:
    """收录时间相同的任务仍按入队顺序列出，筛选游标可稳定续页。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            other_session = await scope.new_session()
            uploads = [
                await scope.submit(target, key, "合成消息")
                for key, target in (
                    ("first", session),
                    ("other", other_session),
                    ("last", session),
                )
            ]
            assert all(upload.status_code == 202 for upload in uploads)
            operation_ids = [upload.json()["operation_id"] for upload in uploads]
            async with scope.connections.connection() as conn:
                await conn.execute(
                    "UPDATE mnemotrace.remember_operations SET created_at = statement_timestamp() "
                    "WHERE space_id = %s",
                    (scope.space_id.value,),
                )
            path = scope.path + "/operations"
            all_items = (await scope.status(path)).json()
            assert [
                item["operation_id"] for item in all_items["items"]
            ] == operation_ids[::-1]
            assert all_items["next_cursor"] is None
            assert "message_ids" not in all_items["items"][0]
            options: dict[str, str | int] = {
                "limit": 1,
                "status": "queued",
                "session_id": session.rsplit("/", 1)[1],
            }
            first = await scope.client.get(path, headers=scope.headers, params=options)
            assert first.status_code == 200, first.text
            data = first.json()
            assert [item["operation_id"] for item in data["items"]] == [
                operation_ids[-1]
            ]
            cursor = data["next_cursor"]
            assert cursor
            next_page = await scope.client.get(
                path, headers=scope.headers, params={**options, "cursor": cursor}
            )
            assert [
                item["operation_id"] for item in next_page.json()["items"]
            ] == operation_ids[:1]
            assert next_page.json()["next_cursor"] is None
            wrong_filter = await scope.client.get(
                path,
                headers=scope.headers,
                params={**options, "status": "succeeded", "cursor": cursor},
            )
            assert (wrong_filter.status_code, wrong_filter.json()["code"]) == (
                422,
                "invalid_cursor",
            )
            sibling = await scope.client.post(
                "/v1/spaces", headers=scope.headers, json={"name": "另一合成空间"}
            )
            assert sibling.status_code == 201
            wrong_space = await scope.client.get(
                sibling.headers["Location"] + "/operations",
                headers=scope.headers,
                params={**options, "cursor": cursor},
            )
            assert (wrong_space.status_code, wrong_space.json()["code"]) == (
                422,
                "invalid_cursor",
            )
            unrelated_operation = await scope.status(
                sibling.headers["Location"]
                + f"/operations/{operation_ids[-1]}/fact-changes"
            )
            assert (
                unrelated_operation.status_code,
                unrelated_operation.json()["code"],
            ) == (404, "operation_not_found")
            denied = await scope.client.get(path, headers=scope.foreign_headers)
            assert (denied.status_code, denied.json()["code"]) == (
                404,
                "space_not_found",
            )
            empty = await scope.client.get(
                path,
                headers=scope.headers,
                params={"session_id": resource_id("missing")},
            )
            assert empty.json() == {"items": [], "next_cursor": None}

    run(exercise())


def test_audit_pages_follow_sensitivity_and_source_erasure(
    database: PgDatabase,
) -> None:
    """查询同批审计、截止来源及原文，擦除后只返回仍存续的记录。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session_path = await scope.new_session()
            source_path = await scope.new_session()
            messages_path = session_path + "/messages"
            assert (await scope.status(messages_path)).json() == {
                "items": [],
                "next_cursor": None,
            }
            accepted = await scope.submit(session_path, "job", "合成任务")
            operation_path = accepted.headers["Location"]
            audit_path = operation_path + "/fact-changes"
            pending = (await scope.status(audit_path)).json()
            assert pending == {
                "status": "queued",
                "items": [],
                "next_cursor": None,
            }

            queue = PostgresOperations(scope.connections)
            job = await queue.claim()
            assert (
                isinstance(job, ClaimedOperation)
                and job.operation_id == accepted.json()["operation_id"]
            )
            storage = PostgresStorage(scope.connections, owner_id=scope.owner)
            async with storage.session(scope.space_id, write=True) as writer:
                space = await writer.get_space()
                session = SessionId(session_path.rsplit("/", 1)[1])
                source_session = SessionId(source_path.rsplit("/", 1)[1])
                initial = message(
                    space, resource_id("initial"), session=session, at=30.0
                )
                backfill = message(
                    space, resource_id("past/event"), session=session, at=10.0
                )
                source = message(
                    space, resource_id("cutoff/source"), session=source_session, at=20.0
                )
                await writer.append_messages((initial, backfill, source))
                subject = await writer.upsert_entity(Entity(space.id, "合成事实"))
                old = replace(
                    fact(subject, resource_id("old"), provenance=(initial.id,)),
                    valid_at=10.0,
                )
                cause = replace(
                    fact(
                        subject,
                        resource_id("hidden"),
                        provenance=(source.id, backfill.id),
                    ),
                    valid_at=20.0,
                    sensitive=True,
                )
                visible = fact(subject, resource_id("visible"))
                await writer.save_facts((old,), changed_at=11.0)
                changes = await writer.save_facts(
                    (old.close(20.0, 25.0), visible, cause),
                    changed_at=25.0,
                    cutoff_triggers={
                        old.id: (CutoffTrigger(cause.id, 20.0, (source.id,)),)
                    },
                    support_groups={cause.id: ((source.id,), (backfill.id,))},
                )
                await queue.complete(
                    job, writer, RememberResult((initial.id, backfill.id), changes)
                )

            first_message = await scope.client.get(
                messages_path, headers=scope.headers, params={"limit": 1}
            )
            assert first_message.status_code == 200, first_message.text
            assert [entry["message_id"] for entry in first_message.json()["items"]] == [
                initial.id.value
            ]
            message_cursor = first_message.json()["next_cursor"]
            assert message_cursor
            next_message = await scope.client.get(
                messages_path, headers=scope.headers, params={"cursor": message_cursor}
            )
            assert [entry["message_id"] for entry in next_message.json()["items"]] == [
                backfill.id.value
            ]
            assert next_message.json()["next_cursor"] is None
            wrong_message_scope = await scope.client.get(
                source_path + "/messages",
                headers=scope.headers,
                params={"cursor": message_cursor},
            )
            assert wrong_message_scope.json()["code"] == "invalid_cursor"

            first = await scope.client.get(
                audit_path, headers=scope.headers, params={"limit": 1}
            )
            assert first.status_code == 200, first.text
            assert first.json()["status"] == "succeeded"
            assert [item["fact_id"] for item in first.json()["items"]] == [
                visible.id.value
            ]
            audit_cursor = first.json()["next_cursor"]
            assert audit_cursor
            second = await scope.client.get(
                audit_path, headers=scope.headers, params={"cursor": audit_cursor}
            )
            (old_change,) = second.json()["items"]
            assert old_change["fact_id"] == old.id.value
            assert old_change["before"]["invalid_at"] is None
            assert old_change["after"]["invalid_at"] == "1970-01-01T00:00:20Z"
            assert old_change["cutoff_triggers"] == []
            assert second.json()["next_cursor"] is None
            old_path = f"{scope.path}/facts/{old.id.value}/changes"
            wrong_audit_scope = await scope.client.get(
                old_path, headers=scope.headers, params={"cursor": audit_cursor}
            )
            assert wrong_audit_scope.json()["code"] == "invalid_cursor"
            first_fact_page = await scope.client.get(
                old_path, headers=scope.headers, params={"limit": 1}
            )
            assert first_fact_page.json()["items"] == [old_change]
            fact_cursor = first_fact_page.json()["next_cursor"]
            assert fact_cursor
            second_fact_page = await scope.client.get(
                old_path, headers=scope.headers, params={"cursor": fact_cursor}
            )
            assert second_fact_page.json()["items"][0]["before"] is None
            assert second_fact_page.json()["next_cursor"] is None
            all_changes = await scope.client.get(
                audit_path, headers=scope.headers, params={"include_sensitive": True}
            )
            hidden, _, closed = all_changes.json()["items"]
            assert hidden["fact_id"] == cause.id.value
            source_id = source.id.value
            assert closed["cutoff_triggers"] == [
                {
                    "fact_id": cause.id.value,
                    "valid_end": "1970-01-01T00:00:20Z",
                    "source_message_ids": [source_id],
                }
            ]
            original = await scope.status(scope.path + "/messages/" + source_id)
            assert original.json()["content"] == source.content
            assert (
                await scope.status(f"{scope.path}/facts/{cause.id.value}/changes")
            ).status_code == 404
            hide_history = await scope.client.get(
                audit_path,
                headers=scope.headers,
                params={"cursor": audit_cursor, "include_sensitive": True},
            )
            assert hide_history.json()["code"] == "invalid_cursor"
            for path in (audit_path, old_path, messages_path):
                response = await scope.client.get(path, headers=scope.foreign_headers)
                assert (response.status_code, response.json()["code"]) == (
                    404,
                    "space_not_found",
                )

            async with storage.session(scope.space_id, write=True) as writer:
                await writer.save_facts(
                    (replace(cause, sensitive=False),), changed_at=26.0
                )
            historical_hidden = await scope.status(
                f"{scope.path}/facts/{cause.id.value}/changes"
            )
            assert historical_hidden.status_code == 200
            assert historical_hidden.json()["items"] == []
            assert (await scope.status(old_path)).json()["items"][0]["cutoff_triggers"]

            erased = await scope.client.delete(source_path, headers=scope.headers)
            assert erased.status_code == 204
            assert (await scope.status(source_path + "/messages")).status_code == 404
            assert (
                await scope.status(scope.path + "/messages/" + source_id)
            ).status_code == 404
            assert (
                await scope.status(f"{scope.path}/facts/{cause.id.value}")
            ).status_code == 200
            assert [
                item["fact_id"]
                for item in (await scope.status(audit_path)).json()["items"]
            ] == [visible.id.value]
            (creation,) = (await scope.status(old_path)).json()["items"]
            assert creation["before"] is None
            removed = await scope.client.delete(
                f"{scope.path}/facts/{visible.id.value}", headers=scope.headers
            )
            assert removed.status_code == 204
            assert (await scope.status(audit_path)).json()["items"] == []
            assert (await scope.status(operation_path)).json()["fact_change_count"] == 3
            dropped_task = await scope.client.delete(
                session_path, headers=scope.headers
            )
            assert dropped_task.status_code == 204
            assert (await scope.status(audit_path)).status_code == 404
            purge = await scope.client.post(
                scope.path + "/purge",
                headers={**scope.headers, "Idempotency-Key": "purge"},
            )
            assert purge.status_code == 204
            assert (await scope.status(audit_path)).status_code == 404
            assert (await scope.status(old_path)).status_code == 404
            assert (await scope.status(messages_path)).status_code == 404
            assert (await scope.status(scope.path + "/operations")).json()[
                "items"
            ] == []

    run(exercise())


def test_change_response_preserves_unknown_validity_and_empty_sources() -> None:
    """无来源的触发项返回空数组，未知生效时刻仍为空。"""
    subject = Entity(space().id, "合成事实")
    original = fact(subject, "target")
    cutoff = 30.0
    updated = original.close(cutoff, cutoff)
    change = FactChange(
        1,
        cutoff,
        original,
        updated,
        (CutoffTrigger(fact(subject, "cause").id, cutoff),),
    )
    result = FactChangeResponse.from_change(change)
    assert result.changed_at.isoformat() == "1970-01-01T00:00:30+00:00"
    assert result.before is not None and result.before.valid_at is None
    assert result.after.invalid_at == result.changed_at
    assert result.cutoff_triggers[0].valid_end == result.changed_at
    assert result.cutoff_triggers[0].source_message_ids == []


def test_publish_requires_matching_audit_rows_and_operation_space(
    database: PgDatabase,
) -> None:
    """发布只关联同空间的现存审计，擦除任务仅解除关联。"""

    async def exercise() -> None:
        async with _scope(database) as scope:
            session = await scope.new_session()
            accepted = await scope.submit(session, "audit", "合成消息")
            audit_path = accepted.headers["Location"] + "/fact-changes"
            queue = PostgresOperations(scope.connections)
            job = await queue.claim()
            assert isinstance(job, ClaimedOperation)
            storage = PostgresStorage(scope.connections, owner_id=scope.owner)
            subject = Entity(scope.space_id, "合成事实")
            saved = fact(subject, resource_id("independent"))
            fact_path = f"{scope.path}/facts/{saved.id.value}/changes"

            with pytest.raises(RuntimeError, match="关联数量不一致"):
                async with storage.session(scope.space_id, write=True) as writer:
                    await writer.upsert_entity(subject)
                    (change,) = await writer.save_facts((saved,), changed_at=10.0)
                    missing = replace(change, cursor=change.cursor + 1)
                    await queue.complete(job, writer, RememberResult((), (missing,)))
            audit = (await scope.status(audit_path)).json()
            assert audit == {"status": "running", "items": [], "next_cursor": None}
            assert (await scope.status(fact_path)).status_code == 404

            async with storage.session(scope.space_id, write=True) as writer:
                await writer.upsert_entity(subject)
                changes = await writer.save_facts((saved,), changed_at=10.0)
                await queue.complete(job, writer, RememberResult((), changes))
            before = (await scope.status(fact_path)).json()
            assert (await scope.status(audit_path)).json()["items"] == before["items"]

            created = await scope.client.post(
                "/v1/spaces", headers=scope.headers, json={"name": "同账号另一空间"}
            )
            assert created.status_code == 201
            sibling = replace(scope, path=created.headers["Location"])
            sibling_session = await sibling.new_session()
            foreign = await sibling.submit(sibling_session, "audit", "合成原文")
            assert foreign.status_code == 202
            with pytest.raises(ForeignKeyViolation) as rejected:
                async with scope.connections.connection() as conn:
                    await conn.execute(
                        "UPDATE mnemotrace.fact_changes SET operation_id = %s "
                        "WHERE space_id = %s AND fact_id = %s",
                        (
                            foreign.json()["operation_id"],
                            scope.space_id.value,
                            saved.id.value,
                        ),
                    )
            assert (
                rejected.value.diag.constraint_name == "fact_changes_operation_in_space"
            )
            assert (
                await scope.client.delete(sibling_session, headers=scope.headers)
            ).status_code == 204
            assert (await scope.status(audit_path)).json()["items"] == before["items"]

            assert (
                await scope.client.delete(session, headers=scope.headers)
            ).status_code == 204
            assert (await scope.status(audit_path)).status_code == 404
            assert (await scope.status(fact_path)).json() == before
            async with scope.connections.connection() as conn:
                cursor = await conn.execute(
                    "SELECT space_id, operation_id FROM mnemotrace.fact_changes "
                    "WHERE space_id = %s AND fact_id = %s",
                    (scope.space_id.value, saved.id.value),
                )
                assert await cursor.fetchone() == (scope.space_id.value, None)

    run(exercise())
