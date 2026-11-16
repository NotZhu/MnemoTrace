"""Explicit Python entry point for record-backed memory in one PostgreSQL space."""
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

from ._postgres_pipeline import PostgresPipeline
from .config import Config
from .consolidate.decay import reinforce
from .retrieve import postgres as retrieval
from .store.contracts import (
    ChangeSet, CommitConditions, FactQuery, message_inputs,
    RecordRef, RecordWrite, StoredRecord, validate_erasures,
)
from .store.postgres.dao import PostgresDAO, new_operation_id
from .store.postgres.database import identifier
from .store.postgres.errors import VersionConflict
from .store.postgres.identity import Spaces
from .store.postgres.records import FACT, SOURCE, MODELS, domain
from .util import time_key

if TYPE_CHECKING:
    from .embed import Embedder
    from .store.contracts import SpaceContext
    from .store.postgres.database import Database


class Memory:
    """记忆存储。用于存储和检索记录。"""

    def __init__(self, database: 'Database', context: 'SpaceContext', *,
                 embedder: 'Embedder | None' = None, embedding_model: str | None = None,
                 llm=None, config: Config | None = None):
        """
        初始化记忆存储。

        Args:
            database: 数据库连接。
            context: 空间上下文。
            embedder: 嵌入器，用于将文本转换为向量。
            embedding_model: 嵌入模型的标识符。
            llm: 语言模型，用于生成回复。
            config: 配置参数。
        """
        if (embedder is None) != (embedding_model is None):
            raise ValueError('Pair an embedder with its stable embedding_model identifier')
        self.dao = PostgresDAO(database, context)
        self._embedder = embedder
        self._model = embedding_model
        if self._model is not None:
            identifier(self._model)
        self._database = database
        self._config = config if config is not None else Config()
        self._processing = PostgresPipeline(self.dao, self._config, llm, self._embedder, self._model)

    @property
    def context(self):
        return self.dao.context

    def _ref(self, kind, record_id):
        return RecordRef(self.context, kind, record_id)

    def _write(self, value, old=None, *, text_changed=False):
        return RecordWrite(StoredRecord.from_record(
            self.context, value,
            content_version=(old.content_version + int(text_changed)) if old is not None else 1,
            record_version=old.record_version + 1 if old is not None else 1,
        ), old.record_version if old is not None else None)

    def _commit(self, snapshot, *, writes=(), **kwargs):
        return self.dao.commit(ChangeSet(CommitConditions(snapshot), new_operation_id(), writes=writes, **kwargs))

    def remember(self, value, session_id: str, *, wait=False, timeout=30,
                 message_id=None, event_time=None, agent_id=None):
        """Store a request and optionally wait for the worker to publish it."""
        from math import isfinite

        identifier(session_id)
        if type(wait) is not bool or type(timeout) not in (int, float) or not isfinite(timeout) or not 0 <= timeout <= 300:
            raise ValueError('Expected a boolean wait and a timeout between 0 and 300 seconds')
        items = message_inputs(value, message_id=message_id, event_time=event_time, agent_id=agent_id)
        pipeline = self._processing
        source_ids, operation_id = pipeline.tasks.accept(session_id, items, pipeline.require_ready)
        positions = pipeline.tasks.source_positions(source_ids)
        if len(positions) != len(source_ids):
            raise VersionConflict('Task sources were removed after acceptance')
        task = pipeline.wait(operation_id, timeout) if wait else pipeline.tasks.operation(operation_id)
        state = task.state.value if task is not None else 'cancelled'
        status = ('completed' if state == 'completed' else 'failed' if state in ('failed', 'cancelled')
                  else 'timed_out' if wait else 'accepted')
        result = dict(ok=status != 'failed', status=status, stored_raw=True, source_ids=source_ids,
                      messages=tuple(dict(message_id=item.message_id, episode_id=source_id,
                                          sequence=positions[source_id])
                                     for item, source_id in zip(items, source_ids)),
                      task=task, operation_id=operation_id)
        if status == 'completed':
            receipt = self.dao.receipt(operation_id)
            result['fact_ids'] = receipt.fact_ids if receipt is not None else ()
        return result

    def _store_originals(self, value, session_id, *, agent_id=None, event_time=None):
        identifier(session_id)
        items = message_inputs(value, agent_id=agent_id, event_time=event_time)
        return self._processing.tasks.store_originals(session_id, items)

    def operation(self, operation_id):
        identifier(operation_id)
        return self._processing.tasks.operation(operation_id)

    def retry_operation(self, operation_id):
        identifier(operation_id)
        return self._processing.tasks.retry(operation_id)

    def list_operations(self, *, session_id=None, after_id=None, limit=100):
        for value in (session_id, after_id):
            if value is not None:
                identifier(value)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Expected page size from 1 to 1000')
        return self._processing.tasks.list_operations(session_id, after_id, limit)

    def _query(self, text, *, top_k, as_of=None, facts=None, use_vectors=True):
        embedder = self._embedder if use_vectors else None
        return retrieval.retrieval_query(text, embedder, self._model, self._config,
                                         top_k=top_k, as_of=as_of, facts=facts)

    def search(self, query: str, *, as_of: float | None = None, top_k: int | None = None,
               facts: FactQuery | None = None) -> retrieval.SearchResult:
        """Retrieve candidate evidence or a unique explicitly selected value."""
        count = self._config.top_k if top_k is None else top_k
        explicit_slot = isinstance(facts, FactQuery) and facts.subject is not None and facts.predicate is not None
        request = self._query(query, top_k=count, as_of=as_of, facts=facts, use_vectors=not explicit_slot)
        with self.dao.read_snapshot() as view:
            result = retrieval.search(view.focused(), request, self._config, count)
            snapshot = view.snapshot
            hits = result.facts[:1] if request.facts.is_current_view and not result.abstained else ()
            records = tuple(view.get(FACT, fact.id) for fact in hits)
        self._reinforce(snapshot, records)
        return result

    def aggregate_facts(self, predicate, *, operation='count', subject=None, start=None, end=None,
                        query=None, facts=None):
        """Count structured facts and optionally recall source candidates for an empty match."""
        from .retrieve.structured import validate_aggregate

        scope = FactQuery(limit=10) if facts is None else facts
        if not isinstance(scope, FactQuery):
            raise ValueError('Expected structured fact conditions')
        scope = replace(scope, predicate=predicate, subject=subject if subject is not None else scope.subject)
        validate_aggregate(operation, scope, start, end, query)
        evidence = None
        if query is not None and not scope.exclude_sensitive:
            evidence = self._query(query, top_k=scope.limit, facts=scope)
        with self.dao.read_snapshot() as view:
            return retrieval.aggregate_facts(view.focused(), scope, operation, start, end, evidence)

    def follow_path(self, anchor, predicates, *, facts=None):
        """Traverse an explicit relation path within one authorized snapshot."""
        from .retrieve.structured import validate_path

        scope = FactQuery(limit=20) if facts is None else facts
        predicates = validate_path(anchor, predicates, scope)
        with self.dao.read_snapshot() as view:
            return retrieval.follow_path(view.focused(), scope, anchor, predicates)

    def _reinforce(self, snapshot, records):
        if not records or not self._config.access_boost:
            return
        writes = []
        for record in records:
            fact = domain(record)
            reinforce(fact, self._config.access_boost, track_access=False)
            writes.append(self._write(fact, record))
        try:
            self._commit(snapshot, writes=tuple(writes))
        except VersionConflict:
            # Reinforcement is optional once the snapshot answer is ready.
            pass

    def retrieve_digests(self, query: str, *, k: int = 5, as_of: float | None = None) -> retrieval.DigestSearchResult:
        request = self._query(query, top_k=k, as_of=as_of)
        with self.dao.read_snapshot() as view:
            return retrieval.digest_search(view.focused(), request, k)

    def lean_context(self, query: str, *, as_of: float | None = None, n_facts: int = 15,
                     n_digests: int = 20, char_budget: int = 60_000,
                     redact_sensitive: bool = False, facts: FactQuery | None = None,
                     reinforce_hits: bool = False, include_full_tokens: bool = False) -> retrieval.ContextResult:
        if (any(type(value) is not int or not 0 <= value <= 1000 for value in (n_facts, n_digests))
                or type(char_budget) is not int or char_budget < 0):
            raise ValueError('Invalid context budget')
        count = max(n_facts, n_digests, 1)
        if facts is not None and as_of is not None:
            raise ValueError('Use either as_of or explicit fact conditions')
        facts = replace(facts, exclude_sensitive=facts.exclude_sensitive or redact_sensitive,
                        limit=max(count, self._config.postgres_candidate_limit)) if facts is not None else FactQuery(
                            valid_at=as_of, exclude_sensitive=redact_sensitive,
                            limit=max(count, self._config.postgres_candidate_limit))
        request = self._query(query, top_k=count, facts=facts)
        with self.dao.read_snapshot() as view:
            result = retrieval.context(view.focused(), request, self._config,
                                       n_facts=n_facts, n_digests=n_digests,
                                       char_budget=char_budget)
            snapshot = view.snapshot
            records = ()
            if reinforce_hits and facts.is_current_view and result.fact_ids:
                # Only these visible IDs are touched. Writes need their full predecessor lists.
                records = view.facts_by_ids(result.fact_ids, replace(facts, exclude_sensitive=False))
            if include_full_tokens and not facts.exclude_sensitive:
                result = replace(result, full_tokens=view.full_context_tokens(facts))
        self._reinforce(snapshot, records)
        return result

    def context_for(self, query: str, *, as_of: float | None = None, top_k: int | None = None, k_digests: int = 3,
                    char_budget: int = 60_000) -> retrieval.ContextResult:
        return self.lean_context(query, as_of=as_of, n_facts=self._config.top_k if top_k is None else top_k,
                                 n_digests=k_digests, char_budget=char_budget)

    def recall_multi(self, space_ids, query: str, **options) -> retrieval.ContextResult:
        if isinstance(space_ids, str):
            raise ValueError('Expected a collection of space IDs')
        ids = tuple(space_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('Expected a nonempty collection of distinct space IDs')
        spaces = Spaces(self._database)
        contexts = tuple(spaces.authorize(self.context.user_id, space_id) for space_id in ids)
        blocks, coverage, sections = [], {}, []
        for context in contexts:
            memory = Memory(self._database, context, embedder=self._embedder, embedding_model=self._model,
                                    config=self._config)
            result = memory.lean_context(query, **options)
            if result.context:
                blocks.append(f'## Space: {context.space_id}\n{result.context}')
            coverage.update(result.coverage_by_space)
            sections.extend(result.sections)
        return retrieval.ContextResult('\n\n'.join(blocks), coverage, sections=tuple(sections))

    def graph_entities(self, name):
        identifier(name)
        entity = self.entity(name)
        return (entity,) if entity is not None else ()

    def graph_neighbors(self, entity_id, *, direction='both', facts=FactQuery()):
        identifier(entity_id)
        with self.dao.read_snapshot() as view:
            edges = tuple(map(domain, view.graph_edges((entity_id,), facts, direction=direction)))
            visible = {row.ref.record_id: domain(row)
                       for row in view.facts_by_ids(tuple(edge.fact_id for edge in edges), facts)}
            return tuple(replace(edge, invalid_at=visible[edge.fact_id].invalid_at) for edge in edges)

    def graph_relations(self, fact_id, *, facts=FactQuery()):
        identifier(fact_id)
        with self.dao.read_snapshot() as view:
            records = view.facts_by_ids((fact_id,), facts)
            if not records:
                return ()
            fact = domain(records[0])
            return tuple(replace(domain(row), invalid_at=fact.invalid_at) for row in view.relations(fact_id))

    def get_episode(self, episode_id):
        return self._get(SOURCE, episode_id)

    def _get(self, kind, record_id):
        with self.dao.read_snapshot() as view:
            record = view.get(kind, record_id)
        return domain(record) if record is not None else None

    def list_sources(self, **filters):
        with self.dao.read_snapshot() as view:
            return tuple(map(domain, view.list_sources(**filters)))

    def get_fact(self, fact_id):
        return self._get(FACT, fact_id)

    def facts(self, query=None):
        with self.dao.read_snapshot() as view:
            records = view.facts() if query is None else view.facts(query)
            return tuple(domain(record) for record in records)

    def history(self, subject, predicate=None, *, source_ids=()):
        with self.dao.read_snapshot() as view:
            records = view.history(subject, predicate, source_ids=source_ids)
        return tuple(sorted(map(domain, records), key=lambda fact: (time_key(fact.valid_at), fact.id)))

    def set_fact_sensitive(self, fact_id, sensitive: bool):
        if type(sensitive) is not bool:
            raise ValueError("Expected a boolean sensitivity flag")
        with self.dao.read_snapshot() as view:
            snapshot, old = view.snapshot, view.get(FACT, fact_id)
        if old is None:
            return None
        fact = domain(old)
        if fact.sensitive != sensitive:
            fact.sensitive = sensitive
            self._commit(snapshot, writes=(self._write(fact, old),))
        return fact

    def delete_fact(self, fact_id):
        return bool(self.erase(self._ref(FACT, fact_id)).affected_count)

    def clear_slot(self, subject, predicate, *, expect_count):
        """Erase the confirmed number of live facts in one slot."""
        if type(expect_count) is not int or expect_count < 0:
            raise ValueError('Expected a non-negative fact count')
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
            records = view.clearable_slot(subject, predicate)
        if len({domain(record).slot for record in records}) > 1:
            raise VersionConflict('Ambiguous slot; erase facts by ID')
        if len(records) != expect_count:
            raise VersionConflict('Slot changed since it was counted')
        return self._commit(snapshot, erasures=tuple(record.ref for record in records))

    def erase(self, *records: RecordRef):
        validate_erasures(records)
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
        return self._commit(snapshot, erasures=records)


    def clear_space(self):
        return self.dao.clear_space()

    def delete_space(self):
        return self.dao.delete_space()

    def profile(self):
        from .store.postgres.profile import read
        with self.dao.read_snapshot() as view:
            return read(view)

    def refresh_profile(self):
        from .store.postgres.profile import enqueue_refresh
        return enqueue_refresh(self)

    def get_settings(self):
        with self.dao.read_snapshot() as view:
            return view.settings()

    def set_settings(self, **fields):
        with self.dao.read_snapshot() as view:
            snapshot, settings = view.snapshot, replace(view.settings(), **fields)
        self._commit(snapshot, settings=settings)
        return settings

    def entity(self, name):
        with self.dao.read_snapshot() as view:
            record = view.entity(name)
        return domain(record) if record is not None else None

    def stats(self):
        with self.dao.read_snapshot() as view:
            return {kind.value: count for kind, count in view.counts().items()}

    @contextmanager
    def export_records(self, *, include_sensitive=False):
        """Yield detached records in stable pages under one explicit export snapshot."""
        with self.dao.read_snapshot() as view:
            def records():
                for kind in (MODELS if include_sensitive else (FACT,)):
                    cursor = None
                    while batch := view.records(kind, after_id=cursor,
                                                hide_sensitive_predecessors=not include_sensitive):
                        for record in batch:
                            if kind == FACT and not include_sensitive:
                                fact = domain(record)
                                if fact.sensitive:
                                    continue
                            yield record
                        cursor = batch[-1].ref.record_id

            yield records()