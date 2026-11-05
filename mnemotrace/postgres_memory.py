"""Explicit Python entry point for record-backed memory in one PostgreSQL space."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from .consolidate.conflict import ConflictResolver
from .consolidate.manual import apply_manual_edit, build_manual_fact
from .embed import HashingEmbedder
from .store.contracts import (
    ChangeSet, CommitConditions, EmbeddingWrite, EntityMerge, FactInvalidation, RecordRef, RecordWrite,
    SourceLink, StoredRecord,
)
from .store.postgres.dao import PostgresDAO, new_operation_id
from .store.postgres.database import identifier
from .store.postgres.errors import VersionConflict
from .store.postgres.records import CONFLICT, ENTITY, FACT, SOURCE, WORKING, MODELS, domain
from .types import Episode, WorkingMemory
from .util import now, time_key

if TYPE_CHECKING:
    from .embed import Embedder
    from .store.contracts import MessageInput, SpaceContext
    from .store.postgres.database import Database


class PostgresMemory:
    """Use an open runtime database and an explicit, trusted space. The caller owns the pool."""

    def __init__(self, database: 'Database', context: 'SpaceContext', *,
                 embedder: 'Embedder | None' = None, embedding_model: str | None = None):
        if embedder is not None and embedding_model is None:
            raise ValueError('A custom embedder requires a stable embedding_model identifier')
        self.dao = PostgresDAO(database, context)
        self._embedder = embedder if embedder is not None else HashingEmbedder()
        self._model = embedding_model if embedding_model is not None else 'hashing-v1'
        identifier(self._model)

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

    def _embedding(self, write, text):
        vector = tuple(self._embedder.embed(text))
        if len(vector) != self._embedder.dim:
            raise ValueError('Embedding dimensions differ from the configured model')
        return EmbeddingWrite(write.ref, write.record.content_version, self._model, vector)

    def store_messages(self, session_id: str, messages: 'tuple[MessageInput, ...]'):
        """Store raw dialogue and return source IDs in input order, including duplicates."""
        return self.dao.accept_raw_messages(session_id, messages)

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
        self._commit(snapshot, writes=(write,))
        return source

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
                 category=None, topic=None):
        """Return the new fact, or None when the existing assertion already covers it."""
        fact = build_manual_fact(subject, predicate, object, self.context.user_id,
                                 valid_at, sensitive, category, topic)
        with self.dao.read_snapshot() as view:
            snapshot = view.snapshot
            current = view.facts_for_manual_add(subject)
        candidates = [domain(item) for item in current]
        action, invalidated = ConflictResolver().reconcile(fact, candidates)
        if action == 'duplicate':
            return None
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
