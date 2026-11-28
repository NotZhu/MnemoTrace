"""为普通响应和框架错误响应提供统一的请求标识与响应头。"""

from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import Scope


def request_id(scope: Scope) -> str:
    """取得当前请求的服务端标识，首次读取时分配。"""
    state = scope.setdefault("state", {})
    if "request_id" not in state:
        state["request_id"] = uuid4().hex
    return str(state["request_id"])


def apply_response_headers(scope: Scope, headers: MutableHeaders) -> None:
    """附加请求标识和 HTTP 响应策略。"""
    headers["X-Request-ID"] = request_id(scope)
    headers["X-Content-Type-Options"] = "nosniff"
    if scope["path"].startswith("/v1/"):
        headers["Cache-Control"] = "no-store"
