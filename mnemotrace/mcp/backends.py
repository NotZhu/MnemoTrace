"""忆痕 MCP server 的后端。

同一套工具有两种后端实现，启动时选择：

  * LocalBackend  — 在进程内嵌入 `MemoryService`。每个服务器一个固定命名空间（agent 自己的
    记忆）。零外部服务：`python -m mnemotrace.mcp` 直接可用（持久化到 ~/.mnemotrace/data）。
  * RemoteBackend — 代理一个运行中的 忆痕 HTTP 服务器（MNEMOTRACE_API_URL + MNEMOTRACE_API_KEY），
    用同一套 MCP 工具驱动你的托管/多租户部署；api-key 在服务端选择命名空间。

两者返回完全相同的 dict 结构（service / HTTP 契约），因此工具层无论哪种模式都只格式化一种 schema。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlencode

from ..store.contracts import FactState
from ..categories import Category, ObjectKind
from ..types import Cardinality, Retention


@runtime_checkable
class Backend(Protocol):
    mode: str
    async def remember_messages(self, messages: list[dict], session_id: str, mode: str = 'live',
                                processing: str = 'buffered', extract: bool = True, *, agent_id: str | None = None) -> dict: ...

    async def remember(self, content: str, session_id: str = "default", extract: bool = True,
                       processing: str = "immediate", *, agent_id: str | None = None) -> dict: ...
    async def operation(self, operation_id: str, *, retry: bool = False) -> dict | None: ...
    async def processing_status(self, session_id: str | None = None) -> dict: ...
    async def flush_processing(self, session_id: str | None = None, timeout: float = 30,
                               retry_failed: bool = False, include_digests: bool = False) -> dict: ...
    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict | None: ...
    async def aggregate_facts(self, predicate: str, **options) -> dict: ...
    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict: ...

    async def recall(
        self,
        query: str,
        n_chunks: int = 6,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        known_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict: ...
    async def session_report(
        self,
        session_id: str,
        include_sensitive: bool = False,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict: ...
    async def sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        q: str = "",
    ) -> dict: ...
    async def memories(self, *, categories: list[Category] | None, limit: int, offset: int, include_superseded: bool) -> dict: ...
    async def stats(self) -> dict: ...

    async def audit(self, limit: int = 40) -> dict: ...
    async def agent_status(self, session_id: str | None = None) -> dict: ...
    async def profile(self) -> dict: ...
    async def refresh_profile(self) -> dict: ...
    async def get_focus(self) -> dict: ...
    async def set_focus(
        self,
        track: list[str] | None = None,
        mute: list[str] | None = None,
    ) -> dict: ...
    async def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        text: str,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind = "unknown",
        cardinality: Cardinality = "unknown",
        retention: Retention = "unknown",
    ) -> dict: ...
    async def update_fact(
        self,
        fact_id: str,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        text: str | None = None,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind | None = None,
        cardinality: Cardinality | None = None,
        retention: Retention | None = None,
    ) -> dict | None: ...
    async def delete_fact(self, fact_id: str) -> dict: ...
    async def import_(self, data: Any, format: str = "auto") -> dict: ...
    async def export(self, include_sensitive: bool = False) -> dict: ...
    async def forget(self) -> dict: ...
    def describe(self) -> str: ...


class LocalBackend:
    """使用可信 user_id 连接一个授权的 Postgres 空间。同步的 service 调用放入
    worker 线程执行，避免 embedder/LLM 阻塞 MCP 事件循环。"""

    mode = "local"

    def __init__(self, user_id: str, space_id: str, service: Any = None) -> None:
        from ..service import MemoryService

        self._owns_service = service is None
        self.svc = service if service is not None else MemoryService.from_env()
        try:
            self.context = self.svc.spaces.authorize(user_id, space_id)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._owns_service:
            self.svc.close()

    async def remember_messages(self, messages: list[dict], session_id: str, mode: str = 'live',
                                processing: str = 'buffered', extract: bool = True, *, agent_id: str | None = None) -> dict:
        return await asyncio.to_thread(self.svc.remember_messages, self.context, messages, session_id, mode, processing, extract, agent_id=agent_id)

    async def remember(self, content: str, session_id: str = "default", extract: bool = True,
                       processing: str = "immediate", *, agent_id: str | None = None) -> dict:
        return await asyncio.to_thread(self.svc.remember, self.context, content, session_id, extract, processing, agent_id=agent_id)

    async def operation(self, operation_id: str, *, retry: bool = False) -> dict | None:
        return await asyncio.to_thread(self.svc.operation, self.context, operation_id, retry=retry)

    async def processing_status(self, session_id: str | None = None) -> dict:
        return await asyncio.to_thread(self.svc.processing_status, self.context, session_id)

    async def flush_processing(self, session_id: str | None = None, timeout: float = 30,
                               retry_failed: bool = False, include_digests: bool = False) -> dict:
        return await asyncio.to_thread(self.svc.flush, self.context, session_id, timeout, retry_failed, include_digests)

    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict | None:
        context = self.context if space_id is None else await asyncio.to_thread(
            self.svc.spaces.authorize, self.context.user_id, space_id)
        return await asyncio.to_thread(self.svc.get_episode, context, episode_id)

    async def aggregate_facts(self, predicate: str, **options) -> dict:
        return await asyncio.to_thread(self.svc.aggregate_facts, self.context, predicate, **options)

    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict:
        return await asyncio.to_thread(self.svc.follow_path, self.context, anchor, predicates, **options)

    async def recall(
        self,
        query: str,
        n_chunks: int = 6,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        known_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict:
        return await asyncio.to_thread(
            self.svc.recall,
            self.context,
            query,
            lean=lean,
            n_chunks=n_chunks,
            as_of=as_of,
            redact_sensitive=redact_sensitive, state=state, valid_at=valid_at, known_at=known_at,
            subject=subject, predicate=predicate, answer=answer,
        )

    async def session_report(
        self,
        session_id: str,
        include_sensitive: bool = False,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        return await asyncio.to_thread(
            self.svc.session_report,
            self.context,
            session_id,
            include_sensitive,
            limit=limit,
            offset=offset,
        )

    async def sessions(self, limit: int = 20, offset: int = 0, q: str = "") -> dict:
        return await asyncio.to_thread(self.svc.sessions, self.context, limit, offset, q)

    async def memories(self, *, categories: list[Category] | None, limit: int, offset: int, include_superseded: bool) -> dict:
        return await asyncio.to_thread(self.svc.memories, self.context, categories=categories, facts_limit=limit,
                                       facts_offset=offset, episodes_limit=0,
                                       status=None if include_superseded else 'live')

    async def stats(self) -> dict:
        return await asyncio.to_thread(self.svc.stats, self.context)

    async def audit(self, limit: int = 40) -> dict:
        return await asyncio.to_thread(self.svc.audit, self.context, limit)

    async def agent_status(self, session_id: str | None = None) -> dict:
        return await asyncio.to_thread(self.svc.agent_status, self.context, session_id)

    async def profile(self) -> dict:
        return await asyncio.to_thread(self.svc.profile, self.context)

    async def refresh_profile(self) -> dict:
        return await asyncio.to_thread(self.svc.refresh_profile, self.context)

    async def get_focus(self) -> dict:
        return await asyncio.to_thread(self.svc.get_focus, self.context)

    async def set_focus(
        self,
        track: list[str] | None = None,
        mute: list[str] | None = None,
    ) -> dict:
        return await asyncio.to_thread(self.svc.set_focus, self.context, track=track, mute=mute)

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        text: str,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind = "unknown",
        cardinality: Cardinality = "unknown",
        retention: Retention = "unknown",
    ) -> dict:
        return await asyncio.to_thread(
            self.svc.add_fact,
            self.context,
            subject,
            predicate,
            object,
            text=text,
            sensitive=sensitive,
            categories=categories,
            object_kind=object_kind,
            cardinality=cardinality,
            retention=retention,
        )

    async def update_fact(
        self,
        fact_id: str,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        text: str | None = None,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind | None = None,
        cardinality: Cardinality | None = None,
        retention: Retention | None = None,
    ) -> dict | None:
        return await asyncio.to_thread(
            self.svc.update_fact,
            self.context,
            fact_id,
            subject=subject,
            predicate=predicate,
            object=object,
            text=text,
            sensitive=sensitive,
            categories=categories,
            object_kind=object_kind,
            cardinality=cardinality,
            retention=retention,
        )

    async def delete_fact(self, fact_id: str) -> dict:
        return await asyncio.to_thread(self.svc.delete_fact, self.context, fact_id)

    async def import_(self, data: Any, format: str = "auto") -> dict:
        return await asyncio.to_thread(self.svc.import_, self.context, format=format, data=data)

    async def export(self, include_sensitive: bool = False) -> dict:
        return await asyncio.to_thread(self.svc.export, self.context, include_sensitive)

    async def forget(self) -> dict:
        return await asyncio.to_thread(self.svc.forget, self.context)

    def describe(self) -> str:
        return f"Postgres space {self.context.space_id}"


class RemoteBackend:
    """通过 Bearer key 访问产品 HTTP 服务，并传递所选空间。"""

    mode = "remote"

    def __init__(self, api_url: str, key: str, space_id: str, client: Any = None) -> None:
        import httpx  # 惰性导入：仅在真正远程运行时需要

        self.api_url = api_url.rstrip("/")
        self.key = key
        self.space_id = space_id
        self._client = client or httpx.AsyncClient(timeout=120.0)

    @property
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    async def _post(self, path: str, body: dict, *, timeout: float | None = None) -> dict:
        r = await self._client.post(self.api_url + path, json={"space_id": self.space_id, **body}, headers=self._headers,
                                    **({'timeout': timeout} if timeout is not None else {}))
        r.raise_for_status()
        return r.json()

    async def _patch(self, path: str, body: dict) -> dict:
        r = await self._client.patch(self.api_url + path, json={"space_id": self.space_id, **body}, headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def _put(self, path: str, body: dict) -> dict:
        r = await self._client.put(self.api_url + path, json={"space_id": self.space_id, **body}, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def _scoped_path(self, path):
        from urllib.parse import parse_qsl

        route, _, query = path.partition('?')
        params = parse_qsl(query, keep_blank_values=True)
        if not any(key == 'space_id' for key, _ in params):
            params.append(('space_id', self.space_id))
        return route + '?' + urlencode(params)

    async def _delete(self, path: str) -> dict:
        r = await self._client.delete(self.api_url + self._scoped_path(path), headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def _get(self, path: str) -> dict:
        r = await self._client.get(self.api_url + self._scoped_path(path), headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def remember_messages(self, messages: list[dict], session_id: str, mode: str = 'live',
                                processing: str = 'buffered', extract: bool = True, *, agent_id: str | None = None) -> dict:
        return await self._post('/v1/messages', dict(messages=messages, session_id=session_id,
            mode=mode, processing=processing, extract=extract, agent_id=agent_id))

    async def remember(self, content: str, session_id: str = "default", extract: bool = True,
                       processing: str = "immediate", *, agent_id: str | None = None) -> dict:
        return await self._post("/v1/remember", {
            "content": content,
            "session_id": session_id,
            "extract": extract,
            "processing": processing,
            "agent_id": agent_id,
        })

    async def operation(self, operation_id: str, *, retry: bool = False) -> dict:
        path = '/v1/operations/' + quote(operation_id, safe='')
        return await self._post(path + '/retry', {}) if retry else await self._get(path)

    async def processing_status(self, session_id: str | None = None) -> dict:
        query = '?' + urlencode({'session_id': session_id}) if session_id is not None else ''
        return await self._get('/v1/processing' + query)

    async def flush_processing(self, session_id: str | None = None, timeout: float = 30,
                               retry_failed: bool = False, include_digests: bool = False) -> dict:
        return await self._post('/v1/processing/flush', {
            'session_id': session_id, 'timeout': timeout, 'retry_failed': retry_failed,
            'include_digests': include_digests}, timeout=timeout + 10)

    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict:
        query = '?' + urlencode({'space_id': space_id}) if space_id is not None else ''
        return await self._get('/v1/episodes/' + quote(episode_id, safe='') + query)

    async def aggregate_facts(self, predicate: str, **options) -> dict:
        return await self._post('/v1/aggregate', {'predicate': predicate, **options})

    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict:
        return await self._post('/v1/follow-path', {'anchor': anchor, 'predicates': predicates, **options})

    async def recall(
        self,
        query: str,
        n_chunks: int = 6,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        known_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict:
        body = {"query": query, "lean": lean, "n_chunks": n_chunks, "state": state,
                "valid_at": valid_at, "known_at": known_at}
        if answer:
            body["answer"] = True
        if subject is not None:
            body["subject"] = subject
        if predicate is not None:
            body["predicate"] = predicate
        if as_of is not None:
            body["as_of"] = as_of
        if redact_sensitive:
            body["redact_sensitive"] = True
        return await self._post("/v1/recall", body)

    async def session_report(
        self,
        session_id: str,
        include_sensitive: bool = False,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        params = {
            "session_id": session_id,
            "include_sensitive": "true" if include_sensitive else "false",
        }
        if limit != 100:
            params["limit"] = str(limit)
        if offset:
            params["offset"] = str(offset)
        return await self._get("/v1/sessions/report?" + urlencode(params))

    async def sessions(self, limit: int = 20, offset: int = 0, q: str = "") -> dict:
        qs = urlencode({"limit": limit, "offset": offset, "q": q})
        return await self._get("/v1/sessions?" + qs)

    async def memories(self, *, categories: list[Category] | None, limit: int, offset: int, include_superseded: bool) -> dict:
        params = dict(facts_limit=limit, facts_offset=offset, episodes_limit=0)
        if categories:
            params['categories'] = categories
        if not include_superseded:
            params['status'] = 'live'
        return await self._get("/v1/memories?" + urlencode(params, doseq=True))

    async def stats(self) -> dict:
        return await self._get("/v1/stats")

    async def audit(self, limit: int = 40) -> dict:
        return await self._get(f"/v1/audit?limit={int(limit)}")

    async def agent_status(self, session_id: str | None = None) -> dict:
        query = f"?{urlencode({'session_id': session_id})}" if session_id is not None else ""
        return await self._get("/v1/agent/status" + query)

    async def profile(self) -> dict:
        return await self._get("/v1/profile")

    async def refresh_profile(self) -> dict:
        return await self._post("/v1/profile/refresh", {})

    async def get_focus(self) -> dict:
        return await self._get("/v1/focus")

    async def set_focus(
        self,
        track: list[str] | None = None,
        mute: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if track is not None:
            body["track"] = track
        if mute is not None:
            body["mute"] = mute
        return await self._put("/v1/focus", body)

    async def add_fact(
        self,
        subject: str,
        predicate: str,
        object: str,
        text: str,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind = "unknown",
        cardinality: Cardinality = "unknown",
        retention: Retention = "unknown",
    ) -> dict:
        body = {"subject": subject, "predicate": predicate, "object": object, "text": text,
                "cardinality": cardinality, "retention": retention}
        if sensitive is not None:
            body["sensitive"] = sensitive
        if categories is not None:
            body["categories"] = categories
        if object_kind is not None:
            body["object_kind"] = object_kind
        return await self._post("/v1/facts", body)

    async def update_fact(
        self,
        fact_id: str,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
        text: str | None = None,
        sensitive: bool | None = None,
        categories: list[Category] | None = None,
        object_kind: ObjectKind | None = None,
        cardinality: Cardinality | None = None,
        retention: Retention | None = None,
    ) -> dict | None:
        body: dict[str, Any] = {}
        if subject is not None:
            body["subject"] = subject
        if predicate is not None:
            body["predicate"] = predicate
        if object is not None:
            body["object"] = object
        if text is not None:
            body["text"] = text
        if sensitive is not None:
            body["sensitive"] = sensitive
        if categories is not None:
            body["categories"] = categories
        if object_kind is not None:
            body["object_kind"] = object_kind
        if cardinality is not None:
            body["cardinality"] = cardinality
        if retention is not None:
            body["retention"] = retention
        return await self._patch(f"/v1/facts/{quote(fact_id, safe='')}", body)

    async def delete_fact(self, fact_id: str) -> dict:
        return await self._delete(f"/v1/facts/{quote(fact_id, safe='')}")

    async def import_(self, data: Any, format: str = "auto") -> dict:
        return await self._post("/v1/import", {"data": data, "format": format})

    async def export(self, include_sensitive: bool = False) -> dict:
        qs = urlencode({"include_sensitive": "true" if include_sensitive else "false"})
        return await self._get("/v1/export?" + qs)

    async def forget(self) -> dict:
        return await self._post("/v1/forget", {"confirm": True})

    def describe(self) -> str:
        return f"remote MnemoTrace server at {self.api_url}"


def make_backend() -> Backend:
    from ..credentials import resolve_key
    from ..store.postgres.database import identifier

    space_id = os.environ.get('MNEMOTRACE_SPACE_ID')
    identifier(space_id)
    api_url = os.environ.get('MNEMOTRACE_API_URL')
    if api_url:
        return RemoteBackend(api_url, resolve_key(required=True), space_id)
    user_id = os.environ.get('MNEMOTRACE_USER_ID')
    identifier(user_id)
    return LocalBackend(user_id, space_id)
