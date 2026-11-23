"""在评测边缘限制 HTTP 请求并保留可审计的模型响应。"""

from collections.abc import Callable
from typing import Literal

import httpx
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from .config import Endpoint, Model
from .contracts import Completion, ModelCall

_JSON = TypeAdapter(JsonValue)


def _redact(value: JsonValue, key: str) -> JsonValue:
    if isinstance(value, str):
        return value.replace(key, "[redacted]")
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    if isinstance(value, dict):
        return {
            name.replace(key, "[redacted]"): _redact(item, key)
            for name, item in value.items()
        }
    return value


class EvaluationError(RuntimeError):
    """表示可记录、可恢复的评测执行错误。"""

    def __init__(self, message: str, *, raw: JsonValue = None) -> None:
        super().__init__(message)
        self.raw = raw


class RequestFailed(EvaluationError):
    """保存不含 URL、响应正文或凭据的请求失败。"""

    def __init__(self, endpoint: str, status: int | None = None) -> None:
        super().__init__(
            f"{endpoint} 请求失败 ({status if status is not None else 'transport'})"
        )
        self.status = status

    @property
    def retryable(self) -> bool:
        """报告可重新提交的传输或过载情况。"""
        return self.status is None or self.status in {408, 429, 500, 502, 503, 504}


class Http:
    """仅向显式配置的端点发送请求。"""

    def __init__(self, client: httpx.Client, name: str, endpoint: Endpoint) -> None:
        self._client = client
        self.name = name
        self._base_url = endpoint.base_url
        key = endpoint.key()
        self._credential = key
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}

    def request(
        self,
        method: Literal["GET", "POST", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        params: dict[str, str] | None = None,
        key: str | None = None,
        expected: tuple[int, ...] = (200,),
        observe: Callable[[int | None, JsonValue], None] | None = None,
    ) -> JsonValue:
        """验证状态和 JSON 响应，记录无敏感内容的错误。"""
        headers = self._headers.copy()
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            response = self._client.request(
                method,
                self._base_url + path,
                json=body,
                params=params,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.RequestError:
            if observe is not None:
                observe(None, None)
            raise RequestFailed(self.name) from None
        status = response.status_code
        if status not in expected and observe is None:
            raise RequestFailed(self.name, response.status_code)
        raw: JsonValue = None
        invalid_json = False
        try:
            if response.content and status != 204:
                raw = _JSON.validate_json(response.content)
        except ValidationError:
            invalid_json = True
            if observe is not None:
                raw = response.text
        if observe is not None:
            if self._credential:
                raw = _redact(raw, self._credential)
            observe(status, raw)
        if status not in expected:
            raise RequestFailed(self.name, status)
        if invalid_json:
            raise EvaluationError(f"{self.name} 返回无效 JSON") from None
        return raw


class _Message(BaseModel):
    content: str | None = None


class _Choice(BaseModel):
    message: _Message
    finish_reason: str | None = None


def _tokens(usage: JsonValue, field: str) -> int | None:
    value = usage.get(field) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else None


class _ChatResponse(BaseModel):
    model: str
    choices: list[_Choice] = Field(min_length=1)


class Chat:
    """按固定参数调用一个 OpenAI 兼容的回答或裁判模型。"""

    def __init__(self, http: Http, config: Model) -> None:
        self._http = http
        self._config = config

    def complete(
        self, prompt: str, *, on_call: Callable[[ModelCall], None]
    ) -> Completion:
        """返回完整响应及实际模型和 token 计数。"""

        def record(status: int | None, raw: JsonValue) -> None:
            usage = raw.get("usage") if isinstance(raw, dict) else None
            on_call(
                ModelCall(
                    status=status,
                    raw=raw,
                    prompt_tokens=_tokens(usage, "prompt_tokens"),
                    completion_tokens=_tokens(usage, "completion_tokens"),
                )
            )

        body: dict[str, JsonValue] = {
            "model": self._config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            self._config.token_limit_field: self._config.max_output_tokens,
        }
        if self._config.disable_thinking == "deepseek":
            body["thinking"] = {"type": "disabled"}
        raw = self._http.request(
            "POST",
            "/chat/completions",
            body=body,
            observe=record,
        )
        try:
            result = _ChatResponse.model_validate(raw)
        except ValidationError:
            raise EvaluationError(f"{self._http.name} 模型响应缺少有效字段") from None
        choice = result.choices[0]
        if (
            choice.finish_reason not in {None, "stop"}
            or not (choice.message.content or "").strip()
        ):
            raise EvaluationError(f"{self._http.name} 模型未完成文本回答", raw=raw)
        usage = raw.get("usage") if isinstance(raw, dict) else None
        return Completion(
            text=choice.message.content or "",
            model=result.model,
            prompt_tokens=_tokens(usage, "prompt_tokens"),
            completion_tokens=_tokens(usage, "completion_tokens"),
            raw=raw,
        )
