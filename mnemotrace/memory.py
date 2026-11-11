"""Explicit Python entry point for record-backed memory in one PostgreSQL space."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from ._postgres_pipeline import PostgresPipeline
from .config import Config
from .consolidate.decay import reinforce
from .consolidate.manual import apply_manual_edit, build_manual_fact, reconcile_manual, validate_fact_patch
from .retrieve import postgres as retrieval
from .store.contracts import (
    ChangeSet, CommitConditions, EmbeddingWrite, FactQuery, MessageInput, OperationKind,
    RecordKind, RecordRef, RecordWrite, RetrievalQuery, SourceLink, StoredRecord, validate_erasures,
)
from .store.postgres.dao import PostgresDAO, new_operation_id
from .store.postgres.database import identifier
from .store.postgres.errors import VersionConflict
from .store.postgres.graph_reads import RetrievalGraph
from .store.postgres.identity import Spaces
from .store.postgres.lexical import project_document, project_text
from .store.postgres.projection import GraphProjection
from .store.postgres.records import CONFLICT, ENTITY, FACT, SOURCE, MODELS, anchor_terms, domain
from .types import Episode
from .util import time_key

if TYPE_CHECKING:
    from .embed import Embedder
    from .store.contracts import SpaceContext
    from .store.postgres.database import Database


class Memory:
    """Use an open runtime database and an explicit, trusted space. The caller owns the pool."""

    def __init__(self, database: 'Database', context: 'SpaceContext', *,
                 embedder: 'Embedder | None' = None, embedding_model: str | None = None,
                 llm=None, config: Config | None = None):
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
        links = tuple(SourceLink(write.ref, self._ref(SOURCE, source_id))
                      for write in writes if write.ref.kind == FACT
                      for source_id in domain(write.record).provenance)
        return self.dao.commit(ChangeSet(CommitConditions(snapshot), new_operation_id(), writes=writes,
                                         source_links=links, **kwargs))

    def _embeddings(self, write, text):
        if self._embedder is None:
            return ()
        vector = tuple(self._embedder.embed(text))
        if len(vector) != self._embedder.dim:
            raise ValueError('Embedding dimensions differ from the configured model')
        return (EmbeddingWrite(write.ref, write.record.content_version, self._model, vector),)

    def store_messages(self, session_id: str, messages: 'tuple[MessageInput, ...]'):
        """Store raw dialogue and return source IDs in input order, including duplicates."""
        return self.dao.accept_raw_messages(session_id, messages)

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
            source_metadata=source_metadata)
        operation_ids = tuple(dict.fromkeys(operation for _, operation in associations))
        result = self._accepted(operation_ids, processing, timeout)
        result.update(source_ids=receipt.source_ids, accepted=receipt.affected_count,
                      messages=tuple(dict(message_id=item.message_id, episode_id=source_id, operation_ids=tuple(
                          operation for source, operation in associations if source == source_id))
                                     for item, source_id in zip(items, receipt.source_ids)))
        return result

    def remember(self, content: str, session_id: str, *, extract: bool = True,
                 event_time=None, agent_id=None, processing='immediate', timeout=30):
        """Save a note and optionally request fact extraction."""
        identifier(session_id)
        self._validate_processing(processing, timeout)
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('Invalid source content or routing')
        source = Episode(content, self.context.user_id, session_id, source_kind='note',
                         extract=extract, event_time=event_time)
        record = replace(StoredRecord.from_record(self.context, source), agent_id=agent_id)
        receipt, operation_id = self._processing.tasks.accept_source(
            record, self._processing.require_model,
            kind=OperationKind.EXTRACT if extract else None)
        if not extract:
            return dict(ok=True, status='stored', stored_raw=True, source_ids=list(receipt.source_ids),
                        operation_ids=[], operations=[])
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

    def flush_processing(self, session_id=None, timeout=30, *, retry_failed=False, include_digests=False):
        if session_id is not None:
            identifier(session_id)
        return self._processing.flush(session_id=session_id, timeout=timeout, retry_failed=retry_failed,
                                      include_digests=include_digests)

    def remember_raw(self, content: str, session_id: str, *, role='user',
                     event_time=None, agent_id=None):
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('Expected non-empty raw content')
        source = Episode(content=content, user_id=self.context.user_id, session_id=session_id,
                         role=role, event_time=event_time, extract=False)
        with self.dao.read_snapshot(session_id) as view:
            snapshot = view.snapshot
            # Match the database clock used by known_at cutoffs and message ingestion.
            source.ingested_at = view.at.timestamp()
        write = self._write(source)
        write = RecordWrite(replace(write.record, agent_id=agent_id))
        self._commit(snapshot, writes=(write,))
        return source

    def backfill_retrieval(self, kind, *, after_id=None, limit=100):
        """Project one page of existing records and return a cursor, or None when exhausted."""
        if not isinstance(kind, RecordKind) or kind not in (FACT, SOURCE, ENTITY):
            raise ValueError('Expected a Fact, Episode, or Entity RecordKind')
        if after_id is not None:
            identifier(after_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Expected page size from 1 to 1000')
        with self.dao._transaction() as sql:
            conditions = ('r.id > %s',) if after_id is not None else ()
            records = sql.select(kind, conditions, (after_id,) if conditions else (), limit=limit)
            for record in records:
                item = domain(record)
                if kind == ENTITY:
                    sql.update('entities', {'anchor_terms': anchor_terms(item.name)},
                               'id = %s AND anchor_terms IS NULL', (item.id,))
                    continue
                if kind == FACT:
                    project_text(sql, kind, item.id, record.content_version, asdict(item), missing_only=True)
                elif item.digest:
                    project_document(sql, SOURCE, item.id, record.digest_version, item.digest,
                                     field='digest', missing_only=True)
        return records[-1].ref.record_id if len(records) == limit else None

    def rebuild_graph(self, *, after_id=None, limit=100):
        """Rebuild one page of graph edges from stored facts."""
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
            graph.prune(all_nodes=len(records) < limit)
        return records[-1].ref.record_id if len(records) == limit else None

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
            reinforce(fact, self._config.access_boost)
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
                        limit=max(count, self._config.ann_pool)) if facts is not None else FactQuery(
                            valid_at=as_of, known_at=as_of, exclude_sensitive=redact_sensitive,
                            limit=max(count, self._config.ann_pool))
        request = self._query(query, top_k=count, facts=facts)
        with self.dao.read_snapshot() as view:
            result = retrieval.context(view.focused(), request, self._config,
                                       n_facts=n_facts, n_digests=n_digests,
                                       char_budget=char_budget)
            snapshot = view.snapshot
            records = ()
            if reinforce_hits and facts.is_current_view and result.fact_ids:
                records = view.facts_by_ids(result.fact_ids, facts)
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

    def graph_entities(self, name, *, limit=100):
        identifier(name)
        request = RetrievalQuery(FactQuery(limit=limit), '')
        with self.dao.read_snapshot() as view:
            return RetrievalGraph(view, request).find_entities(name)

    def graph_neighbors(self, entity_id, *, direction='both', facts=FactQuery()):
        identifier(entity_id)
        with self.dao.read_snapshot() as view:
            edges = tuple(map(domain, view.graph_edges((entity_id,), facts, direction=direction)))
            visible = {row.ref.record_id: domain(row).as_known_at(facts.known_at)
                       for row in view.facts_by_ids(tuple(edge.fact_id for edge in edges), facts)}
            return tuple(replace(edge, invalid_at=visible[edge.fact_id].invalid_at) for edge in edges)

    def graph_relations(self, fact_id, *, facts=FactQuery()):
        identifier(fact_id)
        with self.dao.read_snapshot() as view:
            records = view.facts_by_ids((fact_id,), facts)
            if not records:
                return ()
            fact = domain(records[0]).as_known_at(facts.known_at)
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

    def add_fact(self, subject, predicate, object, *, text, valid_at=None, sensitive=None,
                 categories=None, object_kind="unknown", cardinality="unknown", retention="unknown", return_existing=False):
        """Return the new fact. For a covered assertion, optionally return the stored fact."""
        fact = build_manual_fact(subject, predicate, object, text, self.context.user_id,
                                 valid_at, sensitive, categories, object_kind, cardinality, retention)
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
            at = view.at.timestamp()
            history = view.facts_for_manual_add(fact.subject, predicate)
        # Creation and interval revisions must use the same database clock for known_at reads.
        fact.created_at = fact.last_access = at
        candidates = [domain(item) for item in history]
        action, changed = reconcile_manual(fact, candidates, at, explicit_valid_at=valid_at is not None)
        versions = {item.ref.record_id: item for item in history}
        if action == 'duplicate':
            duplicate, = changed
            if duplicate != domain(versions[duplicate.id]):
                self._commit(snapshot, writes=(self._write(duplicate, versions[duplicate.id]),))
            return duplicate if return_existing else None
        updates = tuple(self._write(old, versions[old.id]) for old in changed)
        write = self._write(fact)
        self._commit(snapshot, writes=(write, *updates),
                     embeddings=self._embeddings(write, fact.text))
        return fact

    def get_fact(self, fact_id):
        fact = self._get(FACT, fact_id)
        return fact if fact is not None and fact.is_published() else None

    def facts(self, query=None):
        with self.dao.read_snapshot() as view:
            records = view.facts() if query is None else view.facts(query)
            known_at = None if query is None else query.known_at
            return tuple(domain(record).as_known_at(known_at) for record in records)

    def history(self, subject, predicate=None, *, source_ids=()):
        with self.dao.read_snapshot() as view:
            records = view.history(subject, predicate, source_ids=source_ids)
        return tuple(sorted(map(domain, records), key=lambda fact: (time_key(fact.valid_at), fact.id)))

    def update_fact(self, fact_id, *, subject=None, predicate=None, object=None,
                    sensitive=None, categories=None, text=None, object_kind=None, cardinality=None, retention=None):
        validate_fact_patch(subject, predicate, object, text)
        with self.dao.read_snapshot() as view:
            snapshot, old = view.snapshot, view.get(FACT, fact_id)
            at = view.at.timestamp()
        if old is None:
            return None
        fact = domain(old)
        if not fact.is_published():
            return None
        before = asdict(fact)
        apply_manual_edit(fact, subject, predicate, object, sensitive, categories,
                          text, object_kind, cardinality, retention, at=at)
        if asdict(fact) == before:
            return fact
        text_changed = fact.text != before['text']
        write = self._write(fact, old, text_changed=text_changed)
        embeddings = self._embeddings(write, fact.text) if text_changed else ()
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
            at = view.at.timestamp()
            writes = []
            if keep != 'dismiss':
                from .consolidate.adjudication import (
                    approval_candidate, conflict_is_reviewable, has_other_manual_blocker, resolve_facts,
                )

                facts = {key: view.get(FACT, getattr(conflict, key)) for key in ('older', 'newer')}
                if any(value is None for value in facts.values()):
                    raise VersionConflict('Conflict facts no longer exist')
                domain_facts = {key: domain(value) for key, value in facts.items()}
                if not conflict_is_reviewable(domain_facts['older'], domain_facts['newer'], at):
                    raise VersionConflict('Conflict facts are no longer current')
                candidate = approval_candidate(domain_facts, keep)
                if candidate is not None and has_other_manual_blocker(
                    candidate, conflict_id, view.related_conflicts(candidate.id, excluding=conflict_id), at,
                ):
                    raise VersionConflict('Other conflicts still protect manual facts from this candidate')
                previous = {value.ref.record_id: value for value in facts.values()}
                history = []
                if keep in domain_facts and domain_facts[keep].review_pending:
                    other = domain_facts['newer' if keep == 'older' else 'older']
                    if other.invalid_at is not None:
                        records = view.merge_history({other.subject.strip().lower()})
                        previous.update((value.ref.record_id, value) for value in records)
                        history = list(map(domain, records))
                writes.extend(self._write(fact, previous[fact.id])
                              for fact in resolve_facts(domain_facts, keep, at, history))
        conflict.status = 'dismissed' if keep == 'dismiss' else 'resolved'
        writes.append(self._write(conflict, record))
        self._commit(snapshot, writes=tuple(writes))
        return True

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
                    while batch := view.records(kind, after_id=cursor):
                        for record in batch:
                            if kind == FACT and not include_sensitive:
                                fact = domain(record)
                                if fact.sensitive or not fact.is_published():
                                    continue
                            yield record
                        cursor = batch[-1].ref.record_id

            yield records()
