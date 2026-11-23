"""将文本和结构化补全映射到兼容的 Chat Completions 路由。"""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from ...ports.inference import (
    Completion,
    CompletionRefused,
    CompletionTruncated,
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


@dataclass(frozen=True)
class _ChatOptions:
    model: str
    json_mode: _JSONMode
    temperature: float | None
    max_output_tokens: int
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

    async def complete_json(
        self, *, system: str, prompt: str, schema: Mapping[str, object]
    ) -> Mapping[str, object]:
        """请求并校验一个 JSON 对象。

        Args:
            system: 系统指令。
            prompt: 待补全文本。
            schema: 端口支持的输出 schema。

        Returns:
            已通过 schema 校验的对象。

        Raises:
            ValueError: schema 不符合端口约定。
            CompletionRefused: 上游拒答或过滤输出。
            CompletionTruncated: 上游截断输出。
            InvalidStructuredOutput: 正文不是符合 schema 的 JSON 对象。
            InferenceRequestError: 请求或上游响应故障。
            TimeoutError: 请求已超时。
        """
        validator = prepare_validator(schema)
        mode = self._options.json_mode
        response_format: dict[str, object] | None = None
        if mode == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mnemotrace_output",
                    "strict": True,
                    "schema": validator.schema,
                },
            }
        else:
            system += "\n\n请返回符合以下 JSON Schema 的单个 JSON 对象。\n"
            system += json.dumps(validator.schema, ensure_ascii=False)
            if mode == "json_object":
                response_format = {"type": "json_object"}

        budget = self._budget()
        async with asyncio.timeout_at(budget.deadline):
            while True:
                payload = self._payload(system, prompt)
                if response_format is not None:
                    payload["response_format"] = response_format
                completion = await self._send(payload, budget, mode)
                try:
                    return decode_output(completion.text, validator)
                except InvalidStructuredOutput as exc:
                    if mode == "json_schema" or not budget.allows_retry():
                        raise
                    system += f"\n重新生成 JSON。校验反馈：{exc}"

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
