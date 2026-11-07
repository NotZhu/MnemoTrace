"""Explicit Python entry point for record-backed memory in one PostgreSQL space."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from ._postgres_pipeline import PostgresPipeline
from .config import Config
from .consolidate.conflict import ConflictResolver
from .consolidate.decay import reinforce
from .consolidate.manual import apply_manual_edit, build_manual_fact
from .consolidate.pipeline import digest
from .embed import HashingEmbedder
from .retrieve import postgres as retrieval
from .retrieve.evidence import plan_evidence
from .store.contracts import (
    ChangeSet, CommitConditions, EmbeddingWrite, EntityMerge, FactInvalidation, FactQuery, MessageInput, OperationKind,
    RecordKind, RecordRef, RecordWrite, RetrievalQuery, SourceLink, StoredRecord, validate_erasures,
)
from .store.postgres.dao import PostgresDAO, _space_row, new_operation_id
from .store.postgres.database import identifier
from .store.postgres.errors import VersionConflict
from .store.postgres.graph_reads import RetrievalGraph
from .store.postgres.identity import Spaces
from .store.postgres.lexical import project_document, project_text
from .store.postgres.projection import GraphProjection
from .store.postgres.records import CONFLICT, ENTITY, FACT, SOURCE, WORKING, MODELS, anchor_terms, domain
from .store.postgres.writes import queue_source_vector
from .types import Episode, WorkingMemory
from .util import now, time_key

if TYPE_CHECKING:
    from .embed import Embedder
    from .store.contracts import SpaceContext
    from .store.postgres.database import Database


class Memory:
    """Use an open runtime database and an explicit, trusted space. The caller owns the pool."""

    def __init__(self, database: 'Database', context: 'SpaceContext', *,
                 embedder: 'Embedder | None' = None, embedding_model: str | None = None,
                 llm=None, config: Config | None = None):
        if embedder is not None and embedding_model is None:
            raise ValueError('A custom embedder requires a stable embedding_model identifier')
        self.dao = PostgresDAO(database, context)
        self._embedder = embedder if embedder is not None else HashingEmbedder()
        self._model = embedding_model if embedding_model is not None else 'hashing-v1'
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

    def _commit(self, snapshot, *, writes=(), source_embedding=None, **kwargs):
        links = tuple(SourceLink(write.ref, self._ref(SOURCE, source_id))
                      for write in writes if write.ref.kind == FACT
                      for source_id in domain(write.record).provenance)
        return self.dao.commit(ChangeSet(CommitConditions(snapshot), new_operation_id(), writes=writes,
                                         source_links=links, **kwargs), source_embedding=source_embedding)

    def _embedding(self, write, text):
        vector = tuple(self._embedder.embed(text))
        if len(vector) != self._embedder.dim:
            raise ValueError('Embedding dimensions differ from the configured model')
        return EmbeddingWrite(write.ref, write.record.content_version, self._model, vector)

    def store_messages(self, session_id: str, messages: 'tuple[MessageInput, ...]'):
        """Store raw dialogue and return source IDs in input order, including duplicates."""
        return self.dao.accept_raw_messages(session_id, messages, embedding=self._embedding_space)

    @property
    def _embedding_space(self):
        return self._model, self._embedder.dim

    def remember_messages(self, session_id: str, messages: tuple[MessageInput, ...], *,
                          processing='buffered', timeout=30, source_metadata=None):
        """Accept dialogue and return source IDs and durable operation progress."""
        identifier(session_id)
        self._validate_processing(processing, timeout)
        items = tuple(messages)
        if not items or any(not isinstance(item, MessageInput) for item in items):
            raise ValueError('Expected a non-empty batch of MessageInput records')
        pipeline = self._processing
        receipt, associations = pipeline.tasks.accept(
            session_id, items, pipeline.require_model, pipeline.fit_message, immediate=processing == 'immediate',
            embedding=self._embedding_space, source_metadata=source_metadata)
        operation_ids = tuple(dict.fromkeys(operation for _, operation in associations))
        result = self._accepted(operation_ids, processing, timeout)
        result.update(source_ids=receipt.source_ids, accepted=receipt.affected_count,
                      messages=tuple(dict(message_id=item.message_id, episode_id=source_id, operation_ids=tuple(
                          operation for source, operation in associations if source == source_id))
                                     for item, source_id in zip(items, receipt.source_ids)))
        return result

    def remember(self, content: str, session_id: str, *, scope='auto', source_kind='raw',
                 event_time=None, agent_id=None, processing='immediate', timeout=30):
        """Accept a note or already-decoded document for automatic processing."""
        identifier(session_id)
        self._validate_processing(processing, timeout)
        if (not isinstance(content, str) or not content.strip() or '\x00' in content
                or scope not in ('auto', 'long', 'working') or source_kind not in ('raw', 'document')):
            raise ValueError('Invalid source content or routing')
        source = Episode(content, self.context.user_id, session_id, source_kind=source_kind,
                         scope=scope, event_time=event_time)
        record = replace(StoredRecord.from_record(self.context, source), agent_id=agent_id)
        if scope == 'working':
            wall_time = now()
            ttl = self._config.working_memory_ttl_seconds
            working = WorkingMemory(content=content, user_id=self.context.user_id, session_id=session_id,
                                    event_time=event_time if event_time is not None else wall_time,
                                    expires_at=wall_time + ttl if ttl is not None else None,
                                    metadata={'provenance': [source.id]})
            write = self._write(working)
            with self.dao.read_snapshot(session_id) as view:
                snapshot = view.snapshot
            self._commit(snapshot, writes=(RecordWrite(record), write),
                         embeddings=(self._embedding(write, working.content),))
            return dict(ok=True, status='stored', source_ids=(source.id,), operation_ids=(), operations=(),
                        id=working.id, kind=working.kind, scope='working')
        receipt, operation_id = self._processing.tasks.accept_source(
            record, self._processing.require_model, embedding=self._embedding_space)
        result = self._accepted((operation_id,), processing, timeout)
        result['source_ids'] = receipt.source_ids
        return result

    @staticmethod
    def _validate_processing(processing, timeout):
        from math import isfinite
        if processing not in ('buffered', 'immediate'):
            raise ValueError('Expected buffered or immediate processing')
        if type(timeout) not in (int, float) or not isfinite(timeout) or timeout < 0:
            raise ValueError('Expected a finite nonnegative timeout')

    def import_document(self, content, session_id, *, event_time=None, metadata=None, agent_id=None,
                        extract=True, summarize=True, dedupe=True, processing='buffered', timeout=30):
        identifier(session_id)
        self._validate_processing(processing, timeout)
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('Expected non-empty document content')
        episode = Episode(content, self.context.user_id, session_id, source_kind='document',
                          scope='long' if extract else 'raw', event_time=event_time,
                          metadata={**(metadata or {}), 'summary_requested': summarize})
        record = replace(StoredRecord.from_record(self.context, episode), agent_id=agent_id)
        kind = (OperationKind.EXTRACT if extract else OperationKind.SUMMARY
                if summarize and len(content) >= self._config.summary_min_chars else None)
        import_digest = digest([session_id, content, agent_id, extract, summarize]) if dedupe else None
        receipt, operation_id = self._processing.tasks.accept_source(
            record, self._processing.require_model, embedding=self._embedding_space, import_digest=import_digest,
            kind=kind)
        ids = (operation_id,) if operation_id else ()
        return dict(self._accepted(ids, processing, timeout), source_ids=receipt.source_ids,
                    accepted=receipt.affected_count)

    def _accepted(self, operation_ids, processing, timeout):
        if processing == 'immediate':
            return self._processing.flush(operation_ids=operation_ids, timeout=timeout)
        return dict(ok=True, status='accepted', operation_ids=operation_ids,
                    operations=tuple(self.operation(operation_id) for operation_id in operation_ids))

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

    def flush_processing(self, session_id=None, timeout=30, *, retry_failed=False, include_summaries=False):
        if session_id is not None:
            identifier(session_id)
        return self._processing.flush(session_id=session_id, timeout=timeout, retry_failed=retry_failed,
                                      include_summaries=include_summaries)

    def remember_raw(self, content: str, session_id: str, *, role='user',
                     event_time=None, agent_id=None):
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('Expected non-empty raw content')
        source = Episode(content=content, user_id=self.context.user_id, session_id=session_id,
                         role=role, event_time=event_time, scope='raw')
        write = self._write(source)
        write = RecordWrite(replace(write.record, agent_id=agent_id))
        with self.dao.read_snapshot(session_id) as view:
            snapshot = view.snapshot
        self._commit(snapshot, writes=(write,), source_embedding=self._embedding_space)
        return source

    def source_vector_operation(self, source_id):
        """Report the current original-text vector task for the configured embedding space."""
        identifier(source_id)
        return self._processing.tasks.source_vector_operation(source_id, self._embedding_space)

    def backfill_retrieval(self, kind, *, after_id=None, limit=100):
        """Project one page of existing records and return a cursor, or None when exhausted."""
        if not isinstance(kind, RecordKind) or kind not in (FACT, SOURCE, ENTITY):
            raise ValueError('Expected a Fact, Episode, or Entity RecordKind')
        if after_id is not None:
            identifier(after_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Expected page size from 1 to 1000')
        with self.dao._transaction() as sql:
            _, generation, _, _ = _space_row(sql)
            conditions = ('r.id > %s',) if after_id is not None else ()
            records = sql.select(kind, conditions, (after_id,) if conditions else (), limit=limit)
            for record in records:
                item = domain(record)
                if kind == ENTITY:
                    sql.update('entities', {'anchor_terms': anchor_terms(item.name, item.aliases)},
                               'id = %s AND anchor_terms IS NULL', (item.id,))
                    continue
                project_text(sql, kind, item.id, record.content_version, asdict(item), missing_only=True)
                if kind == SOURCE:
                    if item.summary:
                        project_document(sql, SOURCE, item.id, record.summary_version, item.summary,
                                         field='summary', missing_only=True)
                    queue_source_vector(sql, item.id, item.session_id, record.content_version,
                                        generation, self._embedding_space)
        return records[-1].ref.record_id if len(records) == limit else None

    def rebuild_graph(self, *, after_id=None, limit=100):
        """Rebuild one page of graph edges from facts and authoritative entity decisions."""
        if after_id is not None:
            identifier(after_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Expected page size from 1 to 1000')
        with self.dao._transaction() as sql:
            conditions = ('r.id > %s',) if after_id is not None else ()
            records = sql.select(FACT, conditions, (after_id,) if conditions else (), limit=limit)
            graph = GraphProjection(sql)
            for record in records:
                graph.project(record.ref.record_id)
            graph.prune()
        return records[-1].ref.record_id if len(records) == limit else None

    def _query(self, text, *, top_k, as_of=None, facts=None):
        return retrieval.retrieval_query(text, self._embedder, self._model, self._config,
                                         top_k=top_k, as_of=as_of, facts=facts)

    def search(self, query: str, *, as_of: float | None = None, top_k: int | None = None,
               facts: FactQuery | None = None) -> retrieval.SearchResult:
        """Retrieve and answer from a consistent database snapshot."""
        count = self._config.top_k if top_k is None else top_k
        request = self._query(query, top_k=count, as_of=as_of, facts=facts)
        decomposition = revision = None
        llm = self._processing.llm
        if (llm is not None and self._config.planner_llm_decomposition and not request.facts.exclude_sensitive
                and plan_evidence(query).multi_hop):
            with self.dao.read_snapshot() as view:
                revision = view.snapshot.core_revision
                vocabulary = retrieval.graph_vocabulary(view.focused(), request)
            decomposition = retrieval.decompose(llm, query, vocabulary)
        with self.dao.read_snapshot() as view:
            result = retrieval.search(
                view.focused(), request, self._config, count, not isinstance(self._embedder, HashingEmbedder),
                decomposition if revision == view.snapshot.core_revision else None,
            )
            snapshot = view.snapshot
            hits = result.facts if result.via == 'multi-hop' else result.facts[:1]
            records = tuple(view.get(FACT, fact.id) for fact in hits) if not result.abstained else ()
        self._reinforce(snapshot, records)
        return result

    def _reinforce(self, snapshot, records):
        if not records or not self._config.access_boost:
            return
        writes = []
        for record in records:
            fact = domain(record)
            reinforce(fact, self._config.access_boost)
            writes.append(self._write(fact, record))
        try:
            self._commit(snapshot, writes=tuple(writes))
        except VersionConflict:
            # Reinforcement is optional once the snapshot answer is ready.
            pass

    def retrieve_episodes(self, query: str, *, k: int = 5, as_of: float | None = None) -> retrieval.SourceSearchResult:
        request = self._query(query, top_k=k, as_of=as_of)
        with self.dao.read_snapshot() as view:
            return retrieval.source_search(view.focused(), request, k)

    def lean_context(self, query: str, *, as_of: float | None = None, n_facts: int = 15, n_chunks: int = 2,
                     n_summaries: int = 20, char_budget: int = 60_000, session_id: str | None = None,
                     redact_sensitive: bool = False, facts: FactQuery | None = None,
                     reinforce_hits: bool = False) -> retrieval.ContextResult:
        if (any(type(value) is not int or not 0 <= value <= 1000 for value in (n_facts, n_chunks, n_summaries))
                or type(char_budget) is not int or char_budget < 0):
            raise ValueError('Invalid context budget')
        count = max(n_facts, n_chunks, n_summaries, 1)
        if facts is not None and as_of is not None:
            raise ValueError('Use either as_of or explicit fact conditions')
        facts = replace(facts, exclude_sensitive=facts.exclude_sensitive or redact_sensitive,
                        limit=max(count, self._config.ann_pool)) if facts is not None else FactQuery(
                            valid_at=as_of, known_at=as_of, exclude_sensitive=redact_sensitive,
                            limit=max(count, self._config.ann_pool))
        request = self._query(query, top_k=count, facts=facts)
        need = plan_evidence(query, aggregation_recall_expansion=self._config.aggregation_recall_expansion)
        subqueries = need.subqueries if need.aggregation else ()
        related = tuple(self._query(text, top_k=1, facts=facts) for text in subqueries if text != query)
        with self.dao.read_snapshot(session_id) as view:
            result = retrieval.context(view.focused(), request, self._config, not isinstance(self._embedder, HashingEmbedder),
                                       n_facts=n_facts, n_chunks=n_chunks, n_summaries=n_summaries,
                                       char_budget=char_budget, session_id=session_id, evidence_queries=related)
            snapshot = view.snapshot
            records = view.facts_by_ids(result.fact_ids, facts) if reinforce_hits and result.fact_ids else ()
        self._reinforce(snapshot, records)
        return result

    def context_for(self, query: str, *, as_of: float | None = None, top_k: int | None = None, k_chunks: int = 3,
                    char_budget: int = 60_000) -> retrieval.ContextResult:
        return self.lean_context(query, as_of=as_of, n_facts=self._config.top_k if top_k is None else top_k,
                                 n_chunks=k_chunks, n_summaries=0, char_budget=char_budget)

    def recall_multi(self, space_ids, query: str, **options) -> retrieval.ContextResult:
        if isinstance(space_ids, str):
            raise ValueError('Expected a collection of space IDs')
        ids = tuple(space_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError('Expected a nonempty collection of distinct space IDs')
        spaces = Spaces(self._database)
        contexts = tuple(spaces.authorize(self.context.user_id, space_id) for space_id in ids)
        blocks, coverage = [], {}
        for context in contexts:
            memory = Memory(self._database, context, embedder=self._embedder, embedding_model=self._model,
                                    config=self._config)
            result = memory.lean_context(query, **options)
            if result.context:
                blocks.append(f'## Space: {context.space_id}\n{result.context}')
            coverage.update(result.coverage_by_space)
        return retrieval.ContextResult('\n\n'.join(blocks), coverage)

    def graph_entities(self, name, *, limit=100):
        identifier(name)
        request = RetrievalQuery(FactQuery(limit=limit), '', (0.0,), self._model)
        with self.dao.read_snapshot() as view:
            return RetrievalGraph(view, request).find_entities(name)

    def graph_neighbors(self, entity_id, *, direction='both', facts=FactQuery()):
        identifier(entity_id)
        with self.dao.read_snapshot() as view:
            entity_id = GraphProjection(view._sql).canonical(entity_id)
            return tuple(map(domain, view.graph_edges((entity_id,), facts, direction=direction)))

    def graph_relations(self, fact_id, *, facts=FactQuery()):
        identifier(fact_id)
        with self.dao.read_snapshot() as view:
            return tuple(map(domain, view.relations(fact_id))) if view.facts_by_ids((fact_id,), facts) else ()

    def get_episode(self, episode_id):
        return self._get(SOURCE, episode_id)

    def _get(self, kind, record_id):
        with self.dao.read_snapshot() as view:
            record = view.get(kind, record_id)
        return domain(record) if record is not None else None

    def list_sources(self, **filters):
        with self.dao.read_snapshot() as view:
            return tuple(map(domain, view.list_sources(**filters)))

    def add_fact(self, subject, predicate, object, *, valid_at=None, sensitive=None,
                 category=None, topic=None, return_existing=False):
        """Return the new fact. For a covered assertion, optionally return the stored fact."""
        fact = build_manual_fact(subject, predicate, object, self.context.user_id,
                                 valid_at, sensitive, category, topic)
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
            current = view.facts_for_manual_add(subject)
        candidates = [domain(item) for item in current]
        resolver = ConflictResolver()
        action, invalidated = resolver.reconcile(fact, candidates)
        if action == 'duplicate':
            if not return_existing:
                return None
            return next(candidate for candidate in map(domain, current)
                        if resolver.reconcile(replace(fact), [replace(candidate)])[0] == 'duplicate')
        versions = {item.ref.record_id: item for item in current}
        invalidations = tuple(FactInvalidation(
            versions[old.id].ref, versions[old.id].record_version, old.invalid_at, old.expired_at,
        ) for old in invalidated)
        write = self._write(fact)
        self._commit(snapshot, writes=(write,), invalidations=invalidations,
                     embeddings=(self._embedding(write, fact.text),))
        return fact

    def get_fact(self, fact_id):
        return self._get(FACT, fact_id)

    def facts(self, query=None):
        with self.dao.read_snapshot() as view:
            records = view.facts() if query is None else view.facts(query)
            return tuple(map(domain, records))

    def history(self, subject, predicate=None, *, source_ids=()):
        with self.dao.read_snapshot() as view:
            records = view.history(subject, predicate, source_ids=source_ids)
        return tuple(sorted(map(domain, records), key=lambda fact: (time_key(fact.valid_at), fact.id)))

    def update_fact(self, fact_id, *, subject=None, predicate=None, object=None,
                    sensitive=None, category=None, statement=None, topic=None, reset_topic=False):
        with self.dao.read_snapshot() as view:
            snapshot, old = view.snapshot, view.get(FACT, fact_id)
        if old is None:
            return None
        fact = domain(old)
        before = asdict(fact)
        apply_manual_edit(fact, subject, predicate, object, sensitive, category,
                          statement, topic, reset_topic)
        if asdict(fact) == before:
            return fact
        text_changed = fact.text != before['text']
        write = self._write(fact, old, text_changed=text_changed)
        embeddings = (self._embedding(write, fact.text),) if text_changed else ()
        self._commit(snapshot, writes=(write,), embeddings=embeddings)
        return fact

    def delete_fact(self, fact_id):
        return bool(self.erase(self._ref(FACT, fact_id)).affected_count)

    def clear_slot(self, subject, predicate, *, expect_count):
        """Erase the confirmed number of live extracted facts in one slot."""
        if type(expect_count) is not int or expect_count < 0:
            raise ValueError('Expected a non-negative fact count')
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
            records = view.clearable_slot(subject, predicate)
        if len(records) != expect_count:
            raise VersionConflict('Slot changed since it was counted')
        return self._commit(snapshot, erasures=tuple(record.ref for record in records))

    def erase(self, *records: RecordRef):
        validate_erasures(records)
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
        return self._commit(snapshot, erasures=records)

    def remember_working(self, content, session_id, *, kind='state', ttl_seconds=None,
                         event_time=None):
        wall_time = now()
        item = WorkingMemory(
            content=content, user_id=self.context.user_id, session_id=session_id, kind=kind,
            event_time=wall_time if event_time is None else event_time,
            expires_at=wall_time + ttl_seconds if ttl_seconds else None,
        )
        with self.dao.read_snapshot(session_id) as view:
            snapshot = view.snapshot
        write = self._write(item)
        self._commit(snapshot, writes=(write,), embeddings=(self._embedding(write, item.content),))
        return item

    def working_memory(self, **filters):
        with self.dao.read_snapshot() as view:
            return tuple(map(domain, view.working(**filters)))

    def consume_working(self, record_id):
        with self.dao.read_snapshot() as view:
            snapshot, old = view.snapshot, view.get(WORKING, record_id)
        if old is None:
            return False
        item = domain(old)
        if not item.consumed:
            self._commit(snapshot, writes=(self._write(replace(item, consumed=True), old),))
        return True

    def clear_working(self, session_id):
        return self.dao.clear_working(session_id).affected_count

    def sweep_working(self, as_of=None):
        return self.dao.sweep_working(as_of).affected_count

    def clear_space(self):
        return self.dao.clear_space()

    def delete_space(self):
        return self.dao.delete_space()

    def get_settings(self):
        with self.dao.read_snapshot() as view:
            return view.settings()

    def set_settings(self, **fields):
        with self.dao.read_snapshot() as view:
            snapshot, settings = view.snapshot, replace(view.settings(), **fields)
        self._commit(snapshot, settings=settings)
        return settings

    def conflicts(self, **filters):
        with self.dao.read_snapshot() as view:
            return tuple(map(domain, view.conflicts(**filters)))

    def resolve_conflict(self, conflict_id, keep='newer'):
        if keep not in {'older', 'newer', 'both', 'dismiss'}:
            raise ValueError('Invalid conflict resolution')
        with self.dao.read_snapshot() as view:
            snapshot, record = view.snapshot, view.get(CONFLICT, conflict_id)
            if record is None:
                return False
            conflict = domain(record)
            if conflict.status != 'pending':
                return False
            facts = {key: view.get(FACT, getattr(conflict, key)) for key in ('older', 'newer')}
            at = view.at.timestamp()
        conflict.status = 'dismissed' if keep == 'dismiss' else 'resolved'
        writes = [self._write(conflict, record)]
        invalidations = ()
        if keep in facts:
            winning, losing = facts[keep], facts['older' if keep == 'newer' else 'newer']
            if winning is None or losing is None:
                raise VersionConflict('Conflict facts no longer exist')
            winner, loser = domain(winning), domain(losing)
            if not winner.is_live(at) or not loser.is_live(at):
                raise VersionConflict('Conflict facts are no longer current')
            winner.supersedes = loser.id
            invalid_at = (loser.invalid_at if loser.invalid_at is not None
                          else max((winner.valid_at, loser.valid_at), key=time_key))
            invalidations = (FactInvalidation(losing.ref, losing.record_version, invalid_at, now()),)
            writes.append(self._write(winner, winning))
        self._commit(snapshot, writes=tuple(writes), invalidations=invalidations)
        return True

    def entity(self, name):
        with self.dao.read_snapshot() as view:
            record = view.entity(name)
        return domain(record) if record is not None else None

    def set_entity_aliases(self, name, aliases):
        with self.dao.read_snapshot() as view:
            snapshot, record = view.snapshot, view.entity(name)
        if record is None:
            return None
        entity = domain(record)
        entity.aliases = list(aliases)
        self._commit(snapshot, writes=(self._write(entity, record),))
        return entity

    def merge_entities(self, survivor_id, merged_id):
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
        change = EntityMerge(self._ref(ENTITY, survivor_id), self._ref(ENTITY, merged_id))
        return self._commit(snapshot, entity_merges=(change,))

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
                    while batch := view.records(kind, after_id=cursor):
                        for record in batch:
                            if kind != FACT or include_sensitive or not domain(record).sensitive:
                                yield record
                        cursor = batch[-1].ref.record_id

            yield records()
