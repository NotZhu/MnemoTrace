"""Shared execution and waiting for durable Postgres operations."""
from contextlib import contextmanager
import json
import math
from threading import Event, Thread
import time
from types import SimpleNamespace

from .llm.failures import exceeds_model_budget, retryable
from .llm.tokens import ModelContextConfigurationError, TokenCounterConfigurationError
from .consolidate.operations import decode, generate, prepare
from .consolidate.facts import extraction_entries, materialize_facts
from .consolidate.ontology import ENTITY_TYPES, VERSION as ONTOLOGY_VERSION, resolve
from .consolidate.pipeline import config_identity, content_hash, source_fingerprint
from .consolidate.planning import (
    PREVIOUS_MESSAGES, build_batch, source_coverage, validate_config,
)
from .consolidate.postgres_merge import build_record_write, embed_texts, merge_records
from .embed import EmbeddingUnavailable
from .extraction import ExtractionError, require_llm
from .store.contracts import (
    EXTRACTION_POLICY_FIELDS, ChangeSet, CommitConditions, CommitReceipt, EmbeddingWrite, ExtractionProduct,
    OperationKind, SourceVersion,
)
from .store.postgres.errors import AccountNotFound, CommitOutcomeUnknown, DataCorruption, DatabaseError, DatabaseUnavailable
from .store.postgres.errors import SpaceNotFound, VersionConflict
from .store.postgres.ontology import OntologyWorkset
from .store.postgres.records import FACT, domain
from .store.postgres.tasks import ACTIVE, RENEW_SECONDS, Tasks


def _episodes(records):
    for record in records:
        episode = domain(record)
        # Database sequences start at zero. The payload uses positive positions.
        yield episode, 1 if episode.sequence is None else episode.sequence + 1


@contextmanager
def _heartbeat(tasks, lease):
    stop = Event()

    def renew():
        while not stop.wait(RENEW_SECONDS):
            try:
                tasks.renew(lease)
            except DatabaseError:
                return  # The transaction guards will fence this execution if it loses the lease.

    thread = Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


