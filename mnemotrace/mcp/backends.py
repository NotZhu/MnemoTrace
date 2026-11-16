"""忆痕 MCP server 的后端。

同一套工具有两种后端实现，启动时选择：

  * LocalBackend  — 在进程内嵌入 `MemoryService`。使用运行角色连接 PostgreSQL，并绑定一个已授权的账号与空间。
  * RemoteBackend — 代理一个运行中的 忆痕 HTTP 服务器（MNEMOTRACE_API_URL + MNEMOTRACE_API_KEY），
    用同一套 MCP 工具驱动你的托管/多租户部署；api-key 在服务端选择命名空间。

两者返回完全相同的 dict 结构（service / HTTP 契约），因此工具层无论哪种模式都只格式化一种 schema。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urlencode

from ..store.contracts import message_inputs, FactState
from ..categories import Category


@runtime_checkable
class Backend(Protocol):
    mode: str
    async def remember(self, value: str | list[dict], session_id: str = "default", *,
                       wait: bool = False, timeout: float = 30, message_id: str | None = None,
                       agent_id: str | None = None, event_time: float | None = None) -> dict:
        """保存消息并按需等待事实抽取。

        Args:
            value: 单条消息文本或一批消息记录。
            session_id: 会话 ID。
            wait: 是否等待任务结束。
            timeout: 最长等待时间，单位为秒。
            message_id: 单条文本的消息 ID。
            agent_id: 来源智能体 ID。
            event_time: 单条文本的事件时间戳。

        Returns:
            包含任务状态和原文记录 ID 的结果字典。
        """
        ...
    async def operation(self, operation_id: str, *, retry: bool = False) -> dict | None:
        """查询任务状态，或按需请求重试。

        Args:
            operation_id: 任务操作 ID。
            retry: 是否重新排队可重试的任务。

        Returns:
            任务状态。无结果时可能返回 None。
        """
        ...
    async def processing_status(self, session_id: str | None = None) -> dict:
        """读取当前空间的任务状态。

        Args:
            session_id: 可选的会话 ID。

        Returns:
            任务列表和可用的下一页游标。
        """
        ...
    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict | None:
        """按来源记录 ID 读取原文。

        Args:
            episode_id: 原文记录 ID。
            space_id: 可选的来源空间 ID。

        Returns:
            原文及元数据。无结果时可能返回 None。
        """
        ...
    async def aggregate_facts(self, predicate: str, **options) -> dict:
        """按显式谓词聚合事实。

        Args:
            predicate: 要聚合的事实谓词。
            **options: 时间范围、计数方式与事实筛选选项。

        Returns:
            计数、事实证据和可选的摘要候选。
        """
        ...
    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict:
        """沿显式谓词序列查找关系路径。

        Args:
            anchor: 起始实体名称。
            predicates: 按顺序遍历的谓词。
            **options: 可见性、有效时间和数量限制。

        Returns:
            匹配路径及其证据。
        """
        ...

    async def recall(
        self,
        query: str,
        n_digests: int = 20,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict:
        """检索当前空间的记忆。

        Args:
            query: 查询文本。
            n_digests: 来源摘要数量上限。
            lean: 是否返回预算内的上下文。
            as_of: 查询事实有效性的时间戳。
            redact_sensitive: 是否排除敏感事实。
            state: 要查询的事实状态。
            valid_at: 事实有效时间，与 as_of 互斥。
            subject: 可选的事实主体。
            predicate: 可选的事实谓词。
            answer: 是否从上下文生成模型回答。

        Returns:
            上下文或候选证据，以及可选的回答。
        """
        ...
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
    async def set_fact_sensitive(self, fact_id: str, sensitive: bool) -> dict | None: ...
    async def delete_fact(self, fact_id: str) -> dict: ...
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

    async def remember(self, value: str | list[dict], session_id: str = "default", *,
                       wait: bool = False, timeout: float = 30, message_id: str | None = None,
                       agent_id: str | None = None, event_time: float | None = None) -> dict:
        """通过本地服务保存消息并提交抽取任务。

        Args:
            value: 单条消息文本或一批消息记录。
            session_id: 会话 ID。
            wait: 是否等待任务结束。
            timeout: 最长等待时间，单位为秒。
            message_id: 单条文本的消息 ID。
            agent_id: 来源智能体 ID。
            event_time: 单条文本的事件时间戳。

        Returns:
            本地服务返回的写入结果。

        Raises:
            ProcessingFailure: 请求等待且任务失败或被取消。
        """
        return await asyncio.to_thread(self.svc.remember, self.context, value, session_id,
                                       wait=wait, timeout=timeout, message_id=message_id,
                                       agent_id=agent_id, event_time=event_time)

    async def operation(self, operation_id: str, *, retry: bool = False) -> dict | None:
        """通过本地服务查询或重试任务。

        Args:
            operation_id: 任务操作 ID。
            retry: 是否重新排队可重试的任务。

        Returns:
            任务状态。任务不存在时返回 None。
        """
        return await asyncio.to_thread(self.svc.operation, self.context, operation_id, retry=retry)

    async def processing_status(self, session_id: str | None = None) -> dict:
        """通过本地服务读取任务状态。

        Args:
            session_id: 可选的会话 ID。

        Returns:
            本地服务返回的任务列表。
        """
        return await asyncio.to_thread(self.svc.processing_status, self.context, session_id)

    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict | None:
        """通过本地服务读取已授权空间中的原文。

        Args:
            episode_id: 原文记录 ID。
            space_id: 可选的来源空间 ID。

        Returns:
            原文及元数据。记录不存在时返回 None。
        """
        context = self.context if space_id is None else await asyncio.to_thread(
            self.svc.spaces.authorize, self.context.user_id, space_id)
        return await asyncio.to_thread(self.svc.get_episode, context, episode_id)

    async def aggregate_facts(self, predicate: str, **options) -> dict:
        """调用本地服务聚合事实。

        Args:
            predicate: 要聚合的事实谓词。
            **options: 时间范围、计数方式与事实筛选选项。

        Returns:
            计数、事实证据和可选的摘要候选。
        """
        return await asyncio.to_thread(self.svc.aggregate_facts, self.context, predicate, **options)

    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict:
        """调用本地服务遍历事实关系。

        Args:
            anchor: 起始实体名称。
            predicates: 按顺序遍历的谓词。
            **options: 可见性、有效时间和数量限制。

        Returns:
            匹配路径及其证据。
        """
        return await asyncio.to_thread(self.svc.follow_path, self.context, anchor, predicates, **options)

    async def recall(
        self,
        query: str,
        n_digests: int = 20,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict:
        """通过本地服务检索当前空间的记忆。

        Args:
            query: 查询文本。
            n_digests: 来源摘要数量上限。
            lean: 是否返回预算内的上下文。
            as_of: 查询事实有效性的时间戳。
            redact_sensitive: 是否排除敏感事实。
            state: 要查询的事实状态。
            valid_at: 事实有效时间，与 as_of 互斥。
            subject: 可选的事实主体。
            predicate: 可选的事实谓词。
            answer: 是否从上下文生成模型回答。

        Returns:
            本地服务返回的检索结果。
        """
        return await asyncio.to_thread(
            self.svc.recall,
            self.context,
            query,
            lean=lean,
            n_digests=n_digests,
            as_of=as_of,
            redact_sensitive=redact_sensitive, state=state, valid_at=valid_at,
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

    async def set_fact_sensitive(self, fact_id: str, sensitive: bool) -> dict | None:
        return await asyncio.to_thread(self.svc.set_fact_sensitive, self.context, fact_id, sensitive)

    async def delete_fact(self, fact_id: str) -> dict:
        return await asyncio.to_thread(self.svc.delete_fact, self.context, fact_id)


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
        """向当前空间提交 JSON 请求。

        Args:
            path: 相对于 API 地址的路径。
            body: 请求体字段。
            timeout: 可选的传输超时秒数。

        Returns:
            解码后的响应对象。

        Raises:
            httpx.HTTPStatusError: 远程服务返回非成功状态。
        """
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
        """向选定空间发送 GET 请求。

        Args:
            path: 相对于 API 地址的路径，可带查询参数。

        Returns:
            解码后的响应对象。

        Raises:
            httpx.HTTPStatusError: 远程服务返回非成功状态。
        """
        r = await self._client.get(self.api_url + self._scoped_path(path), headers=self._headers)
        r.raise_for_status()
        return r.json()

    async def remember(self, value: str | list[dict], session_id: str = "default", *,
                       wait: bool = False, timeout: float = 30, message_id: str | None = None,
                       agent_id: str | None = None, event_time: float | None = None) -> dict:
        """经由 HTTP 保存消息并提交抽取任务。

        Args:
            value: 单条消息文本或一批消息记录。
            session_id: 会话 ID。
            wait: 是否等待任务结束。
            timeout: 最长等待时间，单位为秒。
            message_id: 单条文本的消息 ID。
            agent_id: 来源智能体 ID。
            event_time: 单条文本的事件时间戳。

        Returns:
            远程服务返回的写入结果。

        Raises:
            ValueError: 消息输入不符合约定。
            httpx.HTTPStatusError: 远程服务返回非成功状态。
        """
        items = message_inputs(value, message_id=message_id, event_time=event_time, agent_id=agent_id)
        return await self._post('/v1/remember', dict(session_id=session_id, wait=wait,
            timeout=timeout, agent_id=agent_id, messages=[
                dict(message_id=item.message_id, role=item.role, content=item.content, event_time=item.event_time,
                     **({'agent_id': item.agent_id} if item.agent_id is not None and agent_id is None else {}))
                for item in items]), timeout=timeout + 10 if wait else None)

    async def operation(self, operation_id: str, *, retry: bool = False) -> dict:
        """经由 HTTP 查询或重试任务。

        Args:
            operation_id: 任务操作 ID。
            retry: 是否重新排队可重试的任务。

        Returns:
            远程服务返回的任务状态。
        """
        path = '/v1/operations/' + quote(operation_id, safe='')
        return await self._post(path + '/retry', {}) if retry else await self._get(path)

    async def processing_status(self, session_id: str | None = None) -> dict:
        """经由 HTTP 读取任务状态。

        Args:
            session_id: 可选的会话 ID。

        Returns:
            远程服务返回的任务列表。
        """
        query = '?' + urlencode({'session_id': session_id}) if session_id is not None else ''
        return await self._get('/v1/processing' + query)

    async def get_episode(self, episode_id: str, space_id: str | None = None) -> dict:
        """经由 HTTP 读取指定空间的原文。

        Args:
            episode_id: 原文记录 ID。
            space_id: 可选的来源空间 ID。

        Returns:
            原文及元数据。

        Raises:
            httpx.HTTPStatusError: 远程服务返回非成功状态。
        """
        query = '?' + urlencode({'space_id': space_id}) if space_id is not None else ''
        return await self._get('/v1/episodes/' + quote(episode_id, safe='') + query)

    async def aggregate_facts(self, predicate: str, **options) -> dict:
        """经由 HTTP 聚合事实。

        Args:
            predicate: 要聚合的事实谓词。
            **options: 时间范围、计数方式与事实筛选选项。

        Returns:
            计数、事实证据和可选的摘要候选。
        """
        return await self._post('/v1/aggregate', {'predicate': predicate, **options})

    async def follow_path(self, anchor: str, predicates: list[str], **options) -> dict:
        """经由 HTTP 遍历事实关系。

        Args:
            anchor: 起始实体名称。
            predicates: 按顺序遍历的谓词。
            **options: 可见性、有效时间和数量限制。

        Returns:
            匹配路径及其证据。
        """
        return await self._post('/v1/follow-path', {'anchor': anchor, 'predicates': predicates, **options})

    async def recall(
        self,
        query: str,
        n_digests: int = 20,
        lean: bool = True,
        as_of: float | None = None,
        redact_sensitive: bool = False,
        state: FactState = FactState.CURRENT,
        valid_at: float | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        answer: bool = False,
    ) -> dict:
        """经由 HTTP 检索当前空间的记忆。

        Args:
            query: 查询文本。
            n_digests: 来源摘要数量上限。
            lean: 是否返回预算内的上下文。
            as_of: 查询事实有效性的时间戳。
            redact_sensitive: 是否排除敏感事实。
            state: 要查询的事实状态。
            valid_at: 事实有效时间，与 as_of 互斥。
            subject: 可选的事实主体。
            predicate: 可选的事实谓词。
            answer: 是否从上下文生成模型回答。

        Returns:
            远程服务返回的检索结果。

        Raises:
            httpx.HTTPStatusError: 远程服务返回非成功状态。
        """
        body = {"query": query, "lean": lean, "n_digests": n_digests, "state": state,
                "valid_at": valid_at}
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

    async def set_fact_sensitive(self, fact_id: str, sensitive: bool) -> dict | None:
        return await self._patch(f"/v1/facts/{quote(fact_id, safe='')}/sensitivity", {"sensitive": sensitive})

    async def delete_fact(self, fact_id: str) -> dict:
        return await self._delete(f"/v1/facts/{quote(fact_id, safe='')}")


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
