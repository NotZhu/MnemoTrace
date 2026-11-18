"""通过兼容端点的固定 HTTP 响应验证补全端口。"""

import asyncio
import json

import httpx
import pytest

from mnemotrace_next.adapters.inference import InferenceRequestError
from mnemotrace_next.ports.inference import (
    Completion,
    CompletionRefused,
    CompletionTruncated,
    InvalidStructuredOutput,
)

from .sample import CHAT_ENV, chat_response, configured_chat

SCHEMA = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["note", "task"]},
                    "due": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
                "required": ["kind", "due"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["records"],
    "additionalProperties": False,
}


def test_strict_json_uses_prefix_and_returns_validated_nested_result() -> None:
    """严格模式携带 schema，把解析后的嵌套记录交给调用方。"""
    expected = {"records": [{"kind": "note", "due": None}]}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert (
            request.url == "https://models.example/compatible-mode/v1/chat/completions"
        )
        assert request.headers["Authorization"] == "Bearer fixture-chat-key"
        assert json.loads(request.content) == {
            "model": "synthetic-chat",
            "messages": [
                {"role": "system", "content": "提取记录"},
                {"role": "user", "content": "备忘，没有日期"},
            ],
            "stream": False,
            "max_completion_tokens": 2048,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "mnemotrace_output",
                    "schema": SCHEMA,
                    "strict": True,
                },
            },
        }
        return chat_response(json.dumps(expected))

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            result = await llm.complete_json(
                system="提取记录", prompt="备忘，没有日期", schema=SCHEMA
            )
            assert result == expected

    asyncio.run(exercise())


@pytest.mark.parametrize("temperature", [None, "0"])
def test_plain_completion_preserves_text_and_absent_reason(
    temperature: str | None,
) -> None:
    """普通补全保留正文和结束原因，并仅发送显式配置的温度。

    Args:
        temperature: 可省略的采样温度配置。
    """

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "response_format" not in payload
        if temperature is None:
            assert "temperature" not in payload
        else:
            assert payload["temperature"] == 0.0
        return chat_response("  合成文本😀\n", finish_reason=None)

    async def exercise() -> None:
        env = dict(CHAT_ENV)
        if temperature is not None:
            env["MNEMOTRACE_NEXT_LLM_TEMPERATURE"] = temperature
        async with configured_chat(respond, env) as llm:
            assert await llm.complete(system="回复", prompt="合成输入") == Completion(
                "  合成文本😀\n"
            )

    asyncio.run(exercise())


def test_plain_completion_rejects_unpaired_surrogate() -> None:
    """将无法编码为 UTF-8 的补全正文报告为上游协议错误。"""
    body = rb'{"choices":[{"finish_reason":"stop","message":{"content":"\ud800"}}]}'

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(InferenceRequestError) as captured:
                await llm.complete(system="回复", prompt="合成输入")
            assert captured.value.kind == "protocol"

    asyncio.run(exercise())


