"""Qdrant 配置和客户端共享。"""
from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import replace
from urllib.parse import urlsplit

from ..config import Config
from .qdrant_store import QdrantVectorStore

INDEX_NAMES = ("episodes_vec", "fact_store", "cold_store", "summary_vec")


def path_namespace(path: str) -> str:
    canonical = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_config(config: Config, snapshot_path: str | None = None) -> Config:
    """验证并解析 Qdrant 配置。

    Args:
        config: 配置对象。
        snapshot_path: 可选的快照路径，用于推导命名空间。
    """
    # 如果指定了 qdrant_url，验证其格式
    if config.qdrant_url is not None:
        url = urlsplit(config.qdrant_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("qdrant_url 必须是一个 HTTP(S) 端点，不能包含凭据或查询参数")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("远程 Qdrant 端点需要 HTTPS")
        if config.data_path is not None:
            raise ValueError("qdrant_url 和 data_path 不能同时使用")
    # 如果未指定 qdrant_url，确保 data_path 可用，或从快照根目录派生
    else:
        if snapshot_path is not None and config.data_path is None:
            config = replace(config, data_path=os.path.join(snapshot_path, "qdrant"))
        if not config.data_path:
            raise ValueError("storage='qdrant' 需要一个非空的 data_path，用于本地模式下")
    # 验证 qdrant_collection_prefix 和 qdrant_timeout 是否符合要求
    if (not isinstance(config.qdrant_collection_prefix, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config.qdrant_collection_prefix)):
        raise ValueError("qdrant_collection_prefix 必须包含 1 到 64 个 ASCII 字母、数字或下划线和短横线")
    # 验证 qdrant_timeout 是否为正的有限数字
    if (isinstance(config.qdrant_timeout, bool)
            or not isinstance(config.qdrant_timeout, (int, float))
            or not math.isfinite(config.qdrant_timeout) or config.qdrant_timeout <= 0):
        raise ValueError("qdrant_timeout 必须是一个正的有限数字")
    # 验证 qdrant_namespace 是否符合要求
    namespace = config.qdrant_namespace
    if namespace is None:
        source = snapshot_path or config.data_path
        if source is None:
            raise ValueError("远程 Qdrant 需要 qdrant_namespace 或 Memory.open(path)")
        namespace = path_namespace(source)
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("qdrant_namespace 必须是一个非空字符串")
    return replace(config, qdrant_namespace=namespace)


class QdrantBackend:
    """Qdrant 后端共享客户端和集合。"""
    def __init__(self, config: Config, dimension: int) -> None:
        """初始化 Qdrant 后端共享客户端和集合。

        Args:
            config: 配置对象。
            dimension: 向量维度。
        """
        config = resolve_config(config)  # 验证并解析配置
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise ImportError("Qdrant 需要运行 pip install 'mnemotrace-memory[qdrant]'") from exc
        if config.qdrant_url:
            self.client = QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key,
                                       timeout=config.qdrant_timeout)
        else:
            # 本地模式使用 data_path。服务通过命名空间锁串行访问 SQLite；
            # 直接使用 Memory 的调用方同样须自行串行化并发操作。
            self.client = QdrantClient(path=os.path.expanduser(config.data_path),
                                       force_disable_check_same_thread=True)
        # 关闭标记
        self._closed = False
        digest = hashlib.sha256(config.qdrant_namespace.encode("utf-8")).hexdigest()
        try:
            self.stores = {name: QdrantVectorStore(
                self.client, f"{config.qdrant_collection_prefix}_{digest}_{name}", dimension,
            ) for name in INDEX_NAMES}
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self.client.close()
            self._closed = True

    def erase(self) -> None:
        for store in self.stores.values():
            store.drop()

    def prepare_restore(self, dimension: int, allow_mismatch: bool = False):
        for store in self.stores.values():
            store.dimension = dimension
            if allow_mismatch and store._call("collection_exists", store.collection):
                params = store._call("get_collection", store.collection).config.params.vectors
                # Inspect existing rows at their actual dimension. Schema and all
                # typed payload validation must precede any collection recreation.
                from qdrant_client import models

                if isinstance(params, models.VectorParams):
                    store.dimension = params.size
            store._exists()

        def finish():
            for store in self.stores.values():
                if store.dimension != dimension:
                    store.reset_dimension(dimension)
        return finish
