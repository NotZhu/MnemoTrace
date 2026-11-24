"""将文本和结构化补全映射到兼容的 Chat Completions 路由。"""

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast, overload

from ...ports.inference import (
    Completion,
    CompletionRefused,
    CompletionTruncated,
    InferenceInputTooLarge,
    InvalidStructuredOutput,
)
from ._http import (
    InferenceHTTP,
    InferenceRequestError,
    RequestBudget,
    require_response_object,
)
from ._structured import decode_output, prepare_validator

type _JSONMode = Literal["json_schema", "json_object", "prompt"]
type _TokenLimitField = Literal["max_completion_tokens", "max_tokens"]
type _ThinkingStyle = Literal["deepseek", "dashscope"]
CONTEXT_OVERHEAD_TOKENS = 256
CORRECTION_TOKENS = 512


@dataclass(frozen=True)
class _ChatOptions:
    model: str
    json_mode: _JSONMode
    temperature: float | None
    max_output_tokens: int
    context_window_tokens: int
    token_limit_field: _TokenLimitField
    disable_thinking: _ThinkingStyle | None
    timeout: float


class OpenAIChatLLM:
    """对兼容端点执行带时限的文本和结构化补全。

    Args:
        http: 由装配层共享的 HTTP 请求模块。
        options: 启动时校验的模型及请求选项。
    """

    def __init__(self, http: InferenceHTTP, options: _ChatOptions) -> None:
        self._http = http
        self._options = options

    def structured_prompt_fits(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> bool:
        """按真实结构化请求格式估算输入，并预留一次校验反馈。"""
        normalized = cast(Mapping[str, object], prepare_validator(schema).schema)
        payload = self._structured_payload(system, prompt, normalized)
        return self._fits(payload, reserve=CORRECTION_TOKENS)

    async def complete(self, *, system: str, prompt: str) -> Completion:
        """请求一次文本补全。

        Args:
            system: 系统指令。
            prompt: 待补全文本。

        Returns:
            可编码为 UTF-8 的正文和上游结束原因。

        Raises:
            CompletionRefused: 上游拒答或过滤输出。
            CompletionTruncated: 上游截断输出。
            InferenceRequestError: 请求故障，或正文为空或无法编码为 UTF-8。
            TimeoutError: 请求已超时。
        """
        budget = self._budget()
        async with asyncio.timeout_at(budget.deadline):
            completion = await self._send(self._payload(system, prompt), budget, "text")
            if not completion.text.strip():
                raise InferenceRequestError("protocol")
            try:
                completion.text.encode("utf-8")
            except UnicodeEncodeError:
                raise InferenceRequestError("protocol") from None
            return completion

    @overload
    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object]: ...

    @overload
    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T],
        enforce_context_budget: bool = False,
    ) -> T: ...

    async def complete_json[T](
        self,
        *,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        parse: Callable[[Mapping[str, object]], T] | None = None,
        enforce_context_budget: bool = False,
    ) -> Mapping[str, object] | T:
        """请求并校验一个 JSON 对象。

        Args:
            system: 系统指令。
            prompt: 待补全文本。
            schema: 端口支持的输出 schema。
            parse: 可选的应用解析器，与 JSON 和 schema 校验共享纠错预算。
            enforce_context_budget: 为真时检查模型上下文预算并分类输入超限。

        Returns:
            已通过 schema 校验的对象，或 parse 转换后的结果。

        Raises:
            ValueError: schema 不符合端口约定。
            CompletionRefused: 上游拒答或过滤输出。
            CompletionTruncated: 上游截断输出。
            InferenceInputTooLarge: 启用预算校验时，原始输入超出上下文。
            InvalidStructuredOutput: 正文违反 JSON、schema 或应用契约。
            InferenceRequestError: 请求或上游响应故障。
            TimeoutError: 请求已超时。
        """
        validator = prepare_validator(schema)
        normalized = cast(Mapping[str, object], validator.schema)
        mode = self._options.json_mode
        correct_invalid_output = parse is not None or mode != "json_schema"
        payload = self._structured_payload(system, prompt, normalized)
        if enforce_context_budget and not self._fits(payload):
            raise InferenceInputTooLarge("结构化补全输入超出模型上下文预算")
        budget = self._budget()
        correction_error: InvalidStructuredOutput | None = None
        async with asyncio.timeout_at(budget.deadline):
            while True:
                try:
                    completion = await self._send(payload, budget, mode)
                except InferenceRequestError as exc:
                    if not enforce_context_budget or exc.kind != "input_limit":
                        raise
                    if correction_error is not None:
                        raise correction_error from None
                    raise InferenceInputTooLarge("上游拒绝超出输入预算的请求") from exc
                try:
                    decoded = decode_output(completion.text, validator)
                    return decoded if parse is None else parse(decoded)
                except InvalidStructuredOutput as exc:
                    if not correct_invalid_output or not budget.allows_retry():
                        raise
                    feedback = f"\n重新生成 JSON。校验反馈：{exc}"
                    payload = self._structured_payload(
                        system, prompt, normalized, feedback=feedback
                    )
                    if enforce_context_budget and not self._fits(payload):
                        raise
                    correction_error = exc

    def _structured_payload(
        self,
        system: str,
        prompt: str,
        schema: Mapping[str, object],
        *,
        feedback: str = "",
    ) -> dict[str, object]:
        mode = self._options.json_mode
        if mode != "json_schema":
            system += "\n\n请返回符合以下 JSON Schema 的单个 JSON 对象。\n"
            system += json.dumps(schema, ensure_ascii=False)
        payload = self._payload(system + feedback, prompt)
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mnemotrace_output",
                    "strict": True,
                    "schema": schema,
                },
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _fits(self, payload: dict[str, object], *, reserve: int = 0) -> bool:
        size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        return (
            size + CONTEXT_OVERHEAD_TOKENS + reserve + self._options.max_output_tokens
            <= self._options.context_window_tokens
        )

    def _budget(self) -> RequestBudget:
        return RequestBudget(asyncio.get_running_loop().time() + self._options.timeout)

    def _payload(self, system: str, prompt: str) -> dict[str, object]:
        options = self._options
        payload: dict[str, object] = {
            "model": options.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            options.token_limit_field: options.max_output_tokens,
        }
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.disable_thinking == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        elif options.disable_thinking == "dashscope":
            payload["enable_thinking"] = False
        return payload

    async def _send(
        self, payload: dict[str, object], budget: RequestBudget, mode: str
    ) -> Completion:
        body = await self._http.post(
            "chat/completions", payload, budget=budget, mode=mode
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise InferenceRequestError("protocol")
        choice = require_response_object(choices[0])
        reason = choice.get("finish_reason")
        if reason == "content_filter":
            raise CompletionRefused("上游拒绝补全")
        if reason == "length":
            raise CompletionTruncated("上游补全被截断")
        message = require_response_object(choice.get("message"))
        refusal = message.get("refusal")
        if isinstance(refusal, str) and refusal:
            raise CompletionRefused("上游拒绝补全")
        if reason not in (None, "stop") or "content" not in message:
            raise InferenceRequestError("protocol")
        content = message["content"]
        if content is not None and not isinstance(content, str):
            raise InferenceRequestError("protocol")
        return Completion(content or "", reason)
