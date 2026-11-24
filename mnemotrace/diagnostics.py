"""在单次请求或任务内关联诊断事件。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


@contextmanager
def correlate(identifier: str) -> Iterator[None]:
    """绑定本地标识并在退出时恢复调用方的上下文。"""
    token = correlation_id.set(identifier)
    try:
        yield
    finally:
        correlation_id.reset(token)
