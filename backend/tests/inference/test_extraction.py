"""验证抽取请求与固定模型响应的转换。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from mnemotrace_next.application.extraction import (
    EntityMention,
    SpaceUserMention,
    TimeEvidence,
    extract_facts,
)
from mnemotrace_next.domain.enums import Role
from mnemotrace_next.domain.ids import MessageId, SessionId, SpaceId
from mnemotrace_next.domain.message import Message
from mnemotrace_next.domain.values import Label, LiteralValue, Predicate
from mnemotrace_next.ports.inference import CompletionTruncated, InvalidStructuredOutput

from .sample import chat_response, configured_chat

_ZONE = timezone(timedelta(hours=8))
_NEW_YORK = ZoneInfo("America/New_York")
_SUGGESTED_AT = datetime(2026, 11, 15, 16, 30, tzinfo=UTC).timestamp()
_CONFIRMED_AT = datetime(2026, 11, 17, 2, tzinfo=UTC).timestamp()
_RECEIVED_AT = _CONFIRMED_AT + 60


def _message(identifier: str, role: Role, content: str, at: float | None) -> Message:
    return Message(
        MessageId(identifier),
        SpaceId("space"),
        SessionId("thread"),
        role,
        content,
        at,
        _RECEIVED_AT,
    )


def _batch(
    *, suggestion_time: float | None = _SUGGESTED_AT
) -> tuple[tuple[Message, ...], tuple[Message, ...]]:
    return (
        (
            _message(
                "proposal",
                Role.ASSISTANT,
                "建议从下周一开始默认用英文。",
                suggestion_time,
            ),
            _message("context", Role.USER, "还有时区设置。", _SUGGESTED_AT),
        ),
        (
            _message("confirmation", Role.USER, "对，就按这个来。", _CONFIRMED_AT),
            _message("ack", Role.ASSISTANT, "收到。", _RECEIVED_AT),
        ),
    )


def _fact(**overrides: object) -> dict[str, object]:
    return {
        "subject": {"kind": "user", "value": None},
        "predicate": "default_reply_language",
        "object": {"kind": "literal", "value": "英文"},
        "text": "用户从 2026 年 11 月 23 日开始默认用英文回复。",
        "valid_at_evidence": {
            "kind": "relative_date",
            "source_number": 1,
            "value": None,
            "days": 7,
        },
        "invalid_at_evidence": None,
        "source_numbers": [3, 1],
        "labels": ["user_preferences"],
        "sensitive": False,
        **overrides,
    }


@pytest.mark.parametrize(
    "suggestion_time",
    [_SUGGESTED_AT, None, 10**12],
    ids=["known-time", "legacy-unknown", "legacy-out-of-range"],
)
def test_extraction_anchors_relative_date_to_evidence(
    suggestion_time: float | None,
) -> None:
    """一次读取两段消息，把相对日期锚定提议，而证据包括确认。"""
    calls: list[httpx.Request] = []
    known_time = suggestion_time == _SUGGESTED_AT
    fact_text = (
        "用户从 2026 年 11 月 23 日开始默认用英文回复。"
        if known_time
        else "用户从提议中所说的下周一开始默认用英文回复。"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return chat_response(
            json.dumps({"facts": [_fact(text=fact_text)]}, ensure_ascii=False)
        )

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            (fact,) = await extract_facts(
                llm, *_batch(suggestion_time=suggestion_time), timezone=_ZONE
            )
        assert fact.subject == SpaceUserMention()
        assert fact.predicate == Predicate("default_reply_language")
        assert fact.object == LiteralValue("英文")
        assert fact.text == fact_text
        assert fact.provenance == (MessageId("proposal"), MessageId("confirmation"))
        assert fact.labels == (Label("user_preferences"),)
        assert not fact.sensitive and fact.invalid_at_evidence is None
        expected = datetime(2026, 11, 23, tzinfo=_ZONE).timestamp()
        assert fact.valid_at_evidence == TimeEvidence(
            "relative_date",
            MessageId("proposal"),
            expected if known_time else None,
        )
        (request,) = calls
        sent = json.loads(request.content)
        payload = json.loads(sent["messages"][-1]["content"])
        assert [row["number"] for row in payload["previous"]] == [1, 2]
        assert [row["number"] for row in payload["messages"]] == [3, 4]
        assert payload["previous"][0] == {
            "number": 1,
            "role": "assistant",
            "content": "建议从下周一开始默认用英文。",
            "reference_time": "2026-11-16T00:30:00+08:00" if known_time else None,
        }
        assert payload["messages"][0]["reference_time"] == "2026-11-17T10:00:00+08:00"

    asyncio.run(exercise())


def test_extraction_separates_message_time_and_calendar_end() -> None:
    """事实起点可取发言时刻，日期截止点按配置时区解释。"""
    response = _fact(
        object={"kind": "entity", "value": "星河项目"},
        text="用户从现在起负责星河项目，直到 12 月 1 日。",
        valid_at_evidence={
            "kind": "message_time",
            "source_number": 3,
            "value": None,
            "days": None,
        },
        invalid_at_evidence={
            "kind": "absolute",
            "source_number": 3,
            "value": "2026-12-01",
            "days": None,
        },
        source_numbers=[3],
        sensitive=True,
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return chat_response(json.dumps({"facts": [response]}, ensure_ascii=False))

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            (fact,) = await extract_facts(llm, *_batch(), timezone=_ZONE)
        assert fact.object == EntityMention("星河项目")
        assert fact.sensitive
        assert fact.valid_at_evidence == TimeEvidence(
            "message_time", MessageId("confirmation"), _CONFIRMED_AT
        )
        assert fact.invalid_at_evidence == TimeEvidence(
            "absolute",
            MessageId("confirmation"),
            datetime(2026, 12, 1, tzinfo=_ZONE).timestamp(),
        )

    asyncio.run(exercise())


def test_plan_date_and_unknown_start_remain_distinct() -> None:
    """计划从发言时成立，未知起点的历史陈述保留为空。"""
    messages = (
        _message("plan", Role.USER, "我打算 11 月 23 日去上海。", _CONFIRMED_AT),
        _message(
            "history", Role.USER, "以前默认用中文，记不清何时开始。", _RECEIVED_AT
        ),
    )
    plan_text = "用户计划于 2026 年 11 月 23 日去上海。"
    response = {
        "facts": [
            _fact(
                predicate="plans_to_visit",
                object={"kind": "entity", "value": "上海"},
                text=plan_text,
                valid_at_evidence={
                    "kind": "message_time",
                    "source_number": 1,
                    "value": None,
                    "days": None,
                },
                source_numbers=[1],
            ),
            _fact(
                object={"kind": "literal", "value": "中文"},
                text="用户以前默认用中文，起始时间未知。",
                valid_at_evidence=None,
                source_numbers=[2],
            ),
        ]
    }

    async def exercise() -> None:
        async with configured_chat(
            lambda _: chat_response(json.dumps(response, ensure_ascii=False))
        ) as llm:
            plan, historical = await extract_facts(llm, (), messages)
        assert plan.text == plan_text
        assert plan.valid_at_evidence == TimeEvidence(
            "message_time", MessageId("plan"), _CONFIRMED_AT
        )
        assert plan.invalid_at_evidence is None and historical.valid_at_evidence is None

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("reference_time", "clock_time"),
    [
        (datetime(2026, 3, 7, 12, tzinfo=UTC), "02:30"),
        (datetime(2026, 10, 31, 12, tzinfo=UTC), "01:30"),
    ],
    ids=["skipped-hour", "repeated-hour"],
)
@pytest.mark.parametrize("kind", ["absolute", "relative_date"])
def test_extraction_rejects_nonunique_local_time(
    kind: str, reference_time: datetime, clock_time: str
) -> None:
    """绝对时间与相对日期都须拒绝跳过或重复的本地时刻。"""
    target_date = reference_time.date() + timedelta(days=1)
    value = (
        f"{target_date.isoformat()}T{clock_time}" if kind == "absolute" else clock_time
    )
    response = _fact(
        valid_at_evidence={
            "kind": kind,
            "source_number": 1,
            "value": value,
            "days": None if kind == "absolute" else 1,
        },
        source_numbers=[1],
    )
    messages = (
        _message(
            "change",
            Role.USER,
            f"从 {target_date.isoformat()} {clock_time} 起默认用英文。",
            reference_time.timestamp(),
        ),
    )

    async def exercise() -> None:
        async with configured_chat(
            lambda _: chat_response(json.dumps({"facts": [response]}))
        ) as llm:
            with pytest.raises(InvalidStructuredOutput, match="时间的格式或依据无效"):
                await extract_facts(llm, (), messages, timezone=_NEW_YORK)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("value", "utc_hour"),
    [
        ("2026-11-01T00:30", 4),
        ("2026-11-01T01:30-04:00", 5),
        ("2026-11-01T01:30-05:00", 6),
    ],
    ids=["unique-local-time", "first-occurrence", "second-occurrence"],
)
def test_extraction_accepts_unique_and_explicit_offset_times(
    value: str, utc_hour: int
) -> None:
    """普通本地时刻与明确偏移的时刻都能解析到正确的 UTC 时间。"""
    response = _fact(
        valid_at_evidence={
            "kind": "absolute",
            "source_number": 1,
            "value": value,
            "days": None,
        },
        source_numbers=[1],
    )
    messages = (
        _message("change", Role.USER, f"从 {value} 起默认用英文。", _CONFIRMED_AT),
    )

    async def exercise() -> None:
        async with configured_chat(
            lambda _: chat_response(json.dumps({"facts": [response]}))
        ) as llm:
            (fact,) = await extract_facts(llm, (), messages, timezone=_NEW_YORK)
        assert fact.valid_at_evidence == TimeEvidence(
            "absolute",
            MessageId("change"),
            datetime(2026, 11, 1, utc_hour, 30, tzinfo=UTC).timestamp(),
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"source_numbers": [1, 99]}, "无效的消息编号"),
        ({"source_numbers": [1, 3, 3]}, "重复"),
        ({"source_numbers": [1, 2]}, "本批用户消息"),
        ({"source_numbers": [1, 4]}, "本批用户消息"),
        ({"source_numbers": [3]}, "该事实的证据"),
        ({"text": " "}, "非空文本"),
        (
            {
                "valid_at_evidence": {
                    "kind": "absolute",
                    "source_number": 3,
                    "value": "2026-W48",
                    "days": None,
                }
            },
            "时间的格式",
        ),
        (
            {
                "valid_at_evidence": {
                    "kind": "relative_date",
                    "source_number": 1,
                    "value": None,
                    "days": None,
                }
            },
            "时间的格式",
        ),
        (
            {
                "invalid_at_evidence": {
                    "kind": "absolute",
                    "source_number": 3,
                    "value": "2026-11-01",
                    "days": None,
                }
            },
            "早于截止点",
        ),
        ({"labels": ["fabricated"]}, "违反 enum 规则"),
    ],
    ids=[
        "unknown-reference",
        "duplicate-reference",
        "only-history",
        "assistant-only",
        "unreferenced-anchor",
        "blank-text",
        "week-only-date",
        "invalid-time",
        "invalid-interval",
        "schema",
    ],
)
def test_extraction_rejects_entire_response_when_one_fact_is_invalid(
    overrides: dict[str, object], error: str
) -> None:
    """任一候选引用或时间无效时，整批抽取失败。"""

    def respond(_: httpx.Request) -> httpx.Response:
        return chat_response(json.dumps({"facts": [_fact(), _fact(**overrides)]}))

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(InvalidStructuredOutput, match=error):
                await extract_facts(llm, *_batch())

    asyncio.run(exercise())


def test_extraction_accepts_no_facts_and_propagates_truncation() -> None:
    """合法空数组正常返回，截断不可被当成零事实。"""
    messages = (_message("thanks", Role.USER, "谢谢。", _RECEIVED_AT),)

    async def exercise() -> None:
        async with configured_chat(lambda _: chat_response('{"facts":[]}')) as llm:
            assert await extract_facts(llm, (), messages) == ()
        async with configured_chat(
            lambda _: chat_response('{"facts": [', finish_reason="length")
        ) as llm:
            with pytest.raises(CompletionTruncated):
                await extract_facts(llm, (), messages)

    asyncio.run(exercise())


def test_assistant_only_batch_skips_extraction() -> None:
    """只有助手发言时无需推理，即使前文有用户消息。"""
    previous, new = _batch()

    def respond(_: httpx.Request) -> httpx.Response:
        pytest.fail("只有助手发言的批次不应调用模型")

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            assert await extract_facts(llm, previous, new[1:]) == ()

    asyncio.run(exercise())