class PostgresPipeline:
    """PostgreSQL 数据库管道。用于处理记录和执行数据库操作。"""

    def __init__(self, dao, config, llm, embedder, model):
        """初始化 PostgresPipeline。

        Args:
            dao: 数据库访问对象。
            config: 配置参数。
            llm: 语言模型。
            embedder: 嵌入器。
            model: 模型。
        """
        self.dao = dao
        self.tasks = Tasks(dao)
        self.config, self.llm, self.embedder, self.model = config, llm, embedder, model

    def require_ready(self):
        """验证系统就绪：模型存在且配置合法。"""
        require_llm(self.llm)
        validate_config(self.config)

    def _fingerprint(self, settings):
        policy = {field: getattr(settings, field) for field in EXTRACTION_POLICY_FIELDS}
        identity = config_identity(SimpleNamespace(config=self.config, policy=policy, llm=self.llm,
                                                   embedder=self.embedder), include_embedding=False,
                                   generation_only=True)
        return content_hash([ONTOLOGY_VERSION, identity])

    def _entity_types(self, view):
        learned = OntologyWorkset(self.dao, view.snapshot).types(view)
        return {**ENTITY_TYPES, **{code: item.meaning for code, item in learned.items()}}

    def _prepare(self, view, records, settings, fingerprint):
        first = domain(records[0])
        previous = view.preceding_sources(first, limit=PREVIOUS_MESSAGES)
        types = self._entity_types(view)
        data = build_batch(_episodes(records), _episodes(previous), self.config, entity_types=types)
        policy = settings.extract_system + settings.extract_instruction
        messages = prepare(data, self.config, policy)
        planned = dict(input=data, messages=messages, request_hash=content_hash(messages), fingerprint=fingerprint,
                       context_versions=[dict(source_id=r.ref.record_id, content_version=r.content_version)
                                         for r in previous],
                       pending_versions=[dict(source_id=r.ref.record_id, content_version=r.content_version)
                                         for r in records])
        return planned, (*records, *previous)

    def _generate(self, lease, snapshot, settings, fingerprint, planned, records):
        data = planned['input']
        with generate(self.llm, data, self.config, '', prepared=planned['messages']) as completion:
            result = decode(completion, data, self.config)
        versions = tuple(SourceVersion(record.ref.record_id, record.content_version) for record in records)
        product = ExtractionProduct(CommitConditions(snapshot, versions, lease, fingerprint), json.dumps(data),
                                    json.dumps(result), self.config.digest_max_chars, None)
        self.tasks.save_product(product, settings)
        return result, versions

    def _planned_records(self, view, planned, records, fingerprint):
        if planned.get('fingerprint') != fingerprint:
            return None
        if content_hash(planned.get('messages')) != planned.get('request_hash'):
            raise DataCorruption('Invalid saved extraction request')
        if planned['input']['entity_types'] != self._entity_types(view):
            return None
        expected = {item['source_id']: item['content_version']
                    for item in planned['pending_versions'] + planned['context_versions']}
        previous = view.sources(tuple(item['source_id'] for item in planned['context_versions']))
        all_records = (*records, *previous)
        if {r.ref.record_id: r.content_version for r in all_records} != expected:
            return None
        inputs = {item['id']: item for item in (*planned['input']['pending'], *planned['input']['context'])}
        if any(source_fingerprint(domain(r)) != inputs[r.ref.record_id]['source'] for r in all_records):
            return None
        return all_records

    def _changes(self, conditions, writes=(), embeddings=()):
        operation_id = conditions.lease.operation_id
        revision = conditions.snapshot.core_revision + int(bool(writes))
        receipt = CommitReceipt(operation_id, revision,
                                fact_ids=tuple(w.ref.record_id for w in writes if w.ref.kind == FACT))
        return ChangeSet(conditions, operation_id, tuple(writes),
                         embeddings=tuple(embeddings), receipt=receipt)

    def _commit_core(self, conditions, data, result, records, now, space):
        context = conditions.snapshot.context
        sources = {item['id']: item for item in (*data['context'], *data['pending'])}
        input_hash = content_hash(data)
        candidates = extraction_entries(data, result, input_hash)
        entries = [(candidate_id, item, {key: sources[key] for key in item['source_ids']})
                   for candidate_id, item in candidates]
        ontology = resolve(OntologyWorkset(self.dao, conditions.snapshot), entries, data['entity_types'],
                           context.user_id, context.space_id, self.llm, self.config)
        facts = materialize_facts(context.user_id, data, now, candidates, bindings=ontology.bindings)
        with self.dao.read_snapshot() as view:
            if (view.snapshot.core_revision != conditions.snapshot.core_revision
                    or view.snapshot.space_generation != conditions.snapshot.space_generation):
                raise VersionConflict('Fact workset changed during ontology resolution')
            previous = view.merge_history(facts, symmetric=ontology.symmetric)
            vectors = view.fact_vectors(previous, *space) if space is not None else {}
        writes, embeddings = merge_records(context, self.config, self.embedder, self.model,
                                           now, facts, previous, vectors, self.llm, ontology=ontology)
        by_id = {record.ref.record_id: record for record in records}
        digests = result['digests']
        digest_vectors = embed_texts(self.embedder, digests.values())
        for item in data['pending']:
            previous = by_id[item['id']]
            episode = domain(previous)
            episode.consolidated = True
            episode.coverage = source_coverage(item, input_hash)
            if episode.id in digests:
                episode.digest = digests[episode.id]
                episode.digest_coverage = source_coverage(item, input_hash)
            write = build_record_write(context, episode, previous)
            writes.append(write)
            if episode.id in digests and self.embedder is not None:
                vector = tuple(digest_vectors[episode.digest])
                embeddings.append(EmbeddingWrite(write.ref, write.record.digest_version, self.model,
                                                 vector, field='digest'))
        self.tasks.commit(self._changes(conditions, writes, embeddings))

    def run_next_stage(self, operation_id):
        claimed = self.tasks.claim(operation_id)
        if claimed is None:
            return False
        lease, kind = claimed
        if kind is OperationKind.PROFILE:
            from .store.postgres.profile import run
            return run(self, lease)
        space = (self.model, self.embedder.dim) if self.embedder is not None else None
        phase = 'generate'
        with _heartbeat(self.tasks, lease):
            try:
                with self.dao.read_snapshot() as view:
                    snapshot, row, records = self.tasks.load(view, lease)
                    phase = row['phase']
                    self.require_ready()
                    settings = view.settings()
                    fingerprint = self._fingerprint(settings)
                    product = self.tasks.product(view, snapshot, lease, row)
                    planned = row['planned_request']
                    using = self._planned_records(view, planned, records, fingerprint) if planned else None
                    if product is None and using is None:
                        planned, using = self._prepare(view, records, settings, fingerprint)
                        persist_plan = True
                    else:
                        persist_plan = False

                if product is None:
                    if persist_plan:
                        self.tasks.freeze_request(lease, planned)
                    result, versions = self._generate(lease, snapshot, settings, fingerprint, planned, using)
                    data = planned['input']
                elif using is None or not product.reusable(snapshot, tuple(
                        SourceVersion(record.ref.record_id, record.content_version) for record in using), fingerprint):
                    self.tasks.regenerate(lease)
                    return True
                else:
                    data, result = json.loads(product.input_json), json.loads(product.result_json)
                    versions = product.conditions.sources

                phase = 'embed'
                # Acceptance may have advanced the space revision during generation.
                # Resolve against a fresh business snapshot while keeping the lease.
                with self.dao.read_snapshot(snapshot.session_id) as view:
                    if self._fingerprint(view.settings()) != fingerprint or self._planned_records(
                            view, planned, view.sources(tuple(item['id'] for item in data['pending'])),
                            fingerprint) is None:
                        changed = True
                    else:
                        changed = False
                        conditions = CommitConditions(view.snapshot, versions, lease, fingerprint)
                        now = view.at.timestamp()
                if changed:
                    self.tasks.regenerate(lease)
                else:
                    self._commit_core(conditions, data, result, records, now, space)
            except (AccountNotFound, SpaceNotFound, CommitOutcomeUnknown):
                return True
            except Exception as exc:
                code, can_retry = _failure(exc, phase)
                try:
                    self.tasks.fail(lease, code, can_retry)
                except (AccountNotFound, SpaceNotFound, VersionConflict):
                    pass
        return True

    def wait(self, operation_id, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Expected a finite nonnegative timeout')
        deadline = time.monotonic() + timeout
        while True:
            task = self.tasks.operation(operation_id)
            remaining = deadline - time.monotonic()
            if task is None or task.state.value not in ACTIVE or remaining <= 0:
                return task
            time.sleep(min(remaining, .05))


def _failure(exc, phase):
    if isinstance(exc, (ModelContextConfigurationError, TokenCounterConfigurationError)):
        return 'configuration_invalid', False
    if exceeds_model_budget(exc):
        return 'model_budget_exceeded', False
    if isinstance(exc, EmbeddingUnavailable):
        return 'embedding_unavailable', retryable(exc)
    if isinstance(exc, DataCorruption):
        return 'data_corruption', False
    if isinstance(exc, VersionConflict):
        return 'version_conflict', True
    if isinstance(exc, DatabaseUnavailable):
        return 'database_unavailable', True
    if isinstance(exc, ExtractionError):
        return exc.code, retryable(exc)
    if isinstance(exc, (TypeError, ValueError)):
        return ('embedding_invalid' if phase == 'embed' else 'configuration_invalid'), False
    if phase == 'embed':
        return 'embedding_or_commit_failed', retryable(exc)
    return 'extraction_failed', retryable(exc)
