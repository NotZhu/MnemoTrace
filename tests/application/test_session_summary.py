"""验证会话滚动摘要的分批输入和生成契约。"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import httpx
import pytest

from mnemotrace.application.session_summary import (
    generate_session_summary,
    prepare_summary_batch,
)
from mnemotrace.domain.enums import Role
from mnemotrace.domain.ids import MessageId, SessionId, SpaceId
from mnemotrace.domain.message import Message
from mnemotrace.ports.inference import (
    LLM,
    CompletionRefused,
    CompletionTruncated,
    InvalidStructuredOutput,
)
from tests.inference.sample import CHAT_ENV, chat_response, configured_chat
from tests.ports.fakes import FakeLLM


def _message(seq: int, content: str, role: Role = Role.USER) -> tuple[int, Message]:
    return seq, Message(
        id=MessageId(f"m-{seq}"),
        space_id=SpaceId("workspace"),
        session_id=SessionId("conversation"),
        role=role,
        content=content,
        event_time=None,
        recorded_at=100.0,
    )


async def _messages(*rows: tuple[int, Message]) -> AsyncIterator[tuple[int, Message]]:
    for row in rows:
        yield row


class _SummaryLLM(FakeLLM):
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[tuple[str, str]] = []

    async def _structured_response(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append((system, prompt))
        return {"summary": self.summary}


def test_summary_updates_from_previous_text_and_ordered_messages() -> None:
    llm = _SummaryLLM("用户确认采用第二版。")

    async def run() -> str:
        batch = await prepare_summary_batch(
            previous_summary="用户正在比较两个版本。",
            messages=_messages(
                _message(11, "建议用第二版。", Role.ASSISTANT),
                _message(15, "确认用第二版。"),
            ),
        )
        assert batch is not None
        assert batch.covered_through_seq == 15
        return await generate_session_summary(llm, batch)

    assert asyncio.run(run()) == "用户确认采用第二版。"
    [(_, prompt)] = llm.calls
    assert json.loads(prompt) == {
        "previous_summary": "用户正在比较两个版本。",
        "messages": [
            {"role": "assistant", "content": "建议用第二版。"},
            {"role": "user", "content": "确认用第二版。"},
        ],
    }


@pytest.mark.parametrize(
    ("mode", "first_invalid"),
    [
        ("json_schema", None),
        ("json_object", '{"summary":null}'),
        ("prompt", '```json\n{"summary":"摘要"}\n```'),
    ],
)
def test_summary_respects_configured_json_mode_and_request_budget(
    mode: str, first_invalid: str | None
) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        response_format = payload.get("response_format")
        if mode == "json_schema":
            assert (
                response_format is not None and response_format["type"] == "json_schema"
            )
            assert response_format["json_schema"]["strict"] is True
            assert response_format["json_schema"]["schema"] == {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            }
        elif mode == "json_object":
            assert response_format == {"type": "json_object"}
        else:
            assert "response_format" not in payload

        system, prompt = (message["content"] for message in payload["messages"])
        format_length = (
            len(json.dumps(response_format, ensure_ascii=False))
            if response_format
            else 0
        )
        assert 11_700 < len(system + prompt) + format_length <= 12_000
        if len(requests) == 1 and first_invalid is not None:
            return chat_response(first_invalid)
        return chat_response('{"summary":"摘要"}')

    async def run() -> None:
        batch = await prepare_summary_batch(
            previous_summary=None, messages=_messages(_message(1, "忆" * 16_000))
        )
        assert batch is not None
        async with configured_chat(
            respond, {**CHAT_ENV, "MNEMOTRACE_LLM_JSON_MODE": mode}
        ) as llm:
            assert await generate_session_summary(llm, batch) == "摘要"
        assert len(requests) == (1 if first_invalid is None else 2)

    asyncio.run(run())


def test_input_at_budget_is_kept_whole_and_next_message_waits() -> None:
    llm = _SummaryLLM("稍后继续。")
    batch_limit = 11_744

    async def run() -> None:
        async def generate(*rows: tuple[int, Message]) -> int:
            batch = await prepare_summary_batch(
                previous_summary='引用"上次结论"\n继续。', messages=_messages(*rows)
            )
            assert batch is not None
            await generate_session_summary(llm, batch)
            return batch.covered_through_seq

        await generate(_message(3, "忆"))
        system, prompt = llm.calls[0]
        content = "忆" * (batch_limit - len(system + prompt) + 1)
        assert await generate(_message(3, content), _message(9, "留到下批。")) == 3
        system, prompt = llm.calls[1]
        assert len(system + prompt) == batch_limit
        assert json.loads(prompt)["messages"] == [{"role": "user", "content": content}]

    asyncio.run(run())


def test_oversized_message_waits_then_keeps_balanced_ends_in_its_own_batch() -> None:
    llm = _SummaryLLM("保留首尾。")
    long_content = "开始\n" + '"\\\t忆' * 8_000 + "\n结束"
    oversized = _message(7, long_content)

    async def run() -> None:
        first = await prepare_summary_batch(
            previous_summary=None,
            messages=_messages(
                _message(4, "第一条。"), oversized, _message(9, "稍后。")
            ),
        )
        assert first is not None and first.covered_through_seq == 4
        previous = await generate_session_summary(llm, first)
        second = await prepare_summary_batch(
            previous_summary=previous,
            messages=_messages(oversized, _message(9, "稍后。")),
        )
        assert second is not None and second.covered_through_seq == 7
        await generate_session_summary(llm, second)

    asyncio.run(run())
    _, prompt = llm.calls[1]
    [row] = json.loads(prompt)["messages"]
    head, marker, tail = row["content"].partition("\n[中间内容已省略]\n")
    assert marker and head and tail
    assert head == long_content[: len(head)]
    assert tail == long_content[-len(tail) :]
    assert abs(len(head) - len(tail)) <= 1
    assert oversized[1].content == long_content


def test_no_pending_messages_produce_no_batch() -> None:
    assert (
        asyncio.run(
            prepare_summary_batch(previous_summary="旧摘要", messages=_messages())
        )
        is None
    )


async def _generate_one(llm: LLM) -> str:
    batch = await prepare_summary_batch(
        previous_summary=None, messages=_messages(_message(1, "近况。"))
    )
    assert batch is not None
    return await generate_session_summary(llm, batch)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", ""), (" \n\t", ""), ("忆" * 2_000, "忆" * 2_000)],
    ids=["empty", "blank", "max-length"],
)
def test_summary_accepts_empty_or_maximum_length_text(text: str, expected: str) -> None:
    llm = _SummaryLLM(text)
    assert asyncio.run(_generate_one(llm)) == expected


@pytest.mark.parametrize(
    "response",
    [
        '```json\n{"summary":"摘要"}\n```',
        '[{"summary":"摘要"}]',
        '{"summary":"摘要"} {"summary":"另一份"}',
        '{"summary":"第一份","summary":"第二份"}',
        '{"summary":"摘要","other":"额外字段"}',
        '{"summary":null}',
        '{"summary":"\\ud800"}',
        '{"summary":"\\u0000"}',
        '{"summary":"\\u0001"}',
        json.dumps({"summary": "忆" * 2_001}),
    ],
    ids=[
        "code-fence",
        "array",
        "two-objects",
        "duplicate-key",
        "extra-key",
        "null",
        "unpaired-surrogate",
        "null-byte",
        "control-character",
        "over-length",
    ],
)
def test_summary_rejects_invalid_output_in_one_completion(response: str) -> None:
    calls = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return chat_response(response)

    async def run() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(InvalidStructuredOutput):
                await _generate_one(llm)

    asyncio.run(run())
    assert calls == 1


@pytest.mark.parametrize(
    "error_type", [CompletionRefused, CompletionTruncated, TimeoutError]
)
def test_summary_propagates_completion_errors(error_type: type[Exception]) -> None:
    class FailedLLM(FakeLLM):
        async def _structured_response(
            self, *, system: str, prompt: str, schema: Mapping[str, object]
        ) -> Mapping[str, object]:
            raise error_type

    with pytest.raises(error_type):
        asyncio.run(_generate_one(FailedLLM()))
