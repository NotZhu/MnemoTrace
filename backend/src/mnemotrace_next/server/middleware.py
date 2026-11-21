"""限制 HTTP 请求大小并为响应添加服务端生成的请求标识。"""

from uuid import uuid4

from fastapi import HTTPException, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import problem


class RequestBoundary:
    """对声明长度和 ASGI 实际读取的正文应用同一字节上限。"""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                if scope["path"].startswith("/v1/"):
                    headers["Cache-Control"] = "no-store"
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

        await self._app(scope, receive_limited, send_response)
