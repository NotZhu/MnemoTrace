"""Offline algorithm engine for ingestion, consolidation, and retrieval."""
from __future__ import annotations

import re
from copy import copy, deepcopy
from typing import Callable, Optional

from .config import Config
from .connectors.documents import document_inputs
from .consolidate import reinforce
from .consolidate.facts import FactWriter
from .consolidate.manual import apply_manual_edit, build_manual_fact, reconcile_manual
from .consolidate.policy import POLICY_DEFAULTS
from .consolidate.summarizer import ProfileBuilder
from .embed import Embedder
from .ingest import Ingestor
from .llm import LLM
from .retrieve import (
    HybridRetriever,
    history,
)
from .retrieve.answers import (
    SearchResult, chain_terms, digest_match, should_abstain,
)
from .retrieve.events import EventEvidence, event_evidence
from .retrieve.lexical import bm25_scores, overlap_terms
from .store import (
    GraphStore,
    InMemoryDocStore,
    InMemoryGraphStore,
    InMemoryVectorStore,
    VectorStore,
)
from .categories import Category, ObjectKind
from .extraction import ExtractionError, require_llm
from .types import Cardinality, Conflict, Episode, Fact, Retention
from .util import fmt_date, now, time_key


class MemoryEngine:
    def __init__(
        self,
        config: Optional[Config] = None,
        embedder: Optional[Embedder] = None,
        llm: Optional[LLM] = None,
        reranker=None,
        vector_store_factory: Callable[[], VectorStore] = InMemoryVectorStore,
        graph_store_factory: Callable[[], GraphStore] = InMemoryGraphStore,
    ) -> None:
        """初始化记忆及其存储、整合和检索组件。

        Args:
            config: 运行配置；默认为 Config()。
            embedder: 用于写入、整合和检索的嵌入器；
            llm: 可选的语言模型，用于事实提取、摘要和查询规划等；
            reranker: 可选的重排序器，用于调整 chunk 和 session 检索结果。
            vector_store_factory: 创建向量存储的无参工厂，默认使用内存存储；
            graph_store_factory: 创建图存储的无参工厂，默认使用内存存储。
        """
        self.config = config or Config()
        self.embedder = embedder
        self.reranker = reranker  # 可选 cross-encoder；提升 chunk/session 检索精度 (AGENTS.md「检索与模块」)
        self.llm = llm  # 启用时供 agentic 检索（查询分解）使用
        # 初始化文档存储
        self.episodes_doc = InMemoryDocStore()
        # 初始化向量存储
        self.episodes_vec = vector_store_factory()  # 原始事件的向量索引，用于检索相关片段
        self.fact_store = vector_store_factory()  # 热层事实的向量存储，用于主要检索
        self.cold_store = vector_store_factory()  # 冷层事实的向量存储，保留从热层换出的事实
        self.digest_vec = vector_store_factory()  # 会话摘要的向量索引，用于检索相关摘要
        # 初始化图存储
        try:
            self.graph = graph_store_factory()
        except BaseException:
            self.close()
            raise
        # 初始化用户画像构建器
        self.profiles = ProfileBuilder()
        # 初始化用户画像缓存
        self._persona_cache: dict[str, str] = {}
        # 查询工作集：记录 lean_context 最近一次执行到事实组装时选出的事实，供检查。
        # 后续 lean_context 执行到此阶段时会覆盖它；该列表不写入快照。
        self.working_set: list[Fact] = []
        # 条目可随快照保存并恢复；短期指其生命周期，不会因此成为长期事实。
        # 关注主题：track 提升相关事实的显著度；mute 在上下文和画像中抑制相关事实。
        self.focus: dict[str, list[str]] = {"track": [], "mute": []}
        # 记忆策略：抽取指令及抽取、摘要、画像提示词；空提示词使用内置默认值。
        self.policy: dict[str, str] = {"extract_instruction": "", "extract_system": "",
                                       "digest_system": "", "persona_system": ""}
        self._import_seen: set[str] = set()  # 已导入事件的指纹，用于重复导入去重
        self.conflicts: dict[str, Conflict] = {}  # 疑似冲突及其处理状态，按冲突 ID 保存
        self.cold_pages_out: dict[str, int] = {}  # 每个用户从热层换出到冷层的事实数
        self.cold_pages_in: dict[str, int] = {}  # 每个用户从冷层调回热层的事实数
        self._pipeline_state = None
        import threading
        self._pipeline_lock = threading.RLock()
        self._pipeline_step_lock = threading.Lock()
        self._pipeline_closed = False
        self._pipeline_broken = False
        from .consolidate.planning import validate_config
        validate_config(self.config)
        try:
            self._rewire()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Stop processing on this in-memory instance."""
        self._pipeline_closed = True

    def __enter__(self) -> "MemoryEngine":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _reinforce_fact(self, fact: Fact) -> None:
        reinforce(fact, self.config.access_boost)
        self._upsert_fact(fact)

    def _encode(self, text, vector=None):
        if self.embedder is None:
            if vector is not None:
                raise ValueError('A supplied vector requires an embedder')
            return None
        from .store._validation import validate_embedding

        vector = self.embedder.embed(text) if vector is None else vector
        validate_embedding(vector, 'memory embedding', self.embedder.dim)
        return vector

    def _encode_batch(self, texts):
        if self.embedder is None:
            return [None] * len(texts)
        if not texts:
            return []
        from .store._validation import validate_embedding

        vectors = self.embedder.embed_batch(texts)
        if len(vectors) != len(texts):
            raise ValueError('Embedding batch length mismatch')
        for vector in vectors:
            validate_embedding(vector, 'memory embedding', self.embedder.dim)
        return vectors

    def _sync_episode(self, ep: Episode) -> None:
        if self.embedder is not None and ep.digest and ep.digest_embedding is None:
            raise ValueError("A searchable digest requires its embedding")
        self.episodes_doc.put(ep.id, ep)
        if self.embedder is None:
            return
        if ep.digest:
            self.digest_vec.upsert(ep.id, ep.digest_embedding, ep)
            self.episodes_vec.delete(ep.id)
        else:
            self.episodes_vec.upsert(ep.id, ep.embedding, ep)
            self.digest_vec.delete(ep.id)

    def _all_facts(self, *, include_unpublished=False) -> list[Fact]:
        facts = self.fact_store.values() + self.cold_store.values()
        return facts if include_unpublished else [fact for fact in facts if fact.is_published()]

    def _upsert_fact(self, fact: Fact, *, tier: str = "existing") -> None:
        """把被修改的 fact 写回所属层，或强制提升到热层。

        内存 store 持有对象引用，但真实的向量后端在 upsert/get 时常会拷贝 payload。因此仅修改
        返回的 Fact 不够；每次语义编辑/失效化都必须显式把 payload 写回唯一一个层。
        """
        in_cold = self.cold_store.get(fact.id) is not None
        in_hot = self.fact_store.get(fact.id) is not None
        if tier == "hot" or (tier == "existing" and not in_cold):
            self.cold_store.delete(fact.id)
            self.fact_store.upsert(fact.id, fact.embedding, fact)
            return
        if in_hot:
            self.fact_store.delete(fact.id)
        self.cold_store.upsert(fact.id, fact.embedding, fact)

    def _rewire(self) -> None:
        """重建处理组件，使其引用当前存储、嵌入器和配置。

        初始化和重嵌入后调用；嵌入器和 LLM 由运行时提供。
        """
        # 初始化事件写入器
        self.ingestor = Ingestor(self.episodes_doc, self.episodes_vec, self.embedder)
        self.writer = FactWriter(self.fact_store, self.graph, self.embedder, self.config)
        self.retriever = HybridRetriever(self.fact_store, self.graph, self.embedder, self.config)
        self.cold_retriever = HybridRetriever(self.cold_store, self.graph, self.embedder, self.config)
    def reembed(self, embedder: Optional[Embedder] = None) -> dict[str, int]:
        """Regenerate the vectors in this in-memory instance."""
        if embedder is not None:
            self.embedder = embedder
        if self.embedder is None:
            raise ValueError('Configure an embedder to build vectors')

        def _clear(store) -> None:
            for payload in list(store.values()):
                key = getattr(payload, "id", None)
                if key is not None:
                    store.delete(key)

        counts: dict[str, int] = {}
        for name, store in (("facts", self.fact_store), ("cold", self.cold_store)):
            facts = store.values()
            _clear(store)
            vecs = self.embedder.embed_batch([f.text for f in facts]) if facts else []
            for f, v in zip(facts, vecs):
                f.embedding = v
                store.upsert(f.id, v, f)
            counts[name] = len(facts)

        episodes = list(self.episodes_doc.values())  # doc 日志是 episode 文本的权威来源
        _clear(self.episodes_vec)
        raw_episodes = [ep for ep in episodes if not ep.digest]
        vecs = self.embedder.embed_batch([ep.content for ep in raw_episodes]) if raw_episodes else []
        for ep in episodes:
            ep.embedding = None
        for ep, v in zip(raw_episodes, vecs):
            ep.embedding = v
        counts["episodes"] = len(episodes)

        digested = [ep for ep in episodes if ep.digest]
        _clear(self.digest_vec)
        for ep in raw_episodes:
            ep.digest_embedding = None
        vecs = (self.embedder.embed_batch([ep.digest for ep in digested])
                if digested else [])
        for ep, v in zip(digested, vecs):
            ep.digest_embedding = v
        # Copying backends must receive the complete episode only after both
        # selected embeddings have entered the new space.
        for ep in episodes:
            self._sync_episode(ep)
        counts["digests"] = len(digested)


        # Saved generation is independent of the embedding space; its checkpoint
        # vectors must be recomputed before a pending commit can resume.
        if self._pipeline_state:
            from .consolidate.pipeline import config_identity, reset_attempts
            for state in self._pipeline_state['namespaces'].values():
                for window in state['windows'].values():
                    if window['phase'] in {'embed', 'commit'} and window['state'] not in {'completed', 'cancelled'}:
                        window.update(phase='embed', state='pending', vectors={},
                                      next_attempt_at=0)
                        # Changing the embedding space cannot authorize a result
                        # generated under an obsolete model or extraction policy.
                        if window['generation_config'] == config_identity(self, include_embedding=False):
                            window['config'] = config_identity(self)
                        reset_attempts(window, 'embed_attempts', 'reembed', now())

        self._persona_cache.clear()
        self._rewire()  # 检索器和事实提交器持有 embedder 引用，需要重新绑定。
        return counts

    # --- 写路径 ---
    def add(
        self,
        content: str,
        user_id: str = "default",
        session_id: str = "default",
        role: str = "user",
        event_time: Optional[float] = None,
        consolidate: bool = False,
        embedding: Optional[list] = None,
    ) -> Episode:
        """写入一条带时间戳的 episode。

        Args:
            content: 事件内容。
            user_id: 用户 ID。
            session_id: 会话 ID。
            role: 说话者。
            event_time: 事件时间戳。
            consolidate: 是否立即执行整合。
            embedding: 事件内容的嵌入向量。
        Returns:
            Episode: 写入的 episode 对象。
        """
        if self._pipeline_broken or self._pipeline_closed:
            raise RuntimeError('Memory checkpoint failed or closed; create a new instance')
        if consolidate:
            require_llm(self.llm)
        vector = self._encode(content, embedding)
        from ._pipeline import memory_adapter
        with memory_adapter(self, user_id).write_lock(user_id):
            ep = self.ingestor.ingest(content, user_id, session_id, role, event_time, embedding=vector)
        if consolidate:
            self.consolidate()
        return ep

    def remember(self, content: str, user_id: str = "default", session_id: str = "default",
                 scope: str = "auto", *, _embedding=None) -> dict:
        """Save raw input and its requested lifecycle policy.

        Auto/long remain pending until consolidate() validates model output.
        Reusable facts retain their source attribution and time qualifiers.
        """
        if self._pipeline_broken or self._pipeline_closed:
            raise RuntimeError('Memory checkpoint failed or closed; create a new instance')
        if scope not in {"auto", "long"}:
            raise ValueError("scope must be auto or long")
        vector = self._encode(content, _embedding)
        from ._pipeline import memory_adapter
        with memory_adapter(self, user_id).write_lock(user_id):
            ep = self.add(content, user_id=user_id, session_id=session_id, embedding=vector)
            ep.scope = scope
            self._sync_episode(ep)
            return {"scope": scope, "episode_id": ep.id}

    def remember_messages(self, messages: list[dict], session_id: str, user_id: str = 'default',
                          mode: str = 'live', scope: str = 'auto') -> dict:
        """Offline-capable dialogue source/task recording; service owns background execution.

        Product callers use Memory for durable processing.
        """
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        from .consolidate.pipeline import PipelineTasks, normalized
        rows = normalized(messages, session_id, mode, scope)
        with self._pipeline_lock:
            if self._pipeline_closed:
                raise RuntimeError('Memory is closed')
            tasks = PipelineTasks(self, user_id)
            rows, unseen = tasks.inspect(rows, session_id, mode, scope)
        batch = self._encode_batch(list(unseen.values()))
        if len(batch) != len(unseen):
            raise ValueError('Embedding batch length mismatch')
        vectors = dict(zip(unseen, batch))
        with self._pipeline_lock:
            if self._pipeline_closed:
                raise RuntimeError('Memory is closed')
            tasks = PipelineTasks(self, user_id)
            tasks.inspect(rows, session_id, mode, scope)
            try:
                ids, fresh = tasks.accept(rows, session_id, mode, scope, now(), vectors)
            except BaseException:
                self._recover_pipeline_checkpoint()
                raise
            return {**tasks.response(ids), 'accepted': len(fresh)}

    def _recover_pipeline_checkpoint(self):
        """Fence an in-memory instance after a partial write."""
        self._pipeline_broken = True
        self.close()

    def consolidate(self, episodes=None):
        """Activate selected source extraction through the shared checkpoint engine."""
        if self._pipeline_broken or self._pipeline_closed:
            raise RuntimeError('Memory checkpoint failed or closed; create a new instance')
        from ._pipeline import execute_memory
        supplied = list(episodes) if episodes is not None else self.ingestor.pending()
        sources = []
        for value in supplied:
            ep = self.episodes_doc.get(value.id)
            if ep is None:
                if not isinstance(value, Episode):
                    raise ValueError('Cannot extract an absent original source')
                ep = value
                if ep.embedding is None and self.embedder is not None:
                    ep.embedding = self._encode(ep.content)
                self._sync_episode(ep)
            if not ep.consolidated and ep.content.strip():
                sources.append(ep)
        if sources:
            require_llm(self.llm)
        totals = self.writer.empty_stats()
        for user in dict.fromkeys(ep.user_id for ep in sources):
            result = execute_memory(self, user, [ep for ep in sources if ep.user_id == user], 'extract')
            for key in totals:
                totals[key] += result.get(key, 0)
        for value in supplied:
            if isinstance(value, Episode) and self.episodes_doc.get(value.id):
                current = self.episodes_doc.get(value.id)
                value.consolidated, value.coverage = current.consolidated, current.coverage
        return totals


    def consolidate_full(
        self,
        fact_episodes: Optional[list[Episode | EventEvidence]] = None,
        digest_episodes: Optional[list[Episode | EventEvidence]] = None,
    ) -> dict[str, int]:
        """一次连贯的 System-2 处理，构建所有读取期层：
          L1 facts     来自 `fact_episodes`     （对最相关 session 做深度抽取），
          L2 digests 来自 `digest_episodes`  （广泛、廉价的覆盖，用于聚合），
          L3 persona   从产出的 facts 刷新（惰性，首次读取时）。
        两个 episode 集合刻意不同：facts 需要在少数 session 上做深，digests 需要广。
        未指定时两者都默认取 pending 队列。"""
        stats = self.consolidate(fact_episodes)
        stats["digests"] = self.generate_digests(
            digest_episodes if digest_episodes is not None else self.ingestor.pending()
        )
        stats["reflected"] = 0
        return stats

    # --- 批量导入（AGENTS.md 「代码导航」 采用层）：批量接入用户自己的历史 ---
    def flush_messages(self, user_id: str = 'default', session_id: Optional[str] = None,
                       timeout: float = 30, retry_failed: bool = False) -> dict:
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        if self._pipeline_closed:
            raise RuntimeError('Memory is closed; create a new instance')
        from ._pipeline import flush_memory
        return flush_memory(self, user_id, session_id, timeout, retry_failed)

    def import_messages(self, sessions, user_id: str = 'default', consolidate: Optional[bool] = None,
                        generate_digests: bool = True, roles: bool = True, batch_size: int = 256,
                        base_time: Optional[float] = None, dedupe: bool = True) -> dict:
        """Adapt history to individual historical messages; documents retain their own input path.

        Undated messages remain unknown. Positional import IDs are stable within an ordered
        source export; changing an existing position requires an explicit source message ID.
        """
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        from .connectors.dialogue import batches
        from .consolidate.pipeline import PipelineTasks
        plans = batches(list(sessions), consolidate)
        tasks = PipelineTasks(self, user_id)
        if any(messages is not None and extract and tasks.inspect(messages, sid, 'historical')[1]
               for sid, messages, _, extract in plans):
            require_llm(self.llm)
        stats = dict(sessions=0, episodes=0, skipped=0, facts_added=0, digests=0,
                     facts_deferred=0, deferred_reason=None)
        before = len(self.fact_store.values())
        prior_digests = {e.id for e in self.episodes_doc.values() if e.digest}
        for sid, messages, record, extract in plans:
            if messages is None:
                result = self._import_documents([record], user_id, consolidate, generate_digests, roles,
                                                batch_size, base_time, dedupe)
                for key in ('sessions', 'episodes', 'skipped', 'facts_added', 'digests'):
                    stats[key] += result.get(key, 0)
                continue
            result = self.remember_messages(messages, sid, user_id, 'historical', 'auto' if extract else 'raw')
            fresh = result['accepted']
            if not extract:
                stats['facts_deferred'] += fresh
                stats['deferred_reason'] = 'not_requested'
            for row in result['messages']:
                ep = self.episodes_doc.get(row['episode_id'])
                for key in ('source',):
                    if key in record.get('metadata', {}):
                        ep.metadata.setdefault(key, record['metadata'][key])
                self._sync_episode(ep)
            stats['sessions'] += bool(fresh)
            stats['episodes'] += fresh
            stats['skipped'] += len(messages) - fresh
            if extract and fresh:
                result = self.flush_messages(user_id, sid)
                if not result['ok']:
                    raise ExtractionError(stored_raw=True, episode_ids=[r['episode_id'] for r in result['jobs']])
        stats['facts_added'] = len(self.fact_store.values()) - before
        stats['digests'] = sum(bool(e.digest) and e.id not in prior_digests for e in self.episodes_doc.values()
                                 if e.user_id == user_id and e.message_id is not None)
        return stats

    def _import_documents(self, sessions, user_id='default', consolidate=None, generate_digests=True, roles=True,
                          batch_size=256, base_time=None, dedupe=True, prepared_vectors=None):
        import hashlib
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError('batch_size must be a positive integer')
        records = document_inputs(sessions, roles, base_time)
        def fingerprint(record):
            return hashlib.sha1(f"{user_id}|{record['session_id']}|{record['content']}".encode()).hexdigest()
        with self._pipeline_lock:
            candidates = [r for r in records if not dedupe or fingerprint(r) not in self._import_seen]
        if consolidate is not False and candidates:
            require_llm(self.llm)
        if prepared_vectors is None:
            texts = list(dict.fromkeys(r['content'] for r in candidates))
            vectors = []
            for start in range(0, len(texts), batch_size):
                batch = texts[start:start + batch_size]
                encoded = self._encode_batch(batch)
                if len(encoded) != len(batch):
                    raise ValueError('Embedding batch length mismatch')
                vectors.extend(encoded)
            prepared_vectors = dict(zip(texts, vectors))
        added, skipped = [], 0
        from ._pipeline import memory_adapter
        with memory_adapter(self, user_id).write_lock(user_id):
            for record in records:
                key = fingerprint(record)
                if dedupe and key in self._import_seen:
                    skipped += 1
                    continue
                ep = self.add(record['content'], user_id, record['session_id'], role='document',
                              event_time=record['event_time'], embedding=prepared_vectors[record['content']])
                ep.source_kind, ep.scope = 'document', 'raw' if consolidate is False else 'long'
                ep.consolidated = consolidate is False
                ep.metadata.update(record['metadata'])
                if record['title']:
                    ep.metadata['title'] = record['title']
                self._sync_episode(ep)
                self._import_seen.add(key)
                added.append(ep)
        stats = dict(sessions=len({ep.session_id for ep in added}), episodes=len(added), skipped=skipped,
                     facts_added=0, digests=0, facts_deferred=0, deferred_reason=None, episode_ids=[ep.id for ep in added])
        if consolidate is not False and added:
            from ._pipeline import execute_memory
            stats.update(execute_memory(self, user_id, added, 'extract', digests=generate_digests))
        elif generate_digests and added:
            stats['digests'] = self.generate_digests(added)
        return stats


    def import_data(self, data, format: str = "auto", user_id: str = "default",
                    session_id: str = "imported", **kwargs) -> dict[str, int]:
        """便捷方法：解析原始导出（ChatGPT/OpenAI/JSONL/transcript——自动嗅探）并一次调用导入。
        格式见 `mnemotrace.connectors.parse`。"""
        from .connectors import parse
        return self.import_messages(parse(data, format=format, session_id=session_id),
                                    user_id=user_id, **kwargs)

    def import_snapshot(self, snapshot: dict, user_id: str = "default", dedupe: bool = True) -> dict:
        """恢复 忆痕 导出快照（`/v1/export` / `MemoryService.export`）——可移植性往返。快照是
        已经整合过的记忆，所以这是恢复而非写入：fact 携带完整的双时间轴时间戳和取代链直接落库
        （重跑抽取会使其重复并抹平历史），episode 则重新进入带日期的原始日志并标记为已整合，
        System-2 永远不会重新消化它们。share-safe 导出不含 episode——只恢复 fact 是那里的预期
        结果。在 `dedupe`（默认）下幂等。"""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        from .store._validation import validate_snapshot

        snapshot = validate_snapshot(snapshot)
        user = user_id

        # --- episodes：恢复无损带日期日志（仅 include_sensitive 导出中存在） ---
        ep_seen = {ep.metadata.get('snapshot_source_id') for ep in self.episodes_doc.values()
                   if ep.user_id == user and ep.metadata.get('snapshot_source_id')}
        eps_skipped = 0
        prepared_episodes: list[Episode] = []
        for row in snapshot["episodes"]:
            content = row["content"]
            session_id = row["session_id"]
            if dedupe and row['id'] in ep_seen:
                eps_skipped += 1
                continue
            ep = Episode(content=content, user_id=user, session_id=session_id,
                         role=row["role"], event_time=row["event_time"],
                         consolidated=True,  # 它的 fact 在下方恢复；绝不重新抽取
                         digest=row.get("digest", ""), coverage=row.get('coverage'), digest_coverage=row.get('digest_coverage'),
                         metadata={'snapshot_source_id': row['id']})
            ep.embedding = self._encode(content)
            if ep.digest:
                ep.digest_embedding = self._encode(ep.digest)
            prepared_episodes.append(ep)
            ep_seen.add(row['id'])

        # --- facts：同时恢复 live 与被取代的行，保留 valid/transaction 时间 ---
        def fact_key(subject, predicate, obj, valid_at, was_held):
            return (subject, predicate, obj, round(valid_at, 1) if valid_at is not None else None, was_held)

        fact_seen = {fact_key(f.subject, f.predicate, f.object, f.valid_at,
                              f.review_pending or f.approved_at is not None): f.id
                     for f in self._all_facts(include_unpublished=True) if f.user_id == user}
        id_map: dict[str, str] = {}  # 旧快照 id -> 新 id，用于重连取代链
        staged: list[tuple[Fact, str | None]] = []
        facts_skipped = 0
        for row in snapshot["facts"]:
            s, p, o = row["subject"], row["predicate"], row["object"]
            valid_at = row["valid_at"]
            key = fact_key(s, p, o, valid_at,
                           row.get('review_pending', False) or row.get('approved_at') is not None)
            if dedupe and key in fact_seen:
                id_map[row["id"]] = fact_seen[key]
                facts_skipped += 1
                continue
            f = Fact(subject=s, predicate=p, object=o, user_id=user,
                     text=row.get("text", ""), source=row["source"],
                     source_roles=row.get("source_roles", []),
                     sensitive=row["sensitive"],
                     sensitive_override=row.get("sensitive_override", False),
                     review_pending=row.get("review_pending", False), approved_at=row.get("approved_at"),
                      categories=row["categories"], categories_source=row["categories_source"], object_kind=row["object_kind"],
                      cardinality=row["cardinality"], retention=row["retention"],
                     salience=row["salience"], confidence=row["confidence"],
                     valid_at=valid_at,
                     invalid_at=row["invalid_at"], created_at=row["created_at"],
                     expired_at=row["expired_at"], validity_revisions=row["validity_revisions"],
                     # 来源 episode id：原样保留以保审计诚实——被引用的 episode 在这里拿到了新 id，
                     # 这些字段记录的是该 fact 当初来自哪里。
                     provenance=row["provenance"])
            f.embedding = self._encode(f.text)
            id_map[row["id"]] = f.id
            staged.append((f, row["supersedes"]))
            fact_seen[key] = f.id
        known_pairs = {item.pair_key for item in self.conflicts.values() if item.user_id == user}
        restored_conflicts = []
        for row in snapshot['conflicts']:
            fields = {key: value for key, value in row.items() if key not in {'id', 'older', 'newer'}}
            conflict = Conflict(id_map[row['older']], id_map[row['newer']], user_id=user, **fields)
            if conflict.older != conflict.newer and conflict.pair_key not in known_pairs:
                restored_conflicts.append(conflict)
                known_pairs.add(conflict.pair_key)
        # 校验和向量准备全部成功后才写入；取代链也链接到去重命中的已有事实。
        for ep in prepared_episodes:
            self._sync_episode(ep)
        for f, old_sup in staged:
            f.supersedes = id_map.get(old_sup) if old_sup else None
            self.fact_store.upsert(f.id, f.embedding, f)
            # add_fact 会把 fact.invalid_at 带到关系上，因此被取代的历史落库时就是已失效化的边
            # ——图的时间线在往返中得以保留。
            self.writer.graph_builder.add_fact(f)

        self.conflicts.update((conflict.id, conflict) for conflict in restored_conflicts)
        self._enforce_hot_limit()
        self._persona_cache.clear()
        return {"format": "mnemotrace", "facts_restored": len(staged), "facts_skipped": facts_skipped,
                "episodes_restored": len(prepared_episodes), "episodes_skipped": eps_skipped,
                "malformed": 0}

    # --- 用户手写的记忆管理（管理界面驱动的可编辑层） ---
    def add_fact(self, subject: str, predicate: str, object: str, user_id: str = "default",
                 valid_at: Optional[float] = None, sensitive: Optional[bool] = None,
                 categories: list[Category] | None = None, object_kind: ObjectKind = "unknown",
                 cardinality: Cardinality = "unknown", retention: Retention = "unknown") -> Fact:
        """手动断言一条 fact。它被标记为 source='user'（权威）：冲突解决允许它取代同一槽位上
        任何抽取出的值，并且此后受到保护，不会被未来的自动覆盖改写。"""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        user = user_id
        f = build_manual_fact(subject, predicate, object, user, valid_at, sensitive, categories, object_kind, cardinality, retention)
        f.embedding = self._encode(f.text)
        from .consolidate.pipeline import PipelineTasks
        PipelineTasks(self, f.user_id).invalidate()
        history = [x for x in self._all_facts() if x.slot == f.slot]
        action, changed = reconcile_manual(f, history, now())
        for old in changed:
            self._upsert_fact(old, tier="hot")
            self.writer.graph_builder.add_fact(old)
        if changed:
            self.graph.prune_orphan_entities()
        if action != "duplicate":
            self.fact_store.upsert(f.id, f.embedding, f)
            self.writer.graph_builder.add_fact(f)
        self._enforce_hot_limit()
        self._persona_cache.clear()
        return f

    def update_fact(self, fact_id: str, subject: Optional[str] = None, predicate: Optional[str] = None,
                    object: Optional[str] = None, sensitive: Optional[bool] = None,
                    categories: list[Category] | None = None, statement: Optional[str] = None,
                    object_kind: ObjectKind | None = None,
                    cardinality: Cardinality | None = None, retention: Retention | None = None) -> Optional[Fact]:
        """实际修改 fact 内容时标记为用户手写（因此自动抽取不会把它改回去）。
        修改正文时默认标记敏感并清空分类；显式参数优先
        （用户的选择永远优先）。只修改元数据或回传未变的内容时，保留来源、有效状态与历史。"""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        f = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
        if f is None or not f.is_published():
            return None
        edited = copy(f)
        claim_changed = apply_manual_edit(edited, subject, predicate, object, sensitive, categories,
                                          statement, object_kind, cardinality, retention)
        projection_changed = claim_changed or edited.object_kind != f.object_kind
        if claim_changed:
            edited.embedding = self._encode(edited.text)
            from .consolidate.pipeline import PipelineTasks
            PipelineTasks(self, f.user_id).invalidate()
        vars(f).update(vars(edited))
        self._upsert_fact(f, tier="hot")
        if projection_changed:
            self.writer.graph_builder.add_fact(f)
            self.graph.prune_orphan_entities()
        self._persona_cache.clear()
        return f

    def delete_fact(self, fact_id: str) -> bool:
        """被遗忘权：硬删除一条 fact（区别于保留历史的自动失效化）。这是用户主动发起的擦除，
        数据真的被移除——热层和冷层都删。"""
        existed = self.fact_store.get(fact_id) is not None or self.cold_store.get(fact_id) is not None
        if existed and self._pipeline_state:
            from .consolidate.pipeline import PipelineTasks
            record = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
            PipelineTasks(self, record.user_id).invalidate()
        self.graph.delete_relations_for_fact(fact_id)
        self.graph.prune_orphan_entities()
        self.fact_store.delete(fact_id)
        self.cold_store.delete(fact_id)
        self._persona_cache.clear()
        return existed

    # --- 关注领域："关注点"定制（记忆应强调/抑制什么） ---
    def set_focus(self, track: Optional[list[str]] = None, mute: Optional[list[str]] = None) -> dict:
        """定制记忆的优先级。真实生效，不只是个标签：
          * `track` 主题获得显著度提升——显著度是一等检索评分信号 (AGENTS.md 「检索与模块」 w_sal)，
            且豁免衰减/驱逐，因此被 track 的主题确实排名更高并留在热层。
          * `mute` 主题从组装的读上下文（lean_context）和 persona 中被抑制。
        传入 None 表示该列表不变；传入 [] 表示清空。"""
        if track is not None:
            self.focus["track"] = [t.strip() for t in track if t.strip()]
        if mute is not None:
            self.focus["mute"] = [m.strip() for m in mute if m.strip()]
        self.apply_focus()
        self._persona_cache.clear()
        return self.get_focus()

    def get_focus(self) -> dict:
        return {"track": list(self.focus.get("track", [])), "mute": list(self.focus.get("mute", []))}

    @staticmethod
    def _matches(f: Fact, terms: list[str]) -> bool:
        if not terms:
            return False
        hay = f.text.lower()
        return any(t.lower() in hay for t in terms)

    def apply_focus(self, boost: float = 0.5, cap: float = 5.0) -> int:
        """给所有命中 track 主题的已存 fact 提升显著度，让用户声明的优先级确实排名更高并抵抗
        衰减/驱逐。设上限，使反复编辑趋于饱和而非无限膨胀。返回被提升的 fact 数量。"""
        track = self.focus.get("track", [])
        if not track:
            return 0
        n = 0
        for f in list(self.fact_store.values()) + list(self.cold_store.values()):
            if self._matches(f, track):
                f.salience = min(cap, f.salience + boost)
                self._upsert_fact(f)
                n += 1
        return n

    def graph_data(
        self,
        user_id: str = "default",
        as_of: Optional[float] = None,
        include_sensitive: bool = True,
        q: str = "",
        live_only: bool = False,
        limit: Optional[int] = None,
    ) -> dict:
        """把语义图导出为节点 + 边，供关系图谱可视化使用。实体是节点；关系是携带 predicate 和
        双时间轴（live/被取代）状态的边。孤立实体（没有存留边）被丢弃，让画面聚焦于关系本身。

        `include_sensitive=False` 返回仅由未标记敏感的 fact 派生的 share-safe 图。
        `limit` 限制筛选后的关系数；`has_more` 表示仍有未返回的匹配关系。
        """
        user = user_id
        ents = {e.id: e for e in self.graph.entities.values() if e.user_id == user}
        relations = self.graph.relations()
        at = now() if as_of is None else as_of
        needle = q.strip().lower()
        edges, touched = [], set()
        has_more = False
        for r in relations:
            if r.subject_id not in ents or r.object_id not in ents:
                continue
            fact = self.fact_store.get(r.fact_id) or self.cold_store.get(r.fact_id)
            if fact is None or not fact.is_published(at):
                continue
            live = fact.is_live(at)
            if (live_only or as_of is not None) and not live:
                continue
            if not include_sensitive and getattr(fact, "sensitive", False):
                continue
            subject_name = ents[r.subject_id].name
            object_name = ents[r.object_id].name
            haystack = " ".join([
                subject_name,
                object_name,
                r.predicate,
                getattr(fact, "text", ""),
                getattr(fact, "object", ""),
                " ".join(fact.categories),
            ]).lower()
            if needle and needle not in haystack:
                continue
            # 仅用额外一条匹配关系判断截断，不把它的端点加入返回节点。
            if limit is not None and limit > 0 and len(edges) >= limit:
                has_more = True
                break
            edges.append({
                "source": r.subject_id,
                "target": r.object_id,
                "predicate": r.predicate.replace("_", " "),
                "live": live,
                "fact_id": r.fact_id,
                "fact_text": fact.text if fact is not None else "",
                "valid_at": r.valid_at,
                "valid_at_h": fmt_date(r.valid_at),
                "invalid_at": r.invalid_at,
                "invalid_at_h": fmt_date(r.invalid_at) if r.invalid_at is not None else None,
                "provenance": list(fact.provenance) if fact is not None else [],
            })
            touched.add(r.subject_id)
            touched.add(r.object_id)
        nodes = [{"id": eid, "name": ents[eid].name, "type": ents[eid].type} for eid in touched]
        return {"nodes": nodes, "edges": edges, "has_more": has_more}

    # --- 记忆策略：可编辑 prompt + "要记录什么"指令（记忆策略页面） ---
    def get_policy(self) -> dict:
        """返回用户覆盖项和内置默认值，让控制台能展示实际生效的 prompt，并允许用户在其基础上
        编辑。"""
        return {"policy": dict(self.policy), "defaults": dict(POLICY_DEFAULTS)}

    def set_policy(self, **fields: str) -> dict:
        """更新策略字段（extract_instruction / extract_system / digest_system / persona_system）。
        空字符串清除覆盖项（回退到默认值）。后续流水线按新策略准备并校验输入。"""
        for k, v in fields.items():
            if k in self.policy and v is not None:
                self.policy[k] = v
        self._apply_policy()
        self._persona_cache.clear()
        return self.get_policy()

    def _effective(self, key: str) -> str:
        """返回实际生效的策略字段值（用户覆盖或默认值）。"""
        return self.policy.get(key) or POLICY_DEFAULTS[key]

    def _apply_policy(self):
        self.writer.config = self.config


    # --- 冲突检测 -> pending（LLM 检测出含糊的部分；由用户确认） ---
    def pending_conflicts(self, user_id: str = "default") -> list[Conflict]:
        """等待用户裁决的当前冲突及有效区间内的手动历史冲突。"""
        from .consolidate.adjudication import conflict_is_reviewable

        user, at = user_id, now()
        out = []
        for c in self.conflicts.values():
            if c.user_id != user or c.status != "pending":
                continue
            a = self.fact_store.get(c.older) or self.cold_store.get(c.older)
            b = self.fact_store.get(c.newer) or self.cold_store.get(c.newer)
            if conflict_is_reviewable(a, b, at):
                out.append(c)
        return out

    def resolve_conflict(self, conflict_id: str, keep: str = "newer") -> bool:
        """Apply an explicit user decision to a pending conflict."""
        if keep not in {'older', 'newer', 'both', 'dismiss'}:
            raise ValueError('Invalid conflict resolution')
        c = self.conflicts.get(conflict_id)
        if c is None or c.status != 'pending':
            return False
        from .consolidate.adjudication import (
            approval_candidate, conflict_is_reviewable, has_other_manual_blocker, resolve_facts,
        )
        from .consolidate.pipeline import PipelineTasks

        def get_fact(fact_id):
            return self.fact_store.get(fact_id) or self.cold_store.get(fact_id)

        facts = {key: get_fact(getattr(c, key)) for key in ('older', 'newer')}
        at = now()
        if keep != 'dismiss':
            if not conflict_is_reviewable(facts['older'], facts['newer'], at):
                return False
            candidate = approval_candidate(facts, keep)
            if candidate is not None:
                related = ((pair, get_fact(pair.newer if pair.older == candidate.id else pair.older))
                           for pair in self.conflicts.values()
                           if pair.user_id == c.user_id and candidate.id in (pair.older, pair.newer))
                if has_other_manual_blocker(candidate, conflict_id, related, at):
                    return False
        PipelineTasks(self, c.user_id).invalidate()
        history = ()
        if keep in facts and facts[keep].review_pending:
            history = (f for f in self._all_facts() if f.user_id == c.user_id)
        for fact in resolve_facts(facts, keep, at, history):
            self._upsert_fact(fact)
            self.writer.graph_builder.add_fact(fact)
        self.graph.prune_orphan_entities()
        self._persona_cache.clear()
        c.status = 'dismissed' if keep == 'dismiss' else 'resolved'
        return True

    def dismiss_conflict(self, conflict_id: str) -> bool:
        return self.resolve_conflict(conflict_id, 'dismiss')

    def generate_digests(self, episodes):
        """Explicit digest operation using the shared ledger and bounded runner."""
        from ._pipeline import execute_memory
        originals = [self.episodes_doc.get(value.id) for value in episodes]
        originals = [ep for ep in originals if ep is not None and not ep.digest and len(ep.content) >= self.config.digest_min_chars]
        if originals:
            require_llm(self.llm)
        count = 0
        for user in dict.fromkeys(ep.user_id for ep in originals):
            count += execute_memory(self, user, [ep for ep in originals if ep.user_id == user], 'digest')['digests']
        return count


    def retrieve_digests(
        self,
        query: str,
        user_id: str = "default",
        k: int = 8,
        as_of: Optional[float] = None,
        *, known_at: float | None = None,
    ) -> list[EventEvidence]:
        """为查询取 top-k 条 session 摘要，使用与 fact/episode 检索相同的 hybrid（dense + BM25,
        RRF）信号。词法匹配在这里很重要：聚合类问题（"how many trips"）依赖摘要里提到的精确词，
        纯嵌入查找可能把它们排在模糊相似项之后。返回只包含选定文本的只读事件证据。"""
        return self._event_candidates(query, user_id, as_of, k, max(k * 3, 30),
                                      digests_only=True, known_at=known_at)[:k]

    def _event_candidates(self, query, user_id, as_of, k, pool, *, digests_only=False,
                          known_at=None, start=None, end=None):
        from .retrieve.fusion import weighted_rrf
        from .retrieve.hybrid import date_terms, order_positive

        def visible(ep):
            return (ep.user_id == user_id
                    and (known_at is None or ep.ingested_at <= known_at)
                    and (as_of is None or (ep.event_time is not None and ep.event_time <= as_of))
                    and (start is None or (ep.event_time is not None and ep.event_time >= start))
                    and (end is None or (ep.event_time is not None and ep.event_time < end)))

        episodes = {ep.id: ep for ep in self.episodes_doc.values()
                    if visible(ep) and (not digests_only or ep.digest)}
        documents = {}
        for ep in episodes.values():
            date = date_terms(ep.event_time)
            documents[(ep.id, 'text')] = f'{ep.content} {date}'
            if ep.digest:
                documents[(ep.id, 'digest')] = f'{ep.digest} {date}'
        lexical = {}
        for (episode_id, _), score in bm25_scores(query, list(documents.items())).items():
            lexical[episode_id] = max(score, lexical.get(episode_id, 0.0))
        semantic = {}
        if self.embedder is not None:
            vector = self.embedder.embed(query)
            hits = self.digest_vec.search(vector, pool, where=visible)
            if not digests_only:
                hits += self.episodes_vec.search(vector, pool, where=visible)
            for score, ep in hits:
                if ep.id in episodes:
                    semantic[ep.id] = max(score, semantic.get(ep.id, 0.0))
        scores = weighted_rrf({'sem': order_positive(semantic)[:pool],
                               'lex': order_positive(lexical)[:pool]}, {})
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        return [event_evidence(episodes[key]) for key in ranked[:max(k, pool)]]

    def evict_cold(self, max_hot: int) -> int:
        """冷热分层分页（MemoryOS，AGENTS.md 「架构概览与代码导航」 / Bet E）。当热层 fact 集超过容量时，把最冷的
        fact（显著度最低、访问最旧）换页到冷层——但持久的身份/偏好 fact 除外，它们保持热层。
        非破坏性：冷 fact 是移动而非删除，历史与 as-of 查询保持完整，未来的查询仍可把它们换页
        回来。正是这一点让检索成本在 10M-token 规模下保持 O(hot working set) 而非
        O(all history)。返回换出数量。"""

        hot = self.fact_store.values()
        if len(hot) <= max_hot:
            return 0
        evictable = [f for f in hot if f.retention != "durable"]
        evictable.sort(key=lambda f: (f.salience, f.last_access))  # 最冷的（显著度最低/最旧）优先
        n_out = min(len(evictable), len(hot) - max_hot)
        for f in evictable[:n_out]:
            self.cold_store.upsert(f.id, f.embedding, f)  # 保留在冷层
            self.fact_store.delete(f.id)  # 仅从热层索引移除
            self.cold_pages_out[f.user_id] = self.cold_pages_out.get(f.user_id, 0) + 1
        return n_out

    def _enforce_hot_limit(self) -> int:
        max_hot = int(getattr(self.config, "max_hot_facts", 0) or 0)
        if max_hot <= 0:
            return 0
        return self.evict_cold(max_hot)

    def _page_hot(self, facts: list[Fact]) -> None:
        """把检索命中的冷层 fact 提升回热层索引。fact id 和图的边保持不变。"""
        moved = False
        for f in facts:
            if self.cold_store.get(f.id) is not None:
                self.cold_store.delete(f.id)
                reinforce(f, self.config.access_boost)
                self.fact_store.upsert(f.id, f.embedding, f)
                self.cold_pages_in[f.user_id] = self.cold_pages_in.get(f.user_id, 0) + 1
                moved = True
        if moved:
            self._enforce_hot_limit()

    def _retrieve_cold(
        self,
        query: str,
        user: str,
        as_of: Optional[float],
        top_k: Optional[int],
    ) -> tuple[list[tuple[Fact, float]], dict]:
        ranked, diag = self.cold_retriever.retrieve(query, user, as_of, top_k)
        if ranked and as_of is None:
            self._page_hot([f for f, _ in ranked])
        return ranked, diag

    def structured_profile(self, user_id: str = "default") -> dict:
        """L2 结构化 profile：把用户的 live fact 按 基本信息/偏好/习惯 分组，并为展示区分
        "已确认"与"待确认"。这是只读的派生视图——从不过滤 fact store 或检索路径，因此召回
        不受影响（search/lean_context 仍看到全部 fact）。"""
        from .consolidate.structured import build_structured_profile
        user = user_id
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live()]
        return build_structured_profile(live)

    def build_persona(self, user_id: str = "default") -> str:
        """L3：由 live fact 合成的紧凑叙事式 profile（偏好/习惯/所有物）。"""
        user = user_id
        if user in self._persona_cache:
            return self._persona_cache[user]
        subject = "user"
        mute = self.focus.get("mute", [])
        live = [f for f in self._all_facts()
                if f.user_id == user and f.is_live() and not self._matches(f, mute)]
        persona = (self.profiles.narrative(subject, live, llm=self.llm,
                                           system=self._effective("persona_system")) if live else "")
        track = self.focus.get("track", [])
        if track:  # 把用户声明的优先级呈现在 profile 中
            line = "FOCUS AREAS (user asked to prioritize): " + ", ".join(track)
            persona = (persona + "\n" + line).strip() if persona else line
        self._persona_cache[user] = persona
        return persona

    def _persona_at(self, user: str, as_of: Optional[float]) -> str:
        """供读上下文使用的叙事式 profile。当前 profile 会被缓存；as-of profile 不缓存，因为
        时间戳是视图的一部分，不能把当前 fact 泄漏进过去上下文。"""
        if as_of is None:
            return self.build_persona(user)
        subject = "user"
        mute = self.focus.get("mute", [])
        live = [
            f for f in self._all_facts()
            if f.user_id == user and f.is_live(as_of) and not self._matches(f, mute)
        ]
        return self.profiles.narrative(
            subject,
            live,
            llm=self.llm,
            system=self._effective("persona_system"),
        ) if live else ""

    def _chain_facts_for_seeds(
        self,
        seeds: list[Fact],
        user: str,
        as_of: Optional[float],
        limit: int = 12,
        redact_sensitive: bool = False,
    ) -> list[Fact]:
        """把检索到的 fact 扩展为对其可见的取代链。

        这是非破坏性失效化的读路径一侧：检索仍只对 live 的槽位头部排序，但上下文组装可以展示
        有界的链，说明它取代了什么。`as_of` 守卫刻意基于 valid 时间，与现有时间点搜索语义一致，
        因此过去的视图绝不会泄漏未来的取代值。
        """
        if not seeds:
            return []
        view_time = now() if as_of is None else as_of
        facts = [f for f in self._all_facts() if f.user_id == user]
        if redact_sensitive:
            facts = [f for f in facts if not getattr(f, "sensitive", False)]
        by_id = {f.id: f for f in facts}
        replaces: dict[str, list[Fact]] = {}
        for f in facts:
            if f.supersedes:
                replaces.setdefault(f.supersedes, []).append(f)

        def visible(f: Fact) -> bool:
            return (f.valid_at is not None and f.valid_at <= view_time)

        selected: dict[str, Fact] = {}

        def add_visible(f: Fact) -> None:
            if visible(f):
                selected.setdefault(f.id, f)

        def add_chain(seed: Fact) -> None:
            add_visible(seed)
            cur = seed
            seen: set[str] = {seed.id}
            while cur.supersedes and cur.supersedes in by_id and cur.supersedes not in seen:
                cur = by_id[cur.supersedes]
                seen.add(cur.id)
                add_visible(cur)
            queue = [seed]
            while queue:
                cur = queue.pop(0)
                for newer in sorted(replaces.get(cur.id, []), key=lambda f: time_key(f.valid_at)):
                    if newer.id in seen:
                        continue
                    seen.add(newer.id)
                    add_visible(newer)
                    queue.append(newer)

        for seed in seeds:
            add_chain(seed)

        # 单条可见 fact 已在 FACTS 中展示；只有当链补充了解释性证据（如被取代的值或可见的
        # 替代值）时才渲染。
        if len(selected) <= 1:
            return []
        return sorted(selected.values(), key=lambda f: (f.subject.lower(), f.predicate.lower(), time_key(f.valid_at)))[:limit]

    def _fact_evolution_block(
        self,
        seeds: list[Fact],
        user: str,
        as_of: Optional[float],
        query: str = "",
        limit: int = 12,
        redact_sensitive: bool = False,
    ) -> str:
        if query:
            seeds = [f for f in seeds if overlap_terms(query, chain_terms(f))]
        rows = self._chain_facts_for_seeds(
            seeds,
            user,
            as_of,
            limit=limit,
            redact_sensitive=redact_sensitive,
        )
        if not rows:
            return ""
        replaced_ids = {f.supersedes for f in self._all_facts() if f.supersedes}

        def _role(f: Fact) -> str:
            if f.is_live(as_of):
                return "current"
            if f.id in replaced_ids:
                return "superseded"
            return "replacement" if f.supersedes else "past"

        if self.config.chain_current_first:
            # 按槽位分组，把当前值放在最前，历史缩进在其下方。扁平表格迫使阅读者扫描 `role` 列
            # 找哪一行是"现在为真"，而在知识更新类问题上，回答者选中被取代值的频率高到足以输给
            # 朴素的 full-context（例如在有明确搬家记录后仍回答旧城市）。把当前值作为一条断言
            # （而非一行）放在最前面，消除了这种扫描。
            slots: dict[tuple[str, str], list[Fact]] = {}
            for f in rows:
                slots.setdefault((f.subject, f.predicate), []).append(f)
            blocks = []
            for (subj, pred), facts in slots.items():
                facts.sort(key=lambda x: time_key(x.valid_at), reverse=True)
                attr = pred.replace("_", " ")
                live = next((f for f in facts if f.is_live(as_of)), None)
                head = live or facts[0]
                label = "CURRENT" if live is not None else f"LATEST KNOWN ({_role(head)})"
                blocks.append(f"{label}: {subj} {attr} = {head.object}  (since {fmt_date(head.valid_at)})")
                for f in facts:
                    if f is head:
                        continue
                    until = fmt_date(f.invalid_at) if f.invalid_at is not None else "?"
                    blocks.append(
                        f"    was: {f.object}  ({fmt_date(f.valid_at)} - {until}, {_role(f)})"
                    )
            return ("FACT EVOLUTION (current value first, then what it replaced):\n"
                    + "\n".join(blocks))

        lines = [
            "valid from | valid until | subject | attribute | value | role",
            "--- | --- | --- | --- | --- | ---",
        ]
        for f in rows:
            until = fmt_date(f.invalid_at) if f.invalid_at is not None else "current"
            lines.append(
                f"{fmt_date(f.valid_at)} | {until} | {f.subject} | "
                f"{f.predicate.replace('_', ' ')} | {f.object} | {_role(f)}"
            )
        return "FACT EVOLUTION (retrieved supersession chain):\n" + "\n".join(lines)

    @staticmethod
    def _snippet_for_evidence(content: str, needle: str, max_chars: int = 360) -> str:
        """从已选定的事件表示中截取与查询/fact 匹配的行或句子。

        输入已经执行摘要优先规则，此处不读取原文来补充被压缩的信息。
        """
        text = " ".join(content.split())
        if len(text) <= max_chars:
            return text
        parts = [p.strip() for p in re.split(r"(?<=[.!?。！？])\s+|\n+", content) if p.strip()]
        best = ""
        best_score = -1
        for part in parts:
            score = len(overlap_terms(needle, part))
            if score > best_score:
                best = part
                best_score = score
        snippet = " ".join((best or text).split())
        if len(snippet) <= max_chars:
            return snippet
        return snippet[: max_chars - 1].rstrip() + "…"

    def _provenance_raw_block(
        self,
        facts: list[Fact],
        query: str,
        user: str,
        as_of: Optional[float],
        exclude_episode_ids: set[str],
        limit: int = 4,
        snippet_chars: int = 360,
        redact_sensitive: bool = False,
    ) -> str:
        """为检索到的 fact 提供紧凑的来源 episode。

        只使用被检索 fact 明确引用的事件表示，排除已渲染的事件，遵守 `as_of`。
        有摘要时只截取摘要；在脱敏上下文中禁用该块，因为事件文本可能包含未被 fact 分类器识别的敏感内容。
        """
        if redact_sensitive or not facts:
            return ""
        rows: list[str] = []
        seen_eps: set[str] = set(exclude_episode_ids)
        view_time = now() if as_of is None else as_of

        for fact in sorted(facts, key=lambda fact: time_key(fact.valid_at), reverse=True):
            if getattr(fact, "sensitive", False):
                continue
            if query and not overlap_terms(query, chain_terms(fact)):
                continue
            for ep_id in fact.provenance:
                if ep_id in seen_eps:
                    continue
                ep = self.episodes_doc.get(ep_id)
                if ep is None or ep.user_id != user or (ep.event_time is not None and ep.event_time > view_time):
                    continue
                ep = event_evidence(ep)
                seen_eps.add(ep_id)
                needle = f"{query} {fact.text} {fact.subject} {fact.predicate} {fact.object}"
                snippet = self._snippet_for_evidence(ep.text, needle, max_chars=snippet_chars)
                date = ep.metadata.get("date") or fmt_date(ep.event_time)
                rows.append(
                    f"- [{date}] (episode: {ep.id}; space: {ep.user_id}; session: {ep.session_id}; supports: "
                    f"{fact.subject} {fact.predicate.replace('_', ' ')} {fact.object}) {snippet}"
                )
                if len(rows) >= limit:
                    return "PROVENANCE EVENT EVIDENCE (selected sources for retrieved facts):\n" + "\n".join(rows)
        return "PROVENANCE EVENT EVIDENCE (selected sources for retrieved facts):\n" + "\n".join(rows) if rows else ""


    @staticmethod
    def _merge_promoted_chunks(promoted: list, retrieved: list, k: int) -> list:
        """把经来源提升的 episode 与语义检索到的 episode 合并，并设语义下限：提升最多占用
        chunk 预算的一半。

        提升的 chunk 以排序后的 FACT 为键，因此当 fact 检索未命中时，语义 episode 是唯一仍能
        携带答案的路径。若放任提升占满整个预算，恰好会把这些 episode 挤出去，在单 session 召回
        上造成正确->拒答的回归（2026-08-27，相对 headline 日志的 50 项 A/B）；设上限保住了
        旧值/时间类收益，又不在此处付出代价。未用完的提升预算回流给检索 chunk，反之亦然。
        k=1 时唯一槽位保持提升优先（修复前的行为；k=2 采集的回归数据对它没有结论，且现有测试
        钉死了该行为）。
        """
        cap = max(1, k // 2)
        merged: list = []
        seen: set[str] = set()
        for ep in promoted[:cap]:
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        for ep in retrieved:
            if len(merged) >= k:
                break
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        for ep in promoted[cap:]:  # 仅当检索填不满预算时回填
            if len(merged) >= k:
                break
            if ep.id not in seen:
                seen.add(ep.id)
                merged.append(ep)
        return merged

    def _provenance_detail_chunks(
        self,
        facts: list[Fact],
        query: str,
        user: str,
        as_of: Optional[float],
        limit: int,
        redact_sensitive: bool = False,
    ) -> list[EventEvidence]:
        """按 fact 来源提升选定的事件表示，仍遵守同一 `n_chunks` 预算。

        摘要事件的匹配和返回值都只使用摘要。
        """
        if limit <= 0 or redact_sensitive or not facts:
            return []
        view_time = now() if as_of is None else as_of
        scored: dict[str, tuple[int, int, float, EventEvidence]] = {}
        for fact_rank, fact in enumerate(facts):
            if getattr(fact, "sensitive", False):
                continue
            fact_terms = chain_terms(fact)
            fact_overlap = len(overlap_terms(query, fact_terms)) if query else 0
            if query and fact_overlap == 0:
                continue
            for ep_id in fact.provenance:
                ep = self.episodes_doc.get(ep_id)
                if ep is None or ep.user_id != user or (ep.event_time is not None and ep.event_time > view_time):
                    continue
                ep = event_evidence(ep)
                ep_overlap = len(overlap_terms(query, ep.text)) if query else 0
                support_overlap = len(overlap_terms(fact.text, ep.text))
                score = fact_overlap * 8 + support_overlap * 3 + ep_overlap * 2
                prev = scored.get(ep.id)
                item = (score, fact_rank, time_key(ep.event_time), ep)
                if prev is None or (score, -fact_rank, time_key(ep.event_time)) > (prev[0], -prev[1], prev[2]):
                    scored[ep.id] = item
        ranked = sorted(scored.values(), key=lambda item: (-item[0], item[1], -item[2], item[3].id))
        return [ep for _, _, _, ep in ranked[:limit]]

    @staticmethod
    def _evidence_block_title(block: str) -> str:
        return block.split("\n", 1)[0].rstrip(":：")

    @classmethod
    def _evidence_block_kind(cls, block: str) -> str:
        title = cls._evidence_block_title(block)
        if title.startswith("PROVENANCE EVENT"):
            return "provenance"
        if title.startswith("RELEVANT CONVERSATIONS"):
            return "raw"
        if title.startswith("GRAPH PATHS"):
            return "graph"
        if title.startswith("AGGREGATION"):
            return "aggregation"
        if title.startswith("DURATION"):
            return "duration"
        if title.startswith("FACT HISTORY"):
            return "history"
        if title.startswith("FACT EVOLUTION"):
            return "evolution"
        if title.startswith("CURRENT STATE"):
            return "current_state"
        if title.startswith("当前偏好记录（结构化）"):
            return "preference"
        if title.startswith("FACTS"):
            return "facts"
        if title.startswith("SESSION DIGESTS"):
            return "digests"
        if title.startswith("TIMELINE"):
            return "timeline"
        if title.startswith("USER PROFILE"):
            return "profile"
        return "other"

    @staticmethod
    def _fit_evidence_block(block: str, cap: int) -> str:
        if cap <= 0:
            return ""
        if len(block) <= cap:
            return block
        title, _, body = block.partition("\n")
        if cap <= len(title):
            return title[:cap]
        return (title + "\n" + body[: cap - len(title) - 1]).rstrip()

    def _evidence_priority(self, kind: str) -> int:
        score = {
            "facts": 90,
            "current_state": 88,
            "provenance": 82,
            "raw": 78,
            "graph": 76,
            "history": 75,
            "evolution": 72,
            "timeline": 68,
            "aggregation": 66,
            "duration": 66,
            "preference": 66,
            "digests": 58,
            "profile": 38,
            "other": 40,
        }.get(kind, 40)
        return score

    def _evidence_block_cap(self, kind: str, budget: int) -> int:
        ratio = {
            "aggregation": 0.60,
            "duration": 0.55,
            "provenance": 0.55,
            "raw": 0.55,
            "graph": 0.45,
            "history": 0.45,
            "evolution": 0.42,
            "facts": 0.42,
            "timeline": 0.35,
            "digests": 0.35,
            "current_state": 0.35,
            "preference": 0.35,
            "profile": 0.18,
            "other": 0.25,
        }.get(kind, 0.25)
        cap = int(max(96, budget * ratio))
        return min(cap, budget)

    def _fit_blocks_even(self, selected: list[str], budget: int) -> str:
        if not selected or budget <= 0:
            return ""
        sep = "\n\n"
        sep_chars = len(sep) * (len(selected) - 1)
        if budget <= sep_chars:
            return sep.join(block.split("\n", 1)[0] for block in selected)[:budget]
        available = budget - sep_chars
        quota, extra = divmod(available, len(selected))
        fitted: list[str] = []
        for i, block in enumerate(selected):
            cap = quota + (1 if i < extra else 0)
            fitted.append(self._fit_evidence_block(block, cap))
        return sep.join(fitted)[:budget]

    def _fit_blocks_by_evidence_budget(self, blocks: list[str], budget: int) -> str:
        """面向精简读取路径的意图感知证据打包器。

        旧回退逻辑把紧张的预算在块间均分，或从顶部直接截断，可能把宝贵的 token 花在 profile
        文本上，却丢掉问题真正需要的 raw/provenance/graph 证据。这里按查询意图对证据类别排序，
        给每类设上限，再按原有的可读顺序输出被选中的证据。
        """
        if not blocks or budget <= 0:
            return ""
        sep = "\n\n"
        ranked = sorted(
            enumerate(blocks),
            key=lambda item: (-self._evidence_priority(self._evidence_block_kind(item[1])), item[0]),
        )
        selected: list[tuple[int, str]] = []
        used = 0
        for idx, block in ranked:
            kind = self._evidence_block_kind(block)
            priority = self._evidence_priority(kind)
            sep_cost = len(sep) if selected else 0
            remaining = budget - used - sep_cost
            if remaining <= 0:
                break
            if selected and remaining < 120 and priority < 100:
                continue
            cap = min(len(block), self._evidence_block_cap(kind, budget), remaining)
            fitted = self._fit_evidence_block(block, cap)
            if not fitted:
                continue
            min_title = self._evidence_block_title(block)
            if selected and len(fitted) < min(len(min_title), max(24, remaining)):
                continue
            selected.append((idx, fitted))
            used += sep_cost + len(fitted)
        if not selected:
            return self._fit_evidence_block(blocks[0], budget)
        return sep.join(block for _, block in sorted(selected, key=lambda item: item[0]))[:budget]

    def lean_context(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        n_digests: int = 20,
        n_facts: int = 15,
        n_chunks: int = 2,
        persona: bool = True,
        agentic: bool = False,
        cascade: bool = False,  # _S 最优为关；它是 _M/10M 的扩展原语（由粗到细的下钻）
        timeline: bool = False,  # 追加按时间排序的事件时间线（有助于时序/时长问题）
        char_budget: int = 60_000,
        redact_sensitive: bool = False,  # 安全/共享视图：只显示非敏感的结构化 fact
        *, facts=None,
    ) -> str:
        """可扩展的读路径（AGENTS.md Bet A/E）：从检索到的抽象层组装一个小而组织良好的上下文，
        而不是整段历史——
            L3 persona  +  L1 带日期 fact  +  L2 session 摘要  +  少量完整 chunk 提供细节。

        历史增长时 token 保持大致恒定（检索的是固定大小的切片），这是 full-context 做不到的。
        两个让它既精简又准确的设计点：
          * top-`n_chunks` 个 session 完整展示（细节），并从摘要块中排除，因此没有 session 出现
            两次——每个 token 都买到新信息。
          * 摘要块提供宽时间覆盖（默认 20），这正是聚合类问题（"how many trips"、"list
            everything"）所需的；摘要很小，覆盖因此仍然廉价。
        `char_budget` 对组装出的上下文硬性封顶，使其永远不可能接近全文历史的规模。"""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        user = user_id
        blocks: list[str] = []
        visible = None
        known_at = None
        if facts is not None:
            from dataclasses import replace
            from .store.contracts import FactQuery

            if not isinstance(facts, FactQuery) or facts.after_id is not None or as_of is not None:
                raise ValueError('Expected structured fact conditions or as_of')
            as_of, known_at = facts.valid_at, facts.known_at
            redact_sensitive = redact_sensitive or facts.exclude_sensitive
            visible = tuple(self._structured_facts(user, replace(facts, exclude_sensitive=redact_sensitive)))
        if redact_sensitive:
            # 自由文本层（persona、来源摘要、原始 chunk）可能把敏感内容揉进散文。因此
            # 脱敏上下文刻意只含 fact，且在 fact 级敏感性分类之后，使共享/第三方注入有清晰的
            # 隐私边界。
            n_digests = 0
            n_chunks = 0
            cascade = False

        queries = [query]
        if agentic and not redact_sensitive and self.llm is not None:
            from .retrieve.agentic import AgenticRetriever
            queries += AgenticRetriever(self, self.llm)._subqueries(query)

        # L3 persona 是自由文本合成，可能揉入敏感 fact；脱敏上下文只允许结构化 fact，因此整个
        # 丢弃。
        if persona and not redact_sensitive and facts is None:
            p = self._persona_at(user, as_of)
            if p:
                label = "USER PROFILE" if as_of is None else f"USER PROFILE (as of {fmt_date(as_of)})"
                blocks.append(f"{label}:\n{p}")


        # L1 fact：对每个（子）查询做 hybrid 检索后取并集；再从查询实体做 n-hop 图扩展。
        fact_map: dict[str, Fact] = {}
        for q in queries:
            ranked = self.retriever.retrieve(q, user, as_of, top_k or n_facts, candidates=visible)[0]
            if not ranked and visible is None:
                ranked = self._retrieve_cold(q, user, as_of, top_k or n_facts)[0]
            for f, _ in ranked:
                fact_map.setdefault(f.id, f)
        if visible is None:
            for f in self._graph_related_facts(query, user, as_of, limit=n_facts):
                fact_map.setdefault(f.id, f)
        all_facts = list(fact_map.values())
        # 关注项 "mute"：丢弃用户要求在读上下文中抑制的主题相关 fact。
        mute = self.focus.get("mute", [])
        if mute:
            all_facts = [f for f in all_facts if not self._matches(f, mute)]
        # 敏感性脱敏（feature ⑤）：组装共享/导出上下文时排除敏感 fact。
        if redact_sensitive:
            all_facts = [f for f in all_facts if not getattr(f, "sensitive", False)]
        # 可选：对 fact 池做 cross-encoder 重排。与重排整段 session 不同（cross-encoder 会把
        # session 截断到 512 导致误排——一个已知的 _S 回归），fact 很短，因此重排能锐化 fact
        # 选择且无截断损失。
        if self.reranker is not None and len(all_facts) > (top_k or n_facts):
            order = self.reranker.rerank(query, [(str(i), f.text) for i, f in enumerate(all_facts)],
                                         top_k or n_facts)
            all_facts = [all_facts[int(i)] for i, _ in order]
        self.working_set = all_facts  # 更新查询工作集，供后续检查
        if all_facts:
            if as_of is None and (facts is None or facts.is_current_view):
                for fact in all_facts:
                    self._reinforce_fact(fact)
            by_date = sorted(all_facts, key=lambda f: time_key(f.valid_at), reverse=True)  # 最新在前（更新优先）
            fl = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in by_date)
            state = "current" if facts is None else facts.state.value
            blocks.append(f"FACTS ({state}, dated):\n{fl}")
            # TIMELINE：同样的 fact 按旧->新排列并显式留出间隔，使"最早/最近/间隔多久"能直接
            # 从顺序读出，日期算术为模型铺好而不是留给心算（时间类问题的主要失败模式）。
            if timeline:
                chrono = sorted(all_facts, key=lambda f: time_key(f.valid_at))
                tl = "\n".join(f"- {fmt_date(f.valid_at)}: {f.text}" for f in chrono)
                blocks.append(f"TIMELINE (oldest to newest — use for ordering and durations):\n{tl}")
            if self.config.chain_evidence and facts is None:
                evo = self._fact_evolution_block(
                    all_facts,
                    user,
                    as_of,
                    query=query,
                    limit=max(8, min(18, n_facts + 3)),
                    redact_sensitive=redact_sensitive,
                )
                if evo:
                    blocks.append(evo)

        provenance_facts = list(all_facts)
        if self.config.chain_evidence and all_facts and facts is None:
            seen_chain_facts = {f.id for f in provenance_facts}
            for f in self._chain_facts_for_seeds(
                all_facts,
                user,
                as_of,
                limit=max(8, min(18, n_facts + 3)),
                redact_sensitive=redact_sensitive,
            ):
                if f.id not in seen_chain_facts:
                    seen_chain_facts.add(f.id)
                    provenance_facts.append(f)

        # L2 粗层：对每个（子）查询检索 session 摘要并排序。这是由粗到细级联的粗层
        # （AGENTS.md Bet E / OpenViking）：摘要很小，可以廉价地索引并排序大量 session——这是
        # 突破模型窗口（_M / 10M 前沿）的关键。
        summ_ranked: list[EventEvidence] = []
        seen_s: set[str] = set()
        if n_digests:
            for q in queries:
                for e in self.retrieve_digests(q, user, n_digests, as_of=as_of, known_at=known_at):
                    if e.id not in seen_s:
                        seen_s.add(e.id)
                        summ_ranked.append(e)

        # 细层下钻：级联模式下，细节 chunk 就是排序最高的摘要各自的 session（分数由粗层传播到
        # 细层），因此绝不会对无关 session 的原始轮次做嵌入扫描。非级联时，细节是直接的
        # episode 查找（对小型历史足够）。
        if cascade and summ_ranked:
            detail_eps = summ_ranked[:n_chunks] if n_chunks else []
        else:
            detail_eps = []
            if n_chunks:
                seen_detail: set[str] = set()
                per_query = [
                    self.retrieve_episodes(q, user, max(n_chunks, 1), as_of=as_of, known_at=known_at)
                    for q in queries
                ]
                max_hits = max((len(eps) for eps in per_query), default=0)
                for rank in range(max_hits):
                    for eps in per_query:
                        if rank >= len(eps):
                            continue
                        ep = eps[rank]
                        if ep.id in seen_detail:
                            continue
                        seen_detail.add(ep.id)
                        detail_eps.append(ep)
                        if len(detail_eps) >= n_chunks:
                            break
                    if len(detail_eps) >= n_chunks:
                        break
        detail_ids = {e.id for e in detail_eps}
        if self.config.provenance_chunk_promotion and n_chunks and not redact_sensitive and facts is None:
            promoted = self._provenance_detail_chunks(
                provenance_facts,
                " ".join(queries),
                user,
                as_of,
                limit=n_chunks,
                redact_sensitive=redact_sensitive,
            )
            if promoted:
                detail_eps = self._merge_promoted_chunks(promoted, detail_eps, n_chunks)
                detail_ids = {e.id for e in detail_eps}
        digests = [e for e in summ_ranked if e.id not in detail_ids]

        if digests:
            chrono = sorted(digests, key=lambda e: time_key(e.event_time))
            sm = "\n".join(
                f"- [{e.metadata.get('date') or fmt_date(e.event_time)}] (episode: {e.id}; space: {e.user_id}) {e.text}" for e in chrono
            )
            blocks.append(f"SESSION DIGESTS (relevant, chronological):\n{sm}")

        if detail_eps:
            chunks = "\n\n".join(
                f"原始记录时间：{e.metadata.get('date') or fmt_date(e.event_time)}\n"
                f"会话：{e.session_id}\n"
                f"事件：{e.id}\n空间：{e.user_id}\n表示：{e.representation}; partial={e.partial}; role={e.role}\n记录内容：{e.text}" for e in detail_eps
            )
            blocks.append(f"RELEVANT CONVERSATIONS (full detail):\n{chunks}")

        if self.config.provenance_evidence and facts is None:
            raw_prov = self._provenance_raw_block(
                provenance_facts,
                query,
                user,
                as_of,
                exclude_episode_ids=detail_ids,
                redact_sensitive=redact_sensitive,
            )
            if raw_prov:
                blocks.append(raw_prov)

        assembled = "\n\n".join(blocks)
        if char_budget == 60_000:
            full_chars = sum(len(event_evidence(ep).text) for ep in self.episodes_doc.values() if ep.user_id == user)
            if full_chars:
                # 极小的历史仍需要足够空间容纳 profile + fact + 一个 chunk；否则均分块适配器
                # 可能恰好在约束所在处截断持久的项目规则。
                char_budget = min(char_budget, max(2048, full_chars * 3 - 1))
            if len(assembled) > char_budget:
                if self.config.evidence_budgeting:
                    return self._fit_blocks_by_evidence_budget(blocks, char_budget)
                return self._fit_blocks_even(blocks, char_budget)
        elif len(assembled) > char_budget and self.config.evidence_budgeting:
            return self._fit_blocks_by_evidence_budget(blocks, char_budget)
        return assembled[:char_budget]

    # --- 读路径 ---
    def _structured_facts(self, user_id, scope):
        from .store.contracts import FactState

        valid = now() if scope.valid_at is None else scope.valid_at
        known = valid if scope.known_at is None else scope.known_at
        for fact in self._all_facts():
            if fact.user_id != user_id or not fact.is_published(known) or (
                    scope.known_at is not None and fact.created_at > known):
                continue
            if fact.valid_at is not None and fact.valid_at > valid:
                continue
            fact = fact.as_known_at(scope.known_at)
            historical = ((fact.invalid_at is not None and fact.invalid_at <= valid)
                          or (fact.expired_at is not None and fact.expired_at <= known))
            if (scope.state == FactState.CURRENT and historical
                    or scope.state == FactState.HISTORY and not historical):
                continue
            if (scope.exclude_sensitive and fact.sensitive) or self._matches(fact, self.focus.get("mute", [])):
                continue
            if scope.subject is not None and fact.subject.lower() != scope.subject.lower():
                continue
            if scope.predicate is not None and fact.predicate.lower() != scope.predicate.lower():
                continue
            if scope.categories and not set(scope.categories).intersection(fact.categories):
                continue
            if scope.source_ids and not set(scope.source_ids).intersection(fact.provenance):
                continue
            yield fact

    def aggregate_facts(self, predicate, *, operation='count', subject=None, start=None, end=None,
                        query=None, facts=None, user_id='default'):
        from dataclasses import replace
        from .retrieve.structured import summarize_matches, validate_aggregate
        from .store.contracts import FactQuery

        scope = FactQuery(limit=10) if facts is None else facts
        if not isinstance(scope, FactQuery):
            raise ValueError('Expected structured fact conditions')
        scope = replace(scope, predicate=predicate, subject=subject if subject is not None else scope.subject)
        validate_aggregate(operation, scope, start, end, query)
        rows = (fact for fact in self._structured_facts(user_id, scope)
                if (start is None or (fact.valid_at is not None and fact.valid_at >= start))
                and (end is None or (fact.valid_at is not None and fact.valid_at < end)))
        result = summarize_matches(rows, operation, scope.limit)
        if not result.count and query is not None and not scope.exclude_sensitive:
            candidates = tuple(self._event_candidates(
                query, user_id, scope.valid_at, scope.limit, max(scope.limit * 5, 25),
                known_at=scope.known_at, start=start, end=end)[:scope.limit])
            return replace(result, candidates=candidates, limitations=('source_candidates_only',))
        return result

    def follow_path(self, anchor, predicates, *, facts=None, user_id='default'):
        from .retrieve.structured import PathMatch, PathResult, traverse, validate_path
        from .store.contracts import FactQuery

        scope = FactQuery(limit=20) if facts is None else facts
        predicates = validate_path(anchor, predicates, scope)
        anchor = anchor.strip().lower()
        visible = {fact.id: fact for fact in self._structured_facts(user_id, scope)}

        def neighbors(key, predicate):
            for edge in self.graph.neighbors(key, direction='out'):
                if edge.predicate.lower() == predicate and edge.fact_id in visible:
                    yield edge.object_id, visible[edge.fact_id]

        entity = self.graph.get_entity(user_id, anchor)
        if entity is None:
            return PathResult(())
        paths, flags = traverse(entity.id, predicates, neighbors, scope.limit)
        return PathResult(tuple(PathMatch(self.graph.entities[key].name, row) for key, row in paths), flags)

    def search(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        *, facts=None,
    ) -> SearchResult:
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        user = user_id

        from .store.contracts import FactQuery

        scope = facts if facts is not None else FactQuery(valid_at=as_of, known_at=as_of)
        if not isinstance(scope, FactQuery) or scope.after_id is not None or (facts is not None and as_of is not None):
            raise ValueError('Expected structured fact conditions or as_of')
        if facts is not None or self.focus.get("mute"):
            candidates = self._structured_facts(user, scope)
            ranked, diag = self.retriever.retrieve(query, user, scope.valid_at, top_k, candidates=candidates)
        else:
            ranked, diag = self.retriever.retrieve(query, user, as_of, top_k)
        via = "hybrid"
        if not ranked and facts is None and not self.focus.get("mute"):
            ranked, diag = self._retrieve_cold(query, user, as_of, top_k)
            if ranked:
                via = "cold"
        matched = [fact for fact, _ in ranked]
        if not matched or self._should_abstain(query, matched, diag):
            if via != "cold" and facts is None and not self.focus.get("mute"):
                cold, cold_diag = self._retrieve_cold(query, user, as_of, top_k)
                cold_facts = [fact for fact, _ in cold]
                if cold_facts and not self._should_abstain(query, cold_facts, cold_diag):
                    return SearchResult(query, cold_facts, [score for _, score in cold], via="cold")
            fallback = self._digest_fallback(query, user, as_of=as_of) if facts is None else None
            return fallback or SearchResult(query, via="abstain", abstained=True)

        scores = [score for _, score in ranked]
        if via != "cold" and scope.is_current_view:
            self._reinforce_fact(matched[0])
        return SearchResult(query=query, facts=matched, scores=scores, via=via)

    def as_of(self, query: str, when: float, user_id: str = "default", top_k: Optional[int] = None) -> SearchResult:
        """回答"在 `when` 时刻我们认为什么是真的？"——双时间轴时间点查询。"""
        return self.search(query, user_id=user_id, as_of=when, top_k=top_k)

    def get_episode(self, episode_id: str, user_id: str = "default") -> dict | None:
        """Explicit original lookup; no embeddings or internal processing state."""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        ep = self.episodes_doc.get(episode_id)
        if ep is None or ep.user_id != user_id:
            return None
        return dict(id=ep.id, space=ep.user_id, session_id=ep.session_id, role=ep.role,
                    event_time=ep.event_time, ingested_at=ep.ingested_at, content=ep.content,
                    message_id=ep.message_id, sequence=ep.sequence, mode=ep.mode, coverage=deepcopy(ep.coverage))

    def retrieve_episodes(
        self,
        query: str,
        user_id: str = "default",
        k: int = 5,
        pool: Optional[int] = None,
        as_of: Optional[float] = None,
        *, known_at: float | None = None,
    ):
        """为查询检索 top-k 条原始 episode（session）：bi-encoder 候选池 → BM25 词法重排（RRF）
        → 可选 cross-encoder 重排。

        BM25 层：当 pool >= episode 总数时（LongMemEval_S：约 54 个 session，pool 至多 100），
        所有 episode 都在候选里，仅凭嵌入排序会漏掉精确词匹配（名字、地点、日期）。BM25 的 RRF
        融合把这些提上来而不取代语义信号。改善偏好类和精确实体类问题——原始文本词项优于嵌入
        相似度的场景。
        """
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        pool = pool or max(k * 5, 25)
        eps = self._event_candidates(query, user_id, as_of, k, pool, known_at=known_at)

        if self.reranker is not None and len(eps) > k:
            ranked = self.reranker.rerank(query, [(i, ep.text) for i, ep in enumerate(eps)], k)
            return [eps[i] for i, _ in ranked]
        return eps[:k]

    def context_for(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        k_chunks: int = 3,
        agentic: bool = False,
        timeline: bool = False,
        hyde: bool = False,
        graph: bool = False,
        wiki: bool = False,
        summary: bool = False,
        verify: bool = False,
        intent: bool = False,
    ) -> str:
        """组装供 LLM 作答的 hybrid 读上下文（AGENTS.md 「架构概览与代码导航」）：live 且带日期戳的 fact（经冲突
        解决的当前状态）+ top-k 原始 session chunk（细节抽取会丢掉的部分）。每一行都带日期戳，
        正是时间类与知识更新类问题可被回答的关键。

        agentic=True 把单次 chunk 检索换成 LLM 分解的迭代检索（Bet B）。"""
        user = user_id

        # HyDE：用 LLM 写的假想答案扩展查询，以提升检索召回（M2c）。
        search_query = query
        if hyde and self.llm is not None:
            hypo = self.llm.complete(
                f"Write a brief, plausible hypothetical answer (1-2 sentences) to this question, to aid "
                f"retrieval:\n{query}",
                system="You write a short plausible answer. Be concise; no preamble.",
            )
            if hypo.strip():
                search_query = f"{query}\n{hypo.strip()}"

        ranked, _ = self.retriever.retrieve(search_query, user, as_of, top_k)
        # 最新在前：对知识更新类问题，LLM 应在顶部看到最新 fact（如新工作、新城市）——并信任
        # 它甚于列表下方更旧的 fact。
        ranked_by_date = sorted(ranked, key=lambda x: time_key(x[0].valid_at), reverse=True)
        ranked_facts = [f for f, _ in ranked_by_date]
        fact_lines = [f"- [{fmt_date(f.valid_at)}] {f.text}" for f in ranked_facts]
        facts_block = "\n".join(fact_lines) or "(none)"

        chunk_block = ""
        episodes: list[EventEvidence] = []
        if k_chunks:
            if agentic and self.llm is not None:
                from .retrieve.agentic import AgenticRetriever

                episodes = AgenticRetriever(self, self.llm).gather_episodes(query, user, k_chunks, as_of=as_of)
            else:
                episodes = self.retrieve_episodes(search_query, user, k_chunks, as_of=as_of)
            if self.config.provenance_chunk_promotion:
                promoted = self._provenance_detail_chunks(ranked_facts, search_query, user, as_of, k_chunks)
                if promoted:
                    episodes = self._merge_promoted_chunks(promoted, episodes, k_chunks)
            parts = []
            for ep in episodes:
                date = ep.metadata.get("date") or fmt_date(ep.event_time)
                parts.append(f"[{date}] (episode: {ep.id}; space: {ep.user_id}; {ep.representation}; partial={ep.partial}; role={ep.role})\n{ep.text}")
            chunk_block = "\n\n".join(parts)

        result = (
            f"FACTS (current, with dates):\n{facts_block}\n\n"
            f"RELEVANT CONVERSATIONS (with dates):\n{chunk_block}"
        ).strip()
        if self.config.chain_evidence:
            evo = self._fact_evolution_block(ranked_facts, user, as_of, query=search_query)
            if evo:
                result += f"\n\n{evo}"
        if self.config.provenance_evidence:
            raw_prov = self._provenance_raw_block(
                ranked_facts,
                search_query,
                user,
                as_of,
                exclude_episode_ids={ep.id for ep in episodes},
            )
            if raw_prov:
                result += f"\n\n{raw_prov}"
        if timeline:
            # 相关 fact 的显式时间顺序——有助于 "first/after/how long"（M2b）
            ordered = sorted((f for f, _ in ranked), key=lambda f: time_key(f.valid_at))
            tl = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in ordered) or "(none)"
            result = f"TIMELINE (oldest to newest):\n{tl}\n\n" + result
        if graph:
            # L2：从查询的锚实体出发遍历实体图，跨 session 拉取相连的 fact
            # （multi-hop / multi-session）。
            related = self._graph_related_facts(search_query, user, as_of)
            if related:
                block = "\n".join(f"- [{fmt_date(f.valid_at)}] {f.text}" for f in related)
                result += f"\n\nRELATED FACTS (graph traversal):\n{block}"
        if wiki:
            # L4：LLM 精编的逐实体笔记（区分当前与过去），查询时合成。
            notes = self._entity_notes(search_query, user, as_of)
            if notes:
                result = "ENTITY NOTES:\n" + "\n".join(f"- {n}" for n in notes) + "\n\n" + result
        if verify and self.llm is not None:
            # 自校验：起草答案，找出最有用的单个缺口，检索之，追加证据。
            extra = self._self_verify(query, result, user, as_of)
            if extra:
                result += f"\n\nADDITIONAL EVIDENCE (self-verify):\n{extra}"
        if summary and self.llm is not None:
            # L5：把相关材料合成为简短忠实的摘要，置于最前。
            syn = self.llm.complete(
                f"Synthesize, in 2-3 faithful sentences, the facts relevant to: {query}\n\n{result}",
                system="You write a concise, strictly faithful synthesis of the given context.",
            )
            if syn.strip():
                result = f"DIGEST:\n{syn.strip()}\n\n" + result
        if intent and self.llm is not None:
            # L6：前瞻性意图提示。如实说明：不预期对 QA benchmark 有帮助；为完整性和消融实验
            # 而保留。
            hint = self.llm.complete(
                f"In one short phrase, what is the user likely really trying to find out with: {query}",
                system="Reply with a short phrase only.",
            )
            if hint.strip():
                result = f"LIKELY INTENT: {hint.strip()}\n\n" + result
        return result

    def _self_verify(self, query: str, context: str, user: str, as_of: Optional[float]) -> str:
        draft = self.llm.complete(
            f"Using only this context, answer concisely. If something is missing, say what.\n\n{context}\n\nQ: {query}",
            system="Answer from context; note any missing piece.",
        )
        gap = self.llm.complete(
            f"Question: {query}\nDraft answer: {draft}\nWhat ONE short search query would best fill a gap or "
            f"verify this? Reply with the query, or 'none'.",
            system="Reply with one short search query, or exactly 'none'.",
        )
        g = gap.strip().strip(".").lower()
        if not g or g == "none":
            return ""
        more = self.retrieve_episodes(gap.strip(), user, 2, as_of=as_of)
        return "\n\n".join(f"[{ep.metadata.get('date', '?')}] (episode: {ep.id}; space: {ep.user_id}; {ep.representation}; partial={ep.partial}; role={ep.role})\n{ep.text}" for ep in more)

    def _graph_related_facts(self, query: str, user: str, as_of: Optional[float], limit: int = 8) -> list[Fact]:
        if not self.config.graph_proximity:
            return []
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live(as_of)]
        if not live:
            return []
        graph_scores, _ = self.retriever._graph_scores(query, user, live, as_of)
        related = [f for f in live if graph_scores.get(f.id, 0.0) > 0.0]
        related.sort(key=lambda f: (-graph_scores.get(f.id, 0.0), -time_key(f.valid_at), f.text.lower()))
        return related[:limit]

    def _entity_notes(self, query: str, user: str, as_of: Optional[float], max_entities: int = 3) -> list[str]:
        if self.llm is None:
            return []
        notes: list[str] = []
        for eid in list(self.retriever.query_entity_ids(query, user))[:max_entities]:
            ent = self.graph.entities.get(eid)
            if ent is None:
                continue
            facts = [
                f for f in self._all_facts()
                if f.user_id == user and f.subject.lower() == ent.name.lower()
            ]
            if not facts:
                continue
            lines = "\n".join(
                f"[{fmt_date(f.valid_at)}] {f.text}" + ("" if f.is_live(as_of) else " (past)")
                for f in sorted(facts, key=lambda x: time_key(x.valid_at))
            )
            note = self.llm.complete(
                f"Summarize what is known about {ent.name} in 2-3 sentences. Note current vs outdated "
                f"facts.\n{lines}",
                system="You write a concise, accurate entity note that resolves current vs past facts.",
            )
            if note.strip():
                notes.append(f"{ent.name}: {note.strip()}")
        return notes

    def history(self, subject: str, predicate: str, user_id: str = "default") -> list[Fact]:
        user = user_id
        return history(self._all_facts(), user, subject, predicate)

    def profile(self, user_id: str = "default") -> dict[str, str]:
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        subject = "user"
        live = [f for f in self.fact_store.values() if f.user_id == user_id and f.is_live()]
        return self.profiles.build(subject, live)

    # --- 内部实现 ---
    def _should_abstain(self, query: str, facts: list[Fact], diag: dict) -> bool:
        """Require a complete attribute match, object evidence or sufficient semantic similarity."""

        return should_abstain(query, facts, diag.get("sem", {}).values(), self.config.abstain_threshold)

    def _digest_fallback(
        self,
        query: str,
        user_id: str,
        as_of: Optional[float] = None,
    ) -> Optional[SearchResult]:
        """Surface a source-backed digest covering all informative query terms."""
        if not self.config.digest_fallback:
            return None
        candidates: list[tuple[int, int, float, EventEvidence, str]] = []
        k = max(1, int(getattr(self.config, "digest_fallback_k", 6) or 6))
        for ep in self.retrieve_digests(query, user_id, k=k, as_of=as_of):
            text = ep.text.strip()
            if not text:
                continue
            exact = digest_match(query, text)
            if exact:
                candidates.append((exact, len(overlap_terms(query, text)), time_key(ep.event_time), ep, text))
        if candidates:
            _, _, _, ep, text = max(candidates, key=lambda row: (row[0], row[1], row[2]))
            dated = f"[{ep.metadata.get('date') or fmt_date(ep.event_time)}] (episode: {ep.id}; space: {ep.user_id}; session: {ep.session_id}) {text}"
            return SearchResult(query=query, via="digest", _answer=dated)
        return None
