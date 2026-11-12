"""Offline algorithm engine for ingestion, consolidation, and retrieval."""
from __future__ import annotations

from copy import copy, deepcopy
import time
from typing import Callable, Optional

from .config import Config
from .focus import matches, selectors
from .consolidate import reinforce
from .consolidate.facts import FactWriter
from .consolidate.manual import build_manual_fact, reconcile_manual
from .consolidate.policy import POLICY_DEFAULTS
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
from .categories import Category
from .extraction import require_llm
from .types import Conflict, Episode, Fact, Retention
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
        self.fact_store = vector_store_factory()  # 热层事实的向量存储，用于主要检索
        self.cold_store = vector_store_factory()  # 冷层事实的向量存储，保留从热层换出的事实
        self.digest_vec = vector_store_factory()  # 会话摘要的向量索引，用于检索相关摘要
        # 初始化图存储
        try:
            self.graph = graph_store_factory()
        except BaseException:
            self.close()
            raise
        # 查询工作集：记录 lean_context 最近一次执行到事实组装时选出的事实，供检查。
        # 后续 lean_context 执行到此阶段时会覆盖它；该列表不写入快照。
        self.working_set: list[Fact] = []
        # 条目可随快照保存并恢复；短期指其生命周期，不会因此成为长期事实。
        # 关注主题：track 提升相关事实的显著度；mute 在上下文和画像中抑制相关事实。
        self.focus: dict[str, list[str]] = {"track": [], "mute": []}
        # 记忆策略：抽取指令及抽取、摘要、画像提示词；空提示词使用内置默认值。
        self.policy: dict[str, str] = {"extract_instruction": "", "extract_system": "",
                                       "digest_system": "", "profile_system": ""}
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

    def _sync_episode(self, ep: Episode) -> None:
        if not ep.digest:
            ep.digest_written_at = None
            ep.digest_embedding = None
        if self.embedder is not None and ep.digest and ep.digest_embedding is None:
            raise ValueError("A searchable digest requires its embedding")
        self.episodes_doc.put(ep.id, ep)
        if self.embedder is None:
            return
        if ep.digest:
            self.digest_vec.upsert(ep.id, ep.digest_embedding, ep)
        else:
            self.digest_vec.delete(ep.id)

    def _all_facts(self, *, include_unpublished=False) -> list[Fact]:
        facts = self.fact_store.values() + self.cold_store.values()
        return facts if include_unpublished else [fact for fact in facts if fact.is_published()]

    def _upsert_fact(self, fact: Fact, *, tier: str = "existing") -> None:
        """把被修改的 fact 写回所属层，或强制提升到热层。

        内存 store 持有对象引用，但真实的向量后端在 upsert/get 时常会拷贝 payload。因此仅修改
        返回的 Fact 不够；每次语义编辑/失效化都必须显式把 payload 写回唯一一个层。
        """
        with self._pipeline_lock:
            from ._profile import sync
            sync(self, {fact.id: fact})
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
        self.ingestor = Ingestor(self.episodes_doc)
        self.writer = FactWriter(self.fact_store, self.graph, self.embedder, self.config)
        self.retriever = HybridRetriever(self.fact_store, self.graph, self.embedder, self.config)
        self.cold_retriever = HybridRetriever(self.cold_store, self.graph, self.embedder, self.config)
        self.retriever.track = self.cold_retriever.track = tuple(self.focus["track"])
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
        digested = [ep for ep in episodes if ep.digest]
        for ep in episodes:
            if not ep.digest and ep.digest_embedding is not None:
                self._sync_episode(ep)
        _clear(self.digest_vec)
        vecs = (self.embedder.embed_batch([ep.digest for ep in digested])
                if digested else [])
        for ep, v in zip(digested, vecs):
            ep.digest_embedding = v
        for ep in digested:
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
    ) -> Episode:
        """写入一条带时间戳的 episode。

        Args:
            content: 事件内容。
            user_id: 用户 ID。
            session_id: 会话 ID。
            role: 说话者。
            event_time: 事件时间戳。
            consolidate: 是否立即执行整合。
        Returns:
            Episode: 写入的 episode 对象。
        """
        if self._pipeline_broken or self._pipeline_closed:
            raise RuntimeError('Memory checkpoint failed or closed; create a new instance')
        if consolidate:
            require_llm(self.llm)
        from ._pipeline import memory_adapter
        with memory_adapter(self, user_id).write_lock(user_id):
            ep = self.ingestor.ingest(content, user_id, session_id, role, event_time)
        if consolidate:
            self.consolidate()
        return ep

    def remember(self, content: str, user_id: str = "default", session_id: str = "default",
                 extract: bool = True) -> dict:
        """Save raw input and its requested lifecycle policy.

        Requested extraction remains pending until consolidate() validates model output.
        Reusable facts retain their source attribution and time qualifiers.
        """
        if self._pipeline_broken or self._pipeline_closed:
            raise RuntimeError('Memory checkpoint failed or closed; create a new instance')
        if type(extract) is not bool:
            raise ValueError("extract must be a boolean")
        from ._pipeline import memory_adapter
        with memory_adapter(self, user_id).write_lock(user_id):
            ep = self.add(content, user_id=user_id, session_id=session_id)
            ep.extract = extract
            self._sync_episode(ep)
            return {"extract": extract, "episode_id": ep.id}

    def remember_messages(self, messages: list[dict], session_id: str, user_id: str = 'default',
                          extract: bool = True) -> dict:
        """Offline-capable dialogue source/task recording; service owns background execution.

        Product callers use Memory for durable processing.
        """
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        from .consolidate.pipeline import MessageConflictError, PipelineTasks, normalized
        rows = normalized(messages, session_id, extract)
        with self._pipeline_lock:
            if self._pipeline_closed:
                raise RuntimeError('Memory is closed')
            tasks = PipelineTasks(self, user_id)
            try:
                ids, fresh = tasks.accept(rows, session_id, extract, now())
            except MessageConflictError:
                raise
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
                self._sync_episode(ep)
            if ep.extract and not ep.consolidated and ep.content.strip():
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
        """Extract facts and generate eligible digests from the selected sources.

        Both inputs default to the sources pending when this call starts.
        """
        pending = self.ingestor.pending() if fact_episodes is None or digest_episodes is None else []
        stats = self.consolidate(pending if fact_episodes is None else fact_episodes)
        stats["digests"] = self.generate_digests(pending if digest_episodes is None else digest_episodes)
        stats["reflected"] = 0
        return stats

    # --- 对话任务等待与结构化快照恢复 ---
    def flush_messages(self, user_id: str = 'default', session_id: Optional[str] = None,
                       timeout: float = 30, retry_failed: bool = False) -> dict:
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        if self._pipeline_closed:
            raise RuntimeError('Memory is closed; create a new instance')
        from ._pipeline import flush_memory
        started = time.monotonic()
        result = flush_memory(self, user_id, session_id, timeout, retry_failed)
        if session_id is None:
            from ._profile import flush
            operation = flush(self, user_id, started + timeout, retry_failed)
            if operation is not None:
                state = operation.state.value
                result["jobs"].append(dict(operation_id=operation.operation_id, operation=operation.kind.value,
                                           state=state, phase=operation.phase.value, attempts=operation.attempts,
                                           error_code=operation.error_code, next_attempt_at=operation.next_attempt_at))
                result["counts"][state] += 1
                if state not in {"completed", "failed"}:
                    result.update(ok=False, status="timed_out")
                elif state == "failed" and result["status"] != "timed_out":
                    result.update(ok=False, status="failed")
        return result


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
        with self._pipeline_lock:
            return self._restore_snapshot(snapshot, user_id, dedupe)

    def _restore_snapshot(self, snapshot, user, dedupe):
        # --- episodes：恢复无损带日期日志（仅 include_sensitive 导出中存在） ---
        from .consolidate.pipeline import content_hash

        def restored_id(kind, source_id):
            return kind + '_import_' + content_hash([user, source_id])[:32]

        episode_ids = {}
        eps_skipped = 0
        prepared_episodes: list[Episode] = []
        for row in snapshot["episodes"]:
            content = row["content"]
            session_id = row["session_id"]
            target_id = restored_id('ep', row['id'])
            if dedupe and self.episodes_doc.get(target_id) is not None:
                episode_ids[row['id']] = target_id
                eps_skipped += 1
                continue
            ep = Episode(content=content, user_id=user, session_id=session_id,
                         role=row["role"], event_time=row["event_time"],
                         consolidated=True,  # 它的 fact 在下方恢复；绝不重新抽取
                         digest=row.get("digest", ""), coverage=row.get('coverage'), digest_coverage=row.get('digest_coverage'),
                         digest_written_at=row.get('digest_written_at'),
                         metadata={'snapshot_source_id': row['id']})
            if "ingested_at" in row:
                ep.ingested_at = row["ingested_at"]
            if dedupe:
                ep.id = target_id
            episode_ids[row['id']] = ep.id
            if ep.digest:
                ep.digest_embedding = self._encode(ep.digest)
            prepared_episodes.append(ep)

        # --- facts：同时恢复 live 与被取代的行，保留 valid/transaction 时间 ---
        id_map: dict[str, str] = {}  # 旧快照 id -> 新 id，用于重连取代链
        staged: list[tuple[Fact, list[str]]] = []
        facts_skipped = 0
        for row in snapshot["facts"]:
            s, p, o = row["subject"], row["predicate"], row["object"]
            valid_at = row["valid_at"]
            target_id = restored_id('ft', row['id'])
            if dedupe and (self.fact_store.get(target_id) is not None or self.cold_store.get(target_id) is not None):
                id_map[row['id']] = target_id
                facts_skipped += 1
                continue
            f = Fact(subject=s, predicate=p, object=o, user_id=user,
                     text=row["text"], source=row["source"],
                     source_roles=row.get("source_roles", []),
                     sensitive=row["sensitive"],
                     sensitive_override=row.get("sensitive_override", False),
                     review_pending=row.get("review_pending", False), withdrawn_at=row["withdrawn_at"],
                     categories=row["categories"], categories_source=row["categories_source"], relation_id=row["relation_id"],
                     subject_id=restored_id('en', row["subject_id"]) if row["subject_id"] else None,
                     object_id=restored_id('en', row["object_id"]) if row["object_id"] else None,
                     subject_type=row["subject_type"], object_type=row["object_type"], retention=row["retention"],
                     salience=row["salience"], confidence=row["confidence"],
                     valid_at=valid_at,
                     invalid_at=row["invalid_at"], created_at=row["created_at"],
                     expired_at=row["expired_at"],
                     provenance=[episode_ids.get(key, key) for key in row["provenance"]])
            if dedupe:
                f.id = target_id
            f.embedding = self._encode(f.text)
            id_map[row["id"]] = f.id
            staged.append((f, row["supersedes"]))
        known_pairs = {item.pair_key for item in self.conflicts.values() if item.user_id == user}
        restored_conflicts = []
        held = {f.id for f, _ in staged if f.review_pending}
        held.update(f.id for f in self._all_facts(include_unpublished=True) if f.user_id == user and f.review_pending)
        for row in snapshot['conflicts']:
            fields = {key: value for key, value in row.items() if key not in {'id', 'older', 'newer'}}
            conflict = Conflict(id_map[row['older']], id_map[row['newer']], user_id=user, **fields)
            if (conflict.older != conflict.newer and conflict.pair_key not in known_pairs
                    and (conflict.older in held or conflict.newer in held)):
                restored_conflicts.append(conflict)
                known_pairs.add(conflict.pair_key)
        # 校验和向量准备全部成功后才写入；取代链也链接到去重命中的已有事实。
        for ep in prepared_episodes:
            self._sync_episode(ep)
        for f, old_sup in staged:
            f.supersedes = [id_map[key] for key in old_sup if key in id_map]
            self._upsert_fact(f, tier="hot")
            # add_fact 会把 fact.invalid_at 带到关系上，因此被取代的历史落库时就是已失效化的边
            # ——图的时间线在往返中得以保留。
            self.writer.graph_builder.add_fact(f)

        self.conflicts.update((conflict.id, conflict) for conflict in restored_conflicts)
        self._enforce_hot_limit()
        return {"format": "mnemotrace", "facts_restored": len(staged), "facts_skipped": facts_skipped,
                "episodes_restored": len(prepared_episodes), "episodes_skipped": eps_skipped,
                "malformed": 0}

    # --- 离线工作集 ---
    def add_fact(self, subject: str, predicate: str, object: str, user_id: str = "default",
                 valid_at: Optional[float] = None, sensitive: Optional[bool] = None,
                 categories: list[Category] | None = None, retention: Retention = "unknown", *, text: str,
                 subject_id=None, object_id=None, subject_type=None, object_type=None) -> Fact:
        """Seed a synthetic fact for offline experiments."""
        with self._pipeline_lock:
            if self._pipeline_broken:
                raise RuntimeError('Memory checkpoint failed; create a new instance')
            user = user_id
            f = build_manual_fact(subject, predicate, object, text, user, valid_at, sensitive, categories, retention,
                                  subject_id=subject_id, object_id=object_id, subject_type=subject_type,
                                  object_type=object_type)
            f.embedding = self._encode(f.text)
            from .consolidate.pipeline import PipelineTasks
            PipelineTasks(self, f.user_id).invalidate()
            history = [x for x in self._all_facts() if x.slot == f.slot]
            action, changed = reconcile_manual(f, history, now(), explicit_valid_at=valid_at is not None)
            for old in changed:
                self._upsert_fact(old, tier="hot")
                self.writer.graph_builder.add_fact(old)
            if changed:
                self.graph.prune_orphan_entities()
            if action != "duplicate":
                self._upsert_fact(f, tier="hot")
                self.writer.graph_builder.add_fact(f)
            self._enforce_hot_limit()
            return changed[0] if action == "duplicate" else f

    def delete_fact(self, fact_id: str) -> bool:
        """被遗忘权：硬删除一条 fact（区别于保留历史的自动失效化）。这是用户主动发起的擦除，
        数据真的被移除——热层和冷层都删。"""
        with self._pipeline_lock:
            existed = self.fact_store.get(fact_id) is not None or self.cold_store.get(fact_id) is not None
            if existed and self._pipeline_state:
                from .consolidate.pipeline import PipelineTasks
                record = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
                PipelineTasks(self, record.user_id).invalidate()
            from ._profile import state, settings
            from .profile import synchronize
            record = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
            if record is not None:
                synchronize(state(self, record.user_id, create=True), {fact_id: None}, settings(self))
            self.graph.delete_relations_for_fact(fact_id)
            self.graph.prune_orphan_entities()
            self.fact_store.delete(fact_id)
            self.cold_store.delete(fact_id)
            return existed

    def set_fact_sensitive(self, fact_id: str, sensitive: bool) -> Fact | None:
        if type(sensitive) is not bool:
            raise ValueError('Expected a boolean sensitivity flag')
        with self._pipeline_lock:
            fact = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
            if fact is not None and (fact.sensitive, fact.sensitive_override) != (sensitive, True):
                from .consolidate.pipeline import PipelineTasks
                PipelineTasks(self, fact.user_id).invalidate()
                fact.sensitive, fact.sensitive_override = sensitive, True
                self._upsert_fact(fact)
            return fact

    def withdraw_fact(self, fact_id: str) -> bool:
        with self._pipeline_lock:
            fact = self.fact_store.get(fact_id) or self.cold_store.get(fact_id)
            if fact is None:
                return False
            if fact.withdrawn_at is None:
                from .consolidate.pipeline import PipelineTasks
                PipelineTasks(self, fact.user_id).invalidate()
                fact.withdrawn_at, fact.review_pending = now(), False
                self._upsert_fact(fact)
                self.graph.delete_relations_for_fact(fact.id)
                self.graph.prune_orphan_entities()
                for pair in list(self.conflicts.values()):
                    if fact.id in (pair.older, pair.newer):
                        del self.conflicts[pair.id]
            return True

    # --- 关注领域："关注点"定制（记忆应强调/抑制什么） ---
    def set_focus(self, track: Optional[list[str]] = None, mute: Optional[list[str]] = None) -> dict:
        """Update read-time tracking and muting selectors."""
        with self._pipeline_lock:
            updates = {key: [term.strip() for term in terms if term.strip()]
                       for key, terms in (("track", track), ("mute", mute)) if terms is not None}
            for terms in updates.values():
                selectors(terms)
            self.focus.update(updates)
            self.retriever.track = self.cold_retriever.track = tuple(self.focus["track"])
            from ._profile import sync_settings
            sync_settings(self)
            return self.get_focus()

    def get_focus(self) -> dict:
        return {key: list(self.focus[key]) for key in ("track", "mute")}

    @staticmethod
    def _matches(f: Fact, terms: list[str]) -> bool:
        return matches(f.text, terms, f.categories)

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
            if fact is None or not fact.is_published():
                continue
            live = fact.is_valid_at(at) and (as_of is None or fact.valid_at is not None)
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
        """更新策略字段（extract_instruction / extract_system / digest_system / profile_system）。
        空字符串清除覆盖项（回退到默认值）。后续流水线按新策略准备并校验输入。"""
        with self._pipeline_lock:
            for k, v in fields.items():
                if k in self.policy and v is not None:
                    self.policy[k] = v
            self._apply_policy()
            from ._profile import sync_settings
            sync_settings(self)
            return self.get_policy()

    def _effective(self, key: str) -> str:
        """返回实际生效的策略字段值（用户覆盖或默认值）。"""
        return self.policy.get(key) or POLICY_DEFAULTS[key]

    def _apply_policy(self):
        self.writer.config = self.config


    # --- 冲突检测 -> pending（LLM 检测出含糊的部分；由用户确认） ---
    def pending_conflicts(self, user_id: str = "default") -> list[Conflict]:
        """Return conflicts awaiting a decision in this workspace."""
        from .consolidate.adjudication import conflict_is_reviewable

        user = user_id
        out = []
        for c in self.conflicts.values():
            if c.user_id != user:
                continue
            a = self.fact_store.get(c.older) or self.cold_store.get(c.older)
            b = self.fact_store.get(c.newer) or self.cold_store.get(c.newer)
            if conflict_is_reviewable(a, b):
                out.append(c)
        return out

    def resolve_conflict(self, conflict_id: str, keep: str = "newer") -> bool:
        """Apply an explicit user decision to a pending conflict."""
        if keep not in {'older', 'newer', 'both', 'dismiss'}:
            raise ValueError('Invalid conflict resolution')
        c = self.conflicts.get(conflict_id)
        if c is None:
            return False
        from .consolidate.adjudication import conflict_is_reviewable, resolve_facts
        from .consolidate.pipeline import PipelineTasks

        facts = {key: self.fact_store.get(getattr(c, key)) or self.cold_store.get(getattr(c, key))
                 for key in ('older', 'newer')}
        if keep != 'dismiss' and not conflict_is_reviewable(facts['older'], facts['newer']):
            return False
        PipelineTasks(self, c.user_id).invalidate()
        held = {fact.id for fact in facts.values() if fact is not None and fact.review_pending}
        for fact in resolve_facts(facts, keep, now()):
            if fact.withdrawn_at is not None:
                for pair in list(self.conflicts.values()):
                    if pair.id != conflict_id and fact.id in (pair.older, pair.newer):
                        del self.conflicts[pair.id]
            elif fact.id in held and any(pair.id != conflict_id and fact.id in (pair.older, pair.newer)
                     for pair in self.conflicts.values()):
                fact.review_pending = True
            self._upsert_fact(fact)
            self.writer.graph_builder.add_fact(fact)
        self.graph.prune_orphan_entities()
        del self.conflicts[conflict_id]
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
    ) -> list[EventEvidence]:
        """Rank available source digests using lexical and semantic evidence."""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        return self._rank_digests(query, user_id, k, as_of)

    def _rank_digests(self, query, user_id, k, as_of):
        if not k:
            return []
        candidates = self._digest_candidates(query, user_id, as_of, max(k * 3, 30))
        if self.reranker is not None and len(candidates) > k:
            ranked = self.reranker.rerank(query, [(str(i), ep.text) for i, ep in enumerate(candidates)], k)
            return [candidates[int(i)] for i, _ in ranked]
        return candidates[:k]

    def _source_muted(self, episode):
        terms = self.focus["mute"]
        return matches(episode.content, terms) or matches(episode.digest, terms)

    def _digest_candidates(self, query, user_id, as_of, pool, *,
                          start=None, end=None):
        from .retrieve.fusion import weighted_rrf
        from .retrieve.hybrid import date_terms, order_positive

        def visible(ep):
            return (ep.user_id == user_id
                    and not self._source_muted(ep)
                    and (as_of is None or (ep.event_time is not None and ep.event_time <= as_of))
                    and (start is None or (ep.event_time is not None and ep.event_time >= start))
                    and (end is None or (ep.event_time is not None and ep.event_time < end)))

        episodes = {ep.id: ep for ep in self.episodes_doc.values()
                    if ep.digest and visible(ep)}
        lexical = bm25_scores(query, [(ep.id, f'{ep.digest} {date_terms(ep.event_time)}')
                                      for ep in episodes.values()])
        semantic = {}
        if self.embedder is not None:
            vector = self.embedder.embed(query)
            hits = self.digest_vec.search(vector, pool, where=visible)
            for score, ep in hits:
                if ep.id in episodes:
                    semantic[ep.id] = max(score, semantic.get(ep.id, 0.0))
        scores = weighted_rrf({'sem': order_positive(semantic)[:pool],
                               'lex': order_positive(lexical)[:pool]}, {})
        ranked = sorted(scores, key=lambda key: (-scores[key], key))
        return [event_evidence(episodes[key]) for key in ranked[:pool]]

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

    def profile(self, user_id="default"):
        from ._profile import state
        from .profile import read
        with self._pipeline_lock:
            result = read(state(self, user_id), now())
            operations = state(self, user_id).get("operations", {})
            if operations:
                operation = max(operations.values(), key=lambda op: op["sequence"])
                result.update(operation_id=operation["operation_id"], refresh_state=operation["state"],
                              refresh_error=operation["error_code"])
            return result

    def refresh_profile(self, user_id="default"):
        from ._profile import enqueue
        return enqueue(self, user_id)

    def operation(self, operation_id, user_id="default"):
        from ._profile import state, result
        operation = state(self, user_id).get("operations", {}).get(operation_id)
        return result(operation) if operation else None

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
        facts = [f for f in self._all_facts() if f.user_id == user and not self._matches(f, self.focus["mute"])]
        if redact_sensitive:
            facts = [f for f in facts if not getattr(f, "sensitive", False)]
        by_id = {f.id: f for f in facts}
        replaces: dict[str, list[Fact]] = {}
        for f in facts:
            if f.supersedes:
                for predecessor in f.supersedes:
                    replaces.setdefault(predecessor, []).append(f)

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
            while predecessor := next((key for key in cur.supersedes if key in by_id and key not in seen), None):
                cur = by_id[predecessor]
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
        replaced_ids = {key for f in self._all_facts() for key in f.supersedes}

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

    def lean_context(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        n_digests: int = 20,
        n_facts: int = 15,
        agentic: bool = False,
        timeline: bool = False,  # 追加按时间排序的事件时间线（有助于时序/时长问题）
        char_budget: int = 60_000,
        redact_sensitive: bool = False,  # 安全/共享视图：只显示非敏感的结构化 fact
        *, facts=None,
    ) -> str:
        """Build bounded context from ranked facts and source digests."""
        if self._pipeline_broken:
            raise RuntimeError('Memory checkpoint failed; create a new instance')
        user = user_id
        from .retrieve.context import pack_sections, render_sections

        groups = []
        visible = None
        if facts is not None:
            from dataclasses import replace
            from .store.contracts import FactQuery

            if not isinstance(facts, FactQuery) or facts.after_id is not None or as_of is not None:
                raise ValueError('Expected structured fact conditions or as_of')
            as_of = facts.valid_at
            redact_sensitive = redact_sensitive or facts.exclude_sensitive
            visible = tuple(self._structured_facts(user, replace(facts, exclude_sensitive=redact_sensitive)))
        restricted = facts is not None and bool(facts.subject or facts.predicate or facts.categories or facts.source_ids)
        if redact_sensitive or restricted:
            # Redacted or explicitly scoped reads exclude unconstrained source search.
            n_digests = 0

        queries = [query]
        if agentic and not redact_sensitive and self.llm is not None:
            from .retrieve.agentic import AgenticRetriever
            queries += AgenticRetriever(self, self.llm)._subqueries(query)

        # L1 fact：对每个（子）查询做 hybrid 检索后取并集；再从查询实体做 n-hop 图扩展。
        fact_map: dict[str, Fact] = {}
        fact_limit = n_facts if top_k is None else top_k
        if fact_limit:
            for q in queries:
                ranked = self.retriever.retrieve(q, user, as_of, fact_limit, candidates=visible)[0]
                if not ranked and visible is None:
                    ranked = self._retrieve_cold(q, user, as_of, fact_limit)[0]
                for f, _ in ranked:
                    fact_map.setdefault(f.id, f)
            if visible is None:
                for f in self._graph_related_facts(query, user, as_of, limit=fact_limit):
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
            fl = [f"- [{fmt_date(f.valid_at)}] {f.text}" for f in by_date]
            state = "current" if facts is None else facts.state.value
            groups.append(("facts", f"FACTS ({state}, dated):", fl))
            # TIMELINE：同样的 fact 按旧->新排列并显式留出间隔，使"最早/最近/间隔多久"能直接
            # 从顺序读出，日期算术为模型铺好而不是留给心算（时间类问题的主要失败模式）。
            if timeline:
                chrono = sorted(all_facts, key=lambda f: time_key(f.valid_at))
                tl = [f"- {fmt_date(f.valid_at)}: {f.text}" for f in chrono]
                groups.append(("timeline", "TIMELINE (oldest to newest — use for ordering and durations):", tl))
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
                    heading, _, body = evo.partition("\n")
                    groups.append(("evolution", heading, [body]))

        summ_ranked: list[EventEvidence] = []
        seen_s: set[str] = set()
        if n_digests:
            for q in queries:
                for e in self._rank_digests(q, user, n_digests, as_of):
                    if e.id not in seen_s:
                        seen_s.add(e.id)
                        summ_ranked.append(e)

        if summ_ranked:
            chrono = sorted(summ_ranked[:n_digests], key=lambda e: time_key(e.event_time))
            sm = list(
                f"- [{e.metadata.get('date') or fmt_date(e.event_time)}] (episode: {e.id}; space: {e.user_id}) {e.text}" for e in chrono
            )
            groups.append(("digests", "SESSION DIGESTS (relevant, chronological):", sm))

        sections, _ = pack_sections(groups, char_budget, user)
        return render_sections(sections)

    # --- 读路径 ---
    def _structured_facts(self, user_id, scope):
        from .store.contracts import FactState

        valid = now() if scope.valid_at is None else scope.valid_at
        for fact in self._all_facts(include_unpublished=True):
            if fact.user_id != user_id or fact.review_pending:
                continue
            if fact.valid_at is None and scope.valid_at is not None:
                continue
            if fact.valid_at is not None and fact.valid_at > valid:
                continue
            historical = (fact.withdrawn_at is not None
                          or fact.invalid_at is not None and fact.invalid_at <= valid)
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
            candidates = tuple(self._digest_candidates(
                query, user_id, scope.valid_at, max(scope.limit * 5, 25),
                start=start, end=end)[:scope.limit])
            if candidates:
                return replace(result, candidates=candidates, limitations=('digest_candidates_only',))
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

        scope = facts if facts is not None else FactQuery(valid_at=as_of)
        if not isinstance(scope, FactQuery) or scope.after_id is not None or (facts is not None and as_of is not None):
            raise ValueError('Expected structured fact conditions or as_of')
        def source_references(rows):
            if scope.exclude_sensitive:
                return ()
            ids = dict.fromkeys(key for fact in rows for key in fact.provenance
                                if not scope.source_ids or key in scope.source_ids)
            return tuple(ep.id for key in ids if (ep := self.episodes_doc.get(key)) is not None
                         and ep.user_id == user and not self._source_muted(ep)
                         and (scope.valid_at is None or ep.event_time is not None and ep.event_time <= scope.valid_at)
)

        if scope.subject is not None and scope.predicate is not None:
            from .retrieve.answers import slot_result
            result = slot_result(query, list(self._structured_facts(user, scope)))
            result.facts = result.facts[:top_k or self.config.top_k]
            result.source_ids = source_references(result.facts)
            return result
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
                    return SearchResult(query, cold_facts, [score for _, score in cold], via="cold",
                                        source_ids=source_references(cold_facts))
            if not (scope.exclude_sensitive or scope.subject or scope.predicate or scope.categories or scope.source_ids):
                fallback = self._digest_fallback(query, user, as_of=scope.valid_at)
                if fallback:
                    return fallback
            return SearchResult(query, via="abstain", abstained=True)

        scores = [score for _, score in ranked]
        if via != "cold" and scope.is_current_view:
            self._reinforce_fact(matched[0])
        return SearchResult(query=query, facts=matched, scores=scores, via=via, source_ids=source_references(matched))

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
                    message_id=ep.message_id, sequence=ep.sequence, coverage=deepcopy(ep.coverage))

    def context_for(
        self,
        query: str,
        user_id: str = "default",
        as_of: Optional[float] = None,
        top_k: Optional[int] = None,
        k_digests: int = 3,
        agentic: bool = False,
        timeline: bool = False,
        hyde: bool = False,
        graph: bool = False,
        wiki: bool = False,
        summary: bool = False,
        verify: bool = False,
        intent: bool = False,
    ) -> str:
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

        candidates = None
        if self.focus["mute"]:
            from .store.contracts import FactQuery
            candidates = self._structured_facts(user, FactQuery(valid_at=as_of))
        ranked, _ = self.retriever.retrieve(search_query, user, as_of, top_k, candidates=candidates)
        # 最新在前：对知识更新类问题，LLM 应在顶部看到最新 fact（如新工作、新城市）——并信任
        # 它甚于列表下方更旧的 fact。
        ranked_by_date = sorted(ranked, key=lambda x: time_key(x[0].valid_at), reverse=True)
        ranked_facts = [f for f, _ in ranked_by_date]
        fact_lines = [f"- [{fmt_date(f.valid_at)}] {f.text}" for f in ranked_facts]
        facts_block = "\n".join(fact_lines) or "(none)"

        chunk_block = ""
        episodes: list[EventEvidence] = []
        if k_digests:
            if agentic and self.llm is not None:
                from .retrieve.agentic import AgenticRetriever

                episodes = AgenticRetriever(self, self.llm).gather_digests(query, user, k_digests, as_of=as_of)
            else:
                episodes = self.retrieve_digests(search_query, user, k_digests, as_of=as_of)
            parts = []
            for ep in episodes:
                date = ep.metadata.get("date") or fmt_date(ep.event_time)
                parts.append(f"[{date}] (episode: {ep.id}; space: {ep.user_id}; {ep.representation}; partial={ep.partial}; role={ep.role})\n{ep.text}")
            chunk_block = "\n\n".join(parts)

        result = (
            f"FACTS (current, with dates):\n{facts_block}\n\n"
            f"SOURCE DIGESTS (with dates):\n{chunk_block}"
        ).strip()
        if self.config.chain_evidence:
            evo = self._fact_evolution_block(ranked_facts, user, as_of, query=search_query)
            if evo:
                result += f"\n\n{evo}"
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
        more = self.retrieve_digests(gap.strip(), user, 2, as_of=as_of)
        return "\n\n".join(f"[{ep.metadata.get('date', '?')}] (episode: {ep.id}; space: {ep.user_id}; {ep.representation}; partial={ep.partial}; role={ep.role})\n{ep.text}" for ep in more)

    def _graph_related_facts(self, query: str, user: str, as_of: Optional[float], limit: int = 8) -> list[Fact]:
        if not self.config.graph_proximity:
            return []
        live = [f for f in self._all_facts() if f.user_id == user and f.is_live(as_of)
                and not self._matches(f, self.focus["mute"])]
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
                and not self._matches(f, self.focus["mute"])
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
        return history((fact for fact in self._all_facts(include_unpublished=True) if not fact.review_pending),
                       user, subject, predicate)

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
        for ep in self._rank_digests(query, user_id, k, as_of):
            text = ep.text.strip()
            if not text:
                continue
            exact = digest_match(query, text)
            if exact:
                candidates.append((exact, len(overlap_terms(query, text)), time_key(ep.event_time), ep, text))
        if candidates:
            _, _, _, ep, text = max(candidates, key=lambda row: (row[0], row[1], row[2]))
            return SearchResult(query=query, via="digest", sources=(ep,))
        return None
