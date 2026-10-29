"""Qdrant configuration and the client shared by a Memory's four indexes."""
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
    if config.qdrant_url is not None:
        url = urlsplit(config.qdrant_url)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("qdrant_url must be an HTTP(S) endpoint without credentials or query")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote Qdrant endpoints require HTTPS")
        if config.data_path is not None:
            raise ValueError("qdrant_url and data_path cannot be used together")
    else:
        if snapshot_path is not None and config.data_path is None:
            config = replace(config, data_path=os.path.join(snapshot_path, "qdrant"))
        if not config.data_path:
            raise ValueError("storage='qdrant' requires a non-empty data_path in local mode")
    if (not isinstance(config.qdrant_collection_prefix, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config.qdrant_collection_prefix)):
        raise ValueError("qdrant_collection_prefix must contain 1-64 ASCII letters, digits, _ or -")
    if (isinstance(config.qdrant_timeout, bool)
            or not isinstance(config.qdrant_timeout, (int, float))
            or not math.isfinite(config.qdrant_timeout) or config.qdrant_timeout <= 0):
        raise ValueError("qdrant_timeout must be a positive finite number")
    namespace = config.qdrant_namespace
    if namespace is None:
        source = snapshot_path or config.data_path
        if source is None:
            raise ValueError("remote Qdrant requires qdrant_namespace or Memory.open(path)")
        namespace = path_namespace(source)
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("qdrant_namespace must be a non-empty string")
    return replace(config, qdrant_namespace=namespace)


class QdrantBackend:
    def __init__(self, config: Config, dimension: int) -> None:
        config = resolve_config(config)  # validate before import, I/O or connection
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:
            raise ImportError("Qdrant requires pip install 'mnemotrace-memory[qdrant]'") from exc
        if config.qdrant_url:
            self.client = QdrantClient(url=config.qdrant_url, api_key=config.qdrant_api_key,
                                       timeout=config.qdrant_timeout)
        else:
            # SQLite access is serialized by MemoryService's namespace lock. Direct
            # Memory callers must likewise serialize their own concurrent operations.
            self.client = QdrantClient(path=os.path.expanduser(config.data_path),
                                       force_disable_check_same_thread=True)
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
