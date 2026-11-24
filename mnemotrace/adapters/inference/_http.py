"""共享推理客户端的并发、请求预算与错误处理。"""

import asyncio
import json
import logging
import socket
import ssl
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform
from typing import Literal, cast
from uuid import uuid4

import httpx

from ...diagnostics import correlation_id
from ...ports.inference import InferenceError

type _FailureKind = Literal[
    "auth",
    "quota",
    "rate_limit",
    "input_limit",
    "request",
    "upstream",
    "connection",
    "protocol",
    "response_limit",
]

_LOG = logging.getLogger(__name__)
_GZIP_WINDOW_BITS = 16 + zlib.MAX_WBITS
_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_QUOTA_CODES = {
    "insufficient_quota",
    "billing_hard_limit_reached",
    "insufficient_balance",
    "Arrearage",
    "AllocationQuota.FreeTierOnly",
}
_INPUT_LIMIT_CODES = {"context_length_exceeded", "input_length_exceeded"}


class InferenceRequestError(InferenceError):
    """报告可安全输出的请求或上游响应故障。

    Args:
        kind: 诊断用的错误类别。
        status_code: 有 HTTP 响应时的状态码。
        attempt_id: 本地生成的传输尝试标识。

    Attributes:
        kind: 诊断用的错误类别。
        status_code: 有 HTTP 响应时的状态码。
        attempt_id: 本地生成的传输尝试标识。
    """

    def __init__(
        self,
        kind: _FailureKind,
        *,
        status_code: int | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.attempt_id = attempt_id
        super().__init__(
            f"推理请求失败 ({kind}, status={status_code}, attempt_id={attempt_id})"
        )


class _RetryableFailure(Exception):
    def __init__(
        self,
        error: InferenceRequestError | TimeoutError,
        retry_after: float | None = None,
    ) -> None:
        self.error = error
        self.retry_after = retry_after


@dataclass
class RequestBudget:
    """在一次补全或单个嵌入子批内计数，共用操作的绝对时限。"""

    deadline: float
    attempts: int = 0

    def allows_retry(self, delay: float = 0) -> bool:
        """判断是否还能在剩余时限内再次请求。

        Args:
            delay: 再次请求前的等待秒数。

        Returns:
            仍有请求次数和时间时为 True。
        """
        return (
            self.attempts < 2
            and asyncio.get_running_loop().time() + delay < self.deadline
        )


class InferenceHTTP:
    """为一个端点和凭据组合共享异步 HTTP 请求。

    Args:
        client: 装配层管理的客户端。
        connect_timeout: 连接、写入及连接池等待的超时秒数。
        max_in_flight: 同时发出的请求上限。
        max_response_bytes: 单响应的传输和解压正文上限，单位为字节。
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        connect_timeout: float,
        max_in_flight: int,
        max_response_bytes: int,
    ) -> None:
        self._client = client
        self._connect_timeout = connect_timeout
        self._slots = asyncio.Semaphore(max_in_flight)
        self._max_response_bytes = max_response_bytes
        self._send_attempt_id = (
            client.base_url.scheme == "https"
            and client.base_url.host == "api.openai.com"
            and client.base_url.port in (None, 443)
        )

    async def post(
        self, path: str, payload: dict[str, object], *, budget: RequestBudget, mode: str
    ) -> dict[str, object]:
        """在给定预算内发送推理请求。

        Args:
            path: 相对于已配置 API 前缀的路由。
            payload: 已装配的请求对象。
            budget: 当前补全或子批的尝试次数和时限。
            mode: 用于诊断的调用模式。

        Returns:
            上游返回的 JSON 对象。

        Raises:
            InferenceRequestError: 请求被拒绝、上游故障或响应不是合法对象。
            TimeoutError: 请求已超时。
        """
        while True:
            try:
                return await self._post_once(path, payload, budget, mode)
            except _RetryableFailure as failure:
                delay = (
                    failure.retry_after
                    if failure.retry_after is not None
                    else uniform(0, 0.5)
                )
                if not budget.allows_retry(delay):
                    raise failure.error from None
            await asyncio.sleep(delay)

    async def _post_once(
        self, path: str, payload: dict[str, object], budget: RequestBudget, mode: str
    ) -> dict[str, object]:
        loop = asyncio.get_running_loop()
        async with self._slots:
            started = loop.time()
            remaining = budget.deadline - started
            if remaining <= 0:
                raise TimeoutError("推理总时限已用完")
            budget.attempts += 1
            attempt_id = uuid4().hex
            headers = {"Accept-Encoding": "gzip, identity"}
            if self._send_attempt_id:
                headers["X-Client-Request-Id"] = attempt_id
            phase_timeout = min(self._connect_timeout, remaining)
            status: int | None = None
            body: dict[str, object] | None = None
            outcome = "ok"
            try:
                async with self._client.stream(
                    "POST",
                    path,
                    json=payload,
                    headers=headers,
                    timeout=httpx.Timeout(
                        remaining,
                        connect=phase_timeout,
                        write=phase_timeout,
                        pool=phase_timeout,
                    ),
                ) as response:
                    status = response.status_code
                    raw_body = await _read_response_body(
                        response, self._max_response_bytes
                    )
                body = _response_object(raw_body)
                if response.is_success and body is not None:
                    return body

                http_kind = _failure_kind(status, body)
                outcome = http_kind
                error = InferenceRequestError(
                    http_kind, status_code=status, attempt_id=attempt_id
                )
                if (
                    http_kind in ("rate_limit", "upstream")
                    and status in _RETRY_STATUSES
                ):
                    raise _RetryableFailure(
                        error, _retry_after(response.headers.get("retry-after"))
                    )
                raise error
            except InferenceRequestError as exc:
                outcome = exc.kind
                if exc.attempt_id is not None:
                    raise
                raise InferenceRequestError(
                    exc.kind, status_code=exc.status_code, attempt_id=attempt_id
                ) from None
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except httpx.RequestError as exc:
                if isinstance(exc, httpx.TimeoutException):
                    outcome = "timeout"
                    timeout = TimeoutError(f"推理请求超时 attempt_id={attempt_id}")
                    if isinstance(exc, httpx.ConnectTimeout):
                        raise _RetryableFailure(timeout) from None
                    raise timeout from None
                kind: _FailureKind = (
                    "protocol"
                    if isinstance(exc, (httpx.ProtocolError, httpx.DecodingError))
                    else "connection"
                )
                outcome = kind
                failure = InferenceRequestError(
                    kind, status_code=status, attempt_id=attempt_id
                )
                if isinstance(exc, httpx.ConnectError) and _temporary_connection(exc):
                    raise _RetryableFailure(failure) from None
                raise failure from None

            finally:
                usage = body.get("usage") if body is not None else None
                _LOG.info(
                    "推理请求 model=%s mode=%s attempt=%d elapsed_ms=%.0f status=%s "
                    "outcome=%s correlation_id=%s attempt_id=%s "
                    "prompt_tokens=%s completion_tokens=%s",
                    payload["model"],
                    mode,
                    budget.attempts,
                    (loop.time() - started) * 1000,
                    status,
                    outcome,
                    correlation_id.get(),
                    attempt_id,
                    _usage_count(usage, "prompt_tokens"),
                    _usage_count(usage, "completion_tokens"),
                )


def require_response_object(value: object) -> dict[str, object]:
    """读取上游响应中必须为 JSON 对象的节点。

    Args:
        value: JSON 解码所得的节点。

    Returns:
        以字符串为键的对象。

    Raises:
        InferenceRequestError: 上游在该节点返回了其他类型。
    """
    if not isinstance(value, dict):
        raise InferenceRequestError("protocol")
    return cast(dict[str, object], value)


async def _read_response_body(
    response: httpx.Response, limit: int
) -> bytes | bytearray:
    if response.is_stream_consumed:
        # 自定义传输可返回已缓冲内容，此时 HTTPX 已对正文解码。
        if len(response.content) > limit:
            raise _response_error("response_limit", response)
        return response.content

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity", "gzip"):
        raise _response_error("protocol", response)
    decoder = zlib.decompressobj(_GZIP_WINDOW_BITS) if encoding == "gzip" else None
    body = bytearray()
    received = 0
    try:
        async for chunk in response.aiter_raw():
            received += len(chunk)
            if received > limit:
                raise _response_error("response_limit", response)
            if decoder is None:
                body.extend(chunk)
                continue
            while chunk:
                if decoder.eof:
                    decoder = zlib.decompressobj(_GZIP_WINDOW_BITS)
                # 限制单次解压分配，多读一字节区分恰好达到上限和超限。
                body.extend(decoder.decompress(chunk, limit - len(body) + 1))
                if len(body) > limit:
                    raise _response_error("response_limit", response)
                chunk = decoder.unused_data
    except zlib.error:
        raise _response_error("protocol", response) from None
    if decoder is not None and not decoder.eof:
        raise _response_error("protocol", response)
    return body


def _response_error(
    kind: _FailureKind, response: httpx.Response
) -> InferenceRequestError:
    return InferenceRequestError(kind, status_code=response.status_code)


def _response_object(body: bytes | bytearray) -> dict[str, object] | None:
    try:
        value: object = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _usage_count(usage: object, key: str) -> int | None:
    count: object = usage.get(key) if isinstance(usage, dict) else None
    return count if type(count) is int and count >= 0 else None


def _failure_kind(status_code: int, body: dict[str, object] | None) -> _FailureKind:
    detail: object = (body or {}).get("error", body)
    codes = (
        {value for key in ("code", "type") if isinstance(value := detail.get(key), str)}
        if isinstance(detail, dict)
        else set()
    )
    if status_code == 402 or codes & _QUOTA_CODES:
        return "quota"
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    if status_code == 413 or codes & _INPUT_LIMIT_CODES:
        return "input_limit"
    if status_code == 408 or status_code >= 500:
        return "upstream"
    if status_code >= 400:
        return "request"
    return "protocol"


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        return None
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _temporary_connection(error: httpx.ConnectError) -> bool:
    cause: BaseException | None = error.__cause__
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return False
        if isinstance(cause, socket.gaierror) and cause.errno != socket.EAI_AGAIN:
            return False
        cause = cause.__cause__
    return True
