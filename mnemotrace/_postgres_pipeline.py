"""Shared execution and waiting for durable Postgres operations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json
import math
from threading import Event, Thread
import time
from types import SimpleNamespace

from .llm.failures import retryable
from .consolidate.operations import decode, prepare
from .consolidate.facts import materialize_facts
from .consolidate.pipeline import config_identity, content_hash
from .consolidate.planning import (
    OMITTED, empty_input, fit_digest, fit_window, input_size, output_size, supplied, user_start, validate_config,
)
from .consolidate.postgres_merge import embed_texts, merge_records, write_record
from .embed import EmbeddingUnavailable
from .extraction import ExtractionError, ExtractionResponseError, ExtractionTruncatedError, require_llm
from .llm.base import complete_result
from .llm.execution import one_attempt
from .store.contracts import (
    ChangeSet, CommitConditions, CommitReceipt, EmbeddingWrite, ExtractionProduct, OperationStats, SourceVersion,
)
from .store.postgres.errors import AccountNotFound, CommitOutcomeUnknown, DataCorruption, DatabaseError, DatabaseUnavailable
from .store.postgres.errors import SpaceNotFound, VersionConflict
from .store.postgres.records import FACT, domain
from .store.postgres.tasks import ACTIVE, RENEW_SECONDS, Tasks


def _route(ep):
    return dict(mode=ep.mode, extract=ep.extract, source_kind=ep.source_kind)


def _row(ep, minimum):
    digest_requested = ep.metadata.get('digest_requested', True)
    position = ep.coverage['ranges'][-1][1] if ep.coverage and ep.source_kind != 'dialogue' else 0
    # A raw source saved outside the pipeline can be queued later as a single-source window.
    sequence = 1 if ep.sequence is None else ep.sequence + 1
    return dict(sequence=sequence, source_kind=ep.source_kind, position=position,
                digest='pending' if digest_requested and len(ep.content) >= minimum and not ep.digest else 'not_needed',
                core='completed' if ep.consolidated else 'pending', digest_requested=digest_requested)


def _coverage(item, data, ranges, policy, complete):
    return dict(unit='unicode_code_points', ranges=ranges, partial=ranges != [[0, item['source_length']]],
                policy=policy, source=item['source'], original_length=item['source_length'],
                input_hash=content_hash(data), strategy_completed=complete)


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
    def __init__(self, dao, config, llm, embedder, model):
        self.dao = dao
        self.tasks = Tasks(dao)
        self.config, self.llm, self.embedder, self.model = config, llm, embedder, model

    def require_model(self):
        require_llm(self.llm)
        validate_config(self.config)

    def fit_message(self, current, record, settings):
        episode = domain(record)
        episodes = [domain(item) for item in current]
        if episodes and _route(episodes[0]) != _route(episode):
            return False, False
        episodes.append(episode)
        policy = settings['extract_system'] + settings['extract_instruction']
        data = fit_window(empty_input(_route(episode)),
                          ((ep, _row(ep, self.config.digest_min_chars)) for ep in episodes), self.config, policy)
        fits = len(data['pending']) == len(episodes)
        return fits, (any(item['partial'] for item in data['pending'])
                      or input_size(data, self.config, policy) >= self.config.extraction_max_input_bytes
                      or output_size(data, self.config) + self.config.joint_digest_output_tokens
                      > self.config.joint_max_output_tokens)

    def _fingerprint(self, settings):
        policy = dict(extract_system=settings.extract_system, extract_instruction=settings.extract_instruction,
                      digest_system=settings.digest_system)
        return config_identity(SimpleNamespace(config=self.config, policy=policy, llm=self.llm,
                                               embedder=self.embedder), include_embedding=False)

    def _prepare(self, view, row, records, settings, fingerprint):
        episodes = [domain(record) for record in records]
        if row['kind'] == 'digest':
            episode = episodes[0]
            parts = self.tasks.digest_parts(view, row['operation_id'], episode.id, fingerprint)
            status = dict(_row(episode, self.config.digest_min_chars), digest='pending', parts=parts)
            if len(parts) == 1 and parts[0]['start'] == 0 and parts[0]['end'] == len(episode.content):
                item = supplied(episode, status)
                if episode.source_kind == 'dialogue' and episode.coverage:
                    ranges = episode.coverage['ranges']
                    item.update(ranges=ranges, partial=episode.coverage['partial'],
                                content=OMITTED.join(episode.content[a:b] for a, b in ranges))
                data = dict(empty_input(_route(episode), 'digest'), pending=[item])
                return data, records, parts[0]['text']
            return fit_digest(_route(episode), episode, status, self.config, settings.digest_system), records, None

        pending = [episode for episode in episodes if not episode.consolidated]
        first = pending[0]
        previous = view.preceding_sources(first, self.config.extraction_context_bytes)
        data = empty_input(_route(first))
        context = [supplied(ep, _row(ep, self.config.digest_min_chars), context=True)
                   for ep in map(domain, previous)]
        data['context'] = user_start(context) if first.source_kind == 'dialogue' else context
        return fit_window(data, ((ep, _row(ep, self.config.digest_min_chars)) for ep in pending),
                          self.config, settings.extract_system + settings.extract_instruction), (*records, *previous), None

    def _generate(self, lease, snapshot, settings, fingerprint, prepared):
        data, records, cached_digest = prepared
        if cached_digest is None:
            policy = (settings.digest_system if data['operation'] == 'digest'
                      else settings.extract_system + settings.extract_instruction)
            prompt, system, tokens = prepare(data, self.config, policy)
            completion = complete_result(one_attempt(self.llm), prompt, system=system, max_tokens=tokens)
            if completion.finish_reason not in {None, 'stop', 'end_turn'}:
                raise ExtractionTruncatedError()
            result = decode(completion.text, data, self.config)
        else:
            result = dict(facts=[], digests={data['pending'][0]['id']: cached_digest})
        if data['operation'] == 'digest' and not result['digests']:
            raise ExtractionResponseError()
        ids = {item['id'] for item in (*data['context'], *data['pending'])}
        sources = tuple(dict.fromkeys(SourceVersion(record.ref.record_id, record.content_version)
                                      for record in records if record.ref.record_id in ids))
        context_revision = snapshot.core_revision if any(item['representation'] == 'digest'
                                                        for item in (*data['context'], *data['pending'])) else None
        product = ExtractionProduct(CommitConditions(snapshot, sources, lease, fingerprint), json.dumps(data),
                                    json.dumps(result), OperationStats(0, 0, 0, int(cached_digest is None), 0),
                                    self.config.digest_max_chars, context_revision)
        self.tasks.save_product(product, settings)

    def _changes(self, conditions, writes=(), embeddings=(), links=()):
        operation_id = conditions.lease.operation_id
        revision = conditions.snapshot.core_revision + int(bool(writes))
        receipt = CommitReceipt(operation_id, revision,
                                fact_ids=tuple(w.ref.record_id for w in writes if w.ref.kind == FACT))
        return ChangeSet(conditions, operation_id, tuple(writes), source_links=tuple(links),
                         embeddings=tuple(embeddings), receipt=receipt)

    def _commit_core(self, conditions, data, result, records, merged):
        context = conditions.snapshot.context
        facts, previous, vectors, now, existing_pairs = merged
        writes, embeddings, links = merge_records(context, self.config, self.embedder, self.model, data,
                                                  now, facts, previous, vectors, self.llm, existing_pairs)
        by_id = {record.ref.record_id: record for record in records}
        completed = {record.ref.record_id for record in records if domain(record).consolidated}
        newly_completed = set()
        for item in data['pending']:
            previous = by_id[item['id']]
            episode = domain(previous)
            dialogue = episode.source_kind == 'dialogue'
            end = item['ranges'][-1][1]
            episode.consolidated = dialogue or end == len(episode.content)
            if episode.consolidated:
                newly_completed.add(episode.id)
            ranges = item['ranges'] if dialogue else [[0, end]]
            policy = ('head_tail_v1' if item['partial'] else 'full_message_v1') if dialogue else 'full_source_v1'
            episode.coverage = _coverage(item, data, ranges, policy, episode.consolidated)
            writes.append(write_record(context, episode, previous))
        operation_id = conditions.lease.operation_id
        continuation = ('seg_' + content_hash([operation_id, data])[:48]
                        if completed | newly_completed != set(by_id) else None)
        changes = self._changes(conditions, writes, embeddings, links)

        def digests(sql, generation):
            self.tasks.save_digests(sql, operation_id, generation,
                                      conditions.snapshot.session_id, data['pending'], result['digests'],
                                      newly_completed, self.config.digest_min_chars, conditions.extraction_config)

        self.tasks.commit(changes, continuation=continuation, after=digests)

    def _commit_digest(self, conditions, data, result, record, digest_base):
        episode = domain(record)
        item = data['pending'][0]
        text = result['digests'][episode.id]
        operation_id = conditions.lease.operation_id
        if episode.source_kind != 'dialogue' and item['ranges'] != [[0, len(episode.content)]]:
            def save_part(sql, _generation):
                self.tasks.save_part(sql, operation_id, item, text, conditions.extraction_config, data['parts'])

            # A different digest configuration can regenerate the same source range.
            segment_id = 'seg_' + content_hash([operation_id, conditions.extraction_config, data])[:48]
            self.tasks.commit(self._changes(conditions), continuation=segment_id,
                              after=save_part)
            return
        episode.digest = text
        episode.digest_coverage = _coverage(item, data, item['ranges'], 'digest_source_v1', True)
        write = write_record(conditions.snapshot.context, episode, record, digest_base=digest_base)
        embeddings = ()
        if self.embedder is not None:
            vector = embed_texts(self.embedder, [text])[text]
            embeddings = (EmbeddingWrite(write.ref, write.record.digest_version, self.model,
                                         tuple(vector), field='digest'),)
        self.tasks.commit(self._changes(conditions, (write,), embeddings))

    def run_one(self, operation_id, *, force=False):
        existing = self.tasks.operation(operation_id)
        if existing is not None and existing.kind.value == "profile":
            from .store.postgres.profile import run
            return run(self, operation_id)
        space = (self.model, self.embedder.dim) if self.embedder is not None else None
        lease = self.tasks.claim(operation_id, force=force, embedding=space)
        if lease is None:
            return False
        phase = 'generate'
        with _heartbeat(self.tasks, lease):
            try:
                with self.dao.read_snapshot() as view:
                    snapshot, row, records = self.tasks.load(view, lease)
                    phase = row['phase']
                    if row['kind'] != 'reembed':
                        self.require_model()
                        settings = view.settings()
                        fingerprint = self._fingerprint(settings)
                        product = self.tasks.product(view, snapshot, lease, row)
                        if product is None:
                            prepared = self._prepare(view, row, records, settings, fingerprint)
                        else:
                            source_records = view.sources(tuple(ref.source_id for ref in product.conditions.sources))
                            versions = tuple(SourceVersion(record.ref.record_id, record.content_version)
                                             for record in source_records)
                            reuse = product.reusable(snapshot, versions, fingerprint)
                            if reuse:
                                data, result = json.loads(product.input_json), json.loads(product.result_json)
                                if row['kind'] == 'extract':
                                    now = view.at.timestamp()
                                    facts = materialize_facts(snapshot.context.user_id, data, result, now)
                                    history = view.merge_history({fact.subject.strip().lower() for fact in facts})
                                    vectors = view.fact_vectors(history, *space) if space is not None else {}
                                    merged = facts, history, vectors, now, view.conflict_pairs(history)
                                else:
                                    digest_base = view.digest_version(records[0].ref.record_id)
                if row['kind'] == 'reembed':
                    record, = records
                    text = domain(record).content
                    vector = embed_texts(self.embedder, [text])[text]
                    self.tasks.commit_source_vector(lease, record, self.model, tuple(vector))
                elif product is None:
                    self._generate(lease, snapshot, settings, fingerprint, prepared)
                elif not reuse:
                    self.tasks.regenerate(lease)
                else:
                    conditions = replace(product.conditions, snapshot=snapshot)
                    if row['kind'] == 'digest':
                        self._commit_digest(conditions, data, result, records[0], digest_base)
                    else:
                        self._commit_core(conditions, data, result, records, merged)
            except (AccountNotFound, SpaceNotFound, CommitOutcomeUnknown):
                return True  # Deletion or the next claim resolves the durable outcome.
            except Exception as exc:
                code, retryable = _failure(exc, phase)
                try:
                    self.tasks.fail(lease, code, retryable)
                except (AccountNotFound, SpaceNotFound, VersionConflict):
                    pass
        return True

    def flush(self, *, session_id=None, operation_ids=None, timeout=30, retry_failed=False, include_digests=False):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Expected a finite nonnegative timeout')
        selected = self.tasks.freeze(list(dict.fromkeys(operation_ids)) if operation_ids is not None else None,
                                     session_id, include_digests)
        retried = set()
        deadline = time.monotonic() + timeout
        executor = ThreadPoolExecutor(max_workers=1)
        future = None
        try:
            while True:
                results = [self.tasks.operation(operation_id) for operation_id in selected]
                if include_digests:
                    children = [child for result in results if result for child in result.auxiliary_operation_ids
                                if child not in selected]
                    if children:
                        selected.extend(dict.fromkeys(children))
                        continue
                if retry_failed:
                    for index, result in enumerate(results):
                        if result and result.state.value in ('failed', 'retry_wait') and result.operation_id not in retried:
                            retried.add(result.operation_id)
                            results[index] = self.tasks.retry(result.operation_id)
                active = [result.operation_id for result in results if result and result.state.value in ACTIVE]
                if not active:
                    status = 'completed' if all(result and result.core_committed for result in results) else 'failed'
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = 'timed_out'
                    break
                if future is None or future.done():
                    if future is not None:
                        future.result()
                    future = executor.submit(self._drive, active)
                time.sleep(min(remaining, .05))
            return dict(ok=status == 'completed', status=status, operation_ids=tuple(selected),
                        operations=tuple(result for result in results if result is not None))
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _drive(self, active):
        for operation_id in self.tasks.execution_order(active):
            if self.run_one(operation_id, force=True):
                break


def _failure(exc, phase):
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
