"""按稳定的服务锁路径共享进程内协调对象，供多个服务实例使用。"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from weakref import WeakValueDictionary

from . import _flock


class NamespaceLock:
    def __init__(self, path: str):
        self.path = path
        self.thread_lock = threading.RLock()
        self._owner = threading.local()

    @property
    def file_held(self) -> bool:
        return getattr(self._owner, "file_held", False)

    @contextmanager
    def file_lock(self):
        # 仅最外层持有者打开文件句柄；单独使用 RLock 不能让 flock/msvcrt 支持重入。
        with self.thread_lock:
            if self.file_held:
                yield
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                _flock.acquire(fh)
                self._owner.file_held = True
                try:
                    yield
                finally:
                    self._owner.file_held = False
                    _flock.release(fh)


_registry: WeakValueDictionary = WeakValueDictionary()
_registry_lock = threading.Lock()


def namespace_lock(path: str) -> NamespaceLock:
    # 路径解析涉及 I/O，必须在获取注册表的元数据锁之前完成。
    key = (os.getpid(), os.path.normcase(os.path.realpath(path)))
    with _registry_lock:
        state = _registry.get(key)
        if state is None:
            state = NamespaceLock(path)
            _registry[key] = state
        return state