def test_prompted_mode_reasks_invalid_unicode_and_accepts_a_surrogate_pair() -> None:
    """孤立代理字符触发重问，合法的代理对解码为字符。"""
    schema = {
        "type": "object",
        "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
        "required": ["titles"],
        "additionalProperties": False,
    }
    answers = iter((r'{"titles":["\ud800"]}', r'{"titles":["\ud83d\ude00"]}'))

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_object"}
        async with configured_chat(lambda _: chat_response(next(answers)), env) as llm:
            result = await llm.complete_json(
                system="提取标题", prompt="合成输入", schema=schema
            )
        assert result == {"titles": ["😀"]}

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("mode", "thinking", "expected_flag"),
    [
        ("json_object", "deepseek", {"thinking": {"type": "disabled"}}),
        ("json_object", "dashscope", {"enable_thinking": False}),
        ("prompt", None, {}),
    ],
)
def test_prompted_mode_reasks_with_schema_path_only(
    mode: str, thinking: str | None, expected_flag: dict[str, object]
) -> None:
    """提示约束留在系统消息中，重问只回传校验路径和规则。"""
    requests: list[dict[str, object]] = []
    private_output = "fixture-output-do-not-replay"
    expected = {"records": [{"kind": "task", "due": 12}]}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_tokens"] == 2048
        assert "max_completion_tokens" not in payload
        assert {
            key: payload[key]
            for key in ("thinking", "enable_thinking")
            if key in payload
        } == expected_flag
        if mode == "json_object":
            assert payload["response_format"] == {"type": "json_object"}
        else:
            assert "response_format" not in payload
        system, user = payload["messages"]
        assert user == {"role": "user", "content": "合成任务"}
        assert system["content"].startswith("提取任务\n")
        assert "JSON Schema" in system["content"]
        assert private_output not in system["content"]
        if len(requests) == 1:
            return chat_response(
                json.dumps({"records": [{"kind": "task", "due": private_output}]})
            )
        assert '["records", 0, "due"]' in system["content"]
        assert "anyOf" in system["content"].split("校验反馈：")[1]
        return chat_response(json.dumps(expected))

    async def exercise() -> None:
        env = {
            **CHAT_ENV,
            "MNEMOTRACE_NEXT_LLM_JSON_MODE": mode,
            "MNEMOTRACE_NEXT_LLM_TOKEN_LIMIT_FIELD": "max_tokens",
        }
        if thinking:
            env["MNEMOTRACE_NEXT_LLM_DISABLE_THINKING"] = thinking
        async with configured_chat(respond, env) as llm:
            assert (
                await llm.complete_json(
                    system="提取任务", prompt="合成任务", schema=SCHEMA
                )
                == expected
            )
        assert len(requests) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize(
    ("response_reason", "refusal", "error"),
    [
        ("length", None, CompletionTruncated),
        ("content_filter", None, CompletionRefused),
        ("stop", "fixture-refusal-do-not-print", CompletionRefused),
    ],
)
def test_terminal_states_are_raised_without_reask(
    structured: bool,
    response_reason: str,
    refusal: str | None,
    error: type[RuntimeError],
) -> None:
    """拒答和截断优先于正文校验，并且不触发重问。"""
    attempts = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if response_reason == "content_filter":
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "content_filter"}]}
            )
        return chat_response(
            '{"records":[]}', finish_reason=response_reason, refusal=refusal
        )

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_object"}
        async with configured_chat(respond, env) as llm:
            with pytest.raises(error):
                if structured:
                    await llm.complete_json(system="提取", prompt="输入", schema=SCHEMA)
                else:
                    await llm.complete(system="回复", prompt="输入")
        assert attempts == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "invalid_text",
    [
        "   ",
        '```json\n{"records":[]}\n```',
        "[]",
        "{}",
        '{"records": [], "records": []}',
        '{"records": [{"kind":"note", "due":NaN}]}',
        '{"records": [{"kind":"note", "due":1e400}]}',
        '{"records": [{"kind":"note", "due":true}]}',
        '{"records": [{"kind":"unlisted", "due":null}]}',
        '{"records": [{"kind":"note", "due":null, "extra":0}]}',
    ],
)
def test_invalid_strict_output_is_rejected_without_reask(invalid_text: str) -> None:
    """严格模式发现非法内容或 schema 错误时直接失败。"""
    attempts = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return chat_response(invalid_text)

    async def exercise() -> None:
        async with configured_chat(respond) as llm:
            with pytest.raises(InvalidStructuredOutput):
                await llm.complete_json(system="提取", prompt="输入", schema=SCHEMA)
        assert attempts == 1

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "member",
    [
        {"type": "string", "pattern": "a"},
        {"anyOf": [{"type": "integer"}, {"type": "string"}]},
    ],
)
def test_schema_outside_port_subset_never_reaches_provider(
    member: dict[str, object],
) -> None:
    """本地发现不支持的 schema 时不消耗模型请求。"""

    def respond(_: httpx.Request) -> httpx.Response:
        raise AssertionError("非法 schema 不应发请求")

    async def exercise() -> None:
        schema = {
            "type": "object",
            "properties": {"value": member},
            "required": ["value"],
            "additionalProperties": False,
        }
        async with configured_chat(respond) as llm:
            with pytest.raises(ValueError):
                await llm.complete_json(system="提取", prompt="输入", schema=schema)

    asyncio.run(exercise())


def test_missing_choices_is_a_protocol_error() -> None:
    """损坏的外层响应与有效 JSON 正文校验错误使用不同异常。"""

    async def exercise() -> None:
        async with configured_chat(
            lambda _: httpx.Response(200, json={"choices": []})
        ) as llm:
            with pytest.raises(InferenceRequestError) as captured:
                await llm.complete_json(system="提取", prompt="输入", schema=SCHEMA)
            assert captured.value.kind == "protocol"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("statuses", "error"),
    [
        ((200, 200), InvalidStructuredOutput),
        ((503, 200), InvalidStructuredOutput),
        ((200, 503), InferenceRequestError),
    ],
)
def test_network_retry_and_reask_share_one_extra_attempt(
    statuses: tuple[int, int], error: type[RuntimeError]
) -> None:
    """第二次请求失败时按该次故障结束，不能再消耗一次请求。"""
    remaining = iter(statuses)

    def respond(_: httpx.Request) -> httpx.Response:
        status = next(remaining, None)
        assert status is not None, "一次补全至多发送两次"
        if status == 503:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return chat_response(" ")

    async def exercise() -> None:
        env = {**CHAT_ENV, "MNEMOTRACE_NEXT_LLM_JSON_MODE": "json_object"}
        async with configured_chat(respond, env) as llm:
            with pytest.raises(error):
                await llm.complete_json(system="提取", prompt="输入", schema=SCHEMA)
        assert next(remaining, None) is None

    asyncio.run(exercise())
