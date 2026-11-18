"""共享推理客户端的并发、请求预算与错误处理。"""

import asyncio
import logging
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import uniform
from typing import Literal, cast

import httpx

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
]

_LOG = logging.getLogger(__name__)
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
        request_id: 上游提供的请求标识。

    Attributes:
        kind: 诊断用的错误类别。
        status_code: 有 HTTP 响应时的状态码。
        request_id: 上游提供的请求标识。
    """

    def __init__(
        self,
        kind: _FailureKind,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(
            f"推理请求失败 ({kind}, status={status_code}, request_id={request_id})"
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
    """

    def __init__(
        self, client: httpx.AsyncClient, *, connect_timeout: float, max_in_flight: int
    ) -> None:
        self._client = client
        self._connect_timeout = connect_timeout
        self._slots = asyncio.Semaphore(max_in_flight)

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
            phase_timeout = min(self._connect_timeout, remaining)
            try:
                response = await self._client.post(
                    path,
                    json=payload,
                    timeout=httpx.Timeout(
                        remaining,
                        connect=phase_timeout,
                        write=phase_timeout,
                        pool=phase_timeout,
                    ),
                )
            except httpx.RequestError as exc:
                _LOG.info(
                    "推理请求 model=%s mode=%s attempt=%d elapsed_ms=%.0f transport=%s",
                    payload["model"],
                    mode,
                    budget.attempts,
                    (loop.time() - started) * 1000,
                    type(exc).__name__,
                )
                if isinstance(exc, httpx.TimeoutException):
                    timeout = TimeoutError("推理请求超时")
                    if isinstance(exc, httpx.ConnectTimeout):
                        raise _RetryableFailure(timeout) from None
                    raise timeout from None
                kind: _FailureKind = (
                    "protocol"
                    if isinstance(exc, (httpx.ProtocolError, httpx.DecodingError))
                    else "connection"
                )
                failure = InferenceRequestError(kind)
                if isinstance(exc, httpx.ConnectError) and _temporary_connection(exc):
                    raise _RetryableFailure(failure) from None
                raise failure from None

        body = _response_object(response)
        request_id = _request_id(response.headers, body)
        usage = body.get("usage") if body is not None else None
        _LOG.info(
            "推理请求 model=%s mode=%s attempt=%d elapsed_ms=%.0f status=%d "
            "request_id=%s prompt_tokens=%s completion_tokens=%s",
            payload["model"],
            mode,
            budget.attempts,
            (loop.time() - started) * 1000,
            response.status_code,
            request_id,
            _usage_count(usage, "prompt_tokens"),
            _usage_count(usage, "completion_tokens"),
        )
        if response.is_success and body is not None:
            return body

        http_kind = _failure_kind(response.status_code, body)
        error = InferenceRequestError(
            http_kind, status_code=response.status_code, request_id=request_id
        )
        if (
            http_kind in ("rate_limit", "upstream")
            and response.status_code in _RETRY_STATUSES
        ):
            raise _RetryableFailure(
                error, _retry_after(response.headers.get("retry-after"))
            )
        raise error


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


def _response_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        value: object = response.json()
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _request_id(headers: httpx.Headers, body: dict[str, object] | None) -> str | None:
    request_id: object = (
        headers.get("x-request-id")
        or headers.get("x-dashscope-request-id")
        or headers.get("x-siliconcloud-trace-id")
        or (body or {}).get("request_id")
    )
    if isinstance(request_id, str) and re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", request_id
    ):
        return request_id
    return None


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
