"""管理 HTTP 请求标识、正文大小上限和意外错误响应。"""

from fastapi import HTTPException, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..diagnostics import correlate
from .errors import log_unexpected_error, problem
from .response_headers import apply_response_headers, request_id


class RequestBoundary:
    """限制声明和实际读取的正文字节数，统一处理意外 HTTP 错误。"""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        identifier = request_id(scope)
        response_started = False

        async def send_response(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                apply_response_headers(scope, MutableHeaders(scope=message))
                response_started = True
            await send(message)

        length = Headers(scope=scope).get("content-length")
        if length is not None:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                await problem(Request(scope), 400, "invalid_content_length")(
                    scope, receive, send_response
                )
                return
            if declared > self._max_body_bytes:
                await problem(Request(scope), 413, "request_too_large")(
                    scope, receive, send_response
                )
                return

        received = 0

        async def receive_limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_body_bytes:
                    raise HTTPException(413, detail="request_too_large")
            return message

        with correlate(identifier):
            try:
                await self._app(scope, receive_limited, send_response)
            except Exception as exc:  # noqa: BLE001 - HTTP 边界统一脱敏意外异常。
                log_unexpected_error(identifier, exc)
            else:
                return

            if response_started:
                # 退出原异常处理后再通知 ASGI 服务器中断传输。
                raise RuntimeError("HTTP response interrupted")
            await problem(Request(scope), 500, "internal_error")(
                scope, receive, send_response
            )
