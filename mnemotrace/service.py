"""Product adapters for one authorized PostgreSQL memory space."""
from dataclasses import asdict, replace
import json
import math
import os

from .audit import graph_audit
from .config import Config
from .consolidate.outcomes import OUTCOME_PREDICATES, split_outcome_text
from .consolidate.policy import POLICY_DEFAULTS
from .llm import providers
from .memory import Memory
from .metrics import Metrics, timed
from .store.contracts import FactQuery, FactState, MessageInput
from .store.postgres import Database, DatabaseConfig, Spaces
from .store.postgres.records import CONFLICT, ENTITY, FACT, RELATION, SOURCE, domain
from .util import fmt_date, fmt_datetime, now


class MemoryRevisionChanged(ValueError):
    """A later page belongs to a different committed collection."""


# 为 /v1/recall 的 answerer（控制台的"问答"视图）准备拼装好的记忆框架。
_ANSWER_SYSTEM = (
    "你是用户的记忆助手。根据提供的【记忆】回答问题，简洁、准确、自然。"
    "准确区分人物及其关系归属；不同称呼只有明确的身份依据时，才可视为同一人。"
    "回答前核对记忆中的主语及关系是否对应问题；缺少对应依据时说明信息不足，不借用其他人物的信息。"
    "原始记录时间表示内容被记录的时间，不等同于事件发生时间。"
    "根据原文判断事件时间；相对时间以对应的原始记录时间为参照推算，保留原文的时间精度。"
    "信息发生更新时，按问题所指的时间采用对应版本。"
    "只回答证据支持的内容；信息不足或有歧义时明确说明，不猜测。"
)


def _answer_from_memory(answerer, query: str, ctx: str) -> tuple[str, str]:
    if answerer is None:
        return "", "model_not_configured"
    if not ctx.strip():
        return "", "no_evidence"
    from .llm.base import complete_result
    try:
        result = complete_result(answerer, f"【记忆】\n{ctx}\n\n【问题】{query}", system=_ANSWER_SYSTEM)
        if result.finish_reason not in {None, "stop", "end_turn"}:
            return "", "model_failed"
        answer = result.text.strip()
    except Exception:  # noqa: BLE001
        return "", "model_failed"
    return answer, "answered" if answer else "empty"


def _est_tokens(text: str) -> int:
    """Display estimate, mirrored by Postgres for the full-context total."""
    return (len(text.encode('utf-8')) + 3) // 4


class ProcessingFailure(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__('Core processing is incomplete')

    def detail(self):
        operations = self.result['operations']
        code = ('processing_incomplete' if self.result['status'] == 'timed_out' else
                next((item['error_code'] for item in operations if item['error_code']), 'extraction_failed'))
        sources = self.result.get('source_ids', ())
        return dict(code=code, message=str(self), stored_raw=bool(sources), episode_ids=sources,
                    status=self.result['status'], operation_ids=self.result['operation_ids'], operations=operations)


def operation_payload(result):
    return {**result, 'operations': [operation.to_dict() for operation in result['operations']]}


def _page(items, total, offset, limit):
    following = offset + limit if limit and offset + limit < total else None
    return dict(items=items, total=total, offset=offset, limit=limit,
                has_more=following is not None, next_offset=following)


def _bounds(limit, offset=0, *, minimum=1):
    return min(500, max(minimum, 100 if limit is None else limit)), max(0, offset)


def _recall_conditions(as_of, state, valid_at, redact_sensitive):
    if as_of is not None:
        if valid_at is not None:
            raise ValueError('Use either as_of or explicit time conditions')
        valid_at = as_of
    return FactQuery(state=state, valid_at=valid_at, exclude_sensitive=redact_sensitive)


def _fact_view(fact, at):
    row = asdict(fact)
    for key in ('embedding', 'user_id'):
        row.pop(key, None)
    live = fact.is_published() and fact.is_valid_at(at)
    row.update(valid_at=fmt_datetime(fact.valid_at),
               invalid_at=fmt_datetime(fact.invalid_at) if fact.invalid_at is not None else None,
               status='live' if live else 'superseded',
               why=split_outcome_text(fact.text)[1] if fact.predicate.lower() in OUTCOME_PREDICATES else '')
    return row


def _source_view(record):
    source = domain(record)
    return dict(id=source.id, content=source.content, chars=len(source.content), digest=source.digest,
                date=source.metadata.get('date') or fmt_date(source.event_time),
                session=source.session_id, session_id=source.session_id, role=source.role,
                message_id=source.message_id, sequence=source.sequence, event_time=source.event_time,
                ingested_at=source.ingested_at, coverage=source.coverage, agent_id=record.agent_id,
                space_id=record.ref.context.space_id)


class MemoryService:
    def __init__(self, database, *, embedder=None, embedding_model=None, llm=None,
                 answerer=None, captioner=None, config=None):
        self.database = database
        self.spaces = Spaces(database)
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.llm = llm
        self.answerer = answerer if answerer is not None else llm
        self.captioner = captioner or llm
        self.config = config or Config()
        self.metrics = Metrics()

    @classmethod
    def from_env(cls, *, embedder_name=None, llm_name=None):
        providers.load_dotenv()
        models = {}
        for field, variable in (('llm', 'LLM'), ('answerer', 'ANSWERER'), ('captioner', 'VISION_LLM')):
            if name := (llm_name if field == 'llm' and llm_name is not None
                        else os.environ.get(f'MNEMOTRACE_{variable}', '')).strip():
                models[field] = providers.make_llm(name)
        config = Config.from_env()
        embedder, model = providers.configured_embedder(embedder_name)
        service = cls(Database(DatabaseConfig.from_env()), embedder=embedder,
                      embedding_model=model, config=config, **models)
        service.database.open()
        return service

    def close(self):
        self.database.close()

    def memory(self, context):
        return Memory(self.database, context, config=self.config, llm=self.llm,
                              embedder=self.embedder, embedding_model=self.embedding_model)

    @timed('remember_messages')
    def remember_messages(self, context, messages, session_id, processing='buffered',
                          extract: bool = True, *, agent_id=None, timeout=30, source_metadata=None):
        items = tuple(MessageInput(**message, agent_id=agent_id, extract=extract) for message in messages)
        result = operation_payload(self.memory(context).remember_messages(
            session_id, items, processing=processing, timeout=timeout, source_metadata=source_metadata))
        if processing == 'immediate' and not result['ok']:
            raise ProcessingFailure(result)
        return result

    @timed('remember')
    def remember(self, context, content, session_id, extract: bool = True, processing='immediate', *,
                 agent_id=None, timeout=30):
        memory = self.memory(context)
        result = operation_payload(memory.remember(content, session_id, extract=extract, processing=processing,
                                                   agent_id=agent_id, timeout=timeout))
        if processing == 'immediate' and not result['ok']:
            raise ProcessingFailure(result)
        return result

    def operation(self, context, operation_id, *, retry=False):
        memory = self.memory(context)
        result = memory.retry_operation(operation_id) if retry else memory.operation(operation_id)
        return result.to_dict() if result is not None else None

    def processing_status(self, context, session_id=None, *, after_id=None, limit=100):
        operations = self.memory(context).list_operations(session_id=session_id, after_id=after_id, limit=limit)
        return dict(operations=[operation.to_dict() for operation in operations],
                    next_after_id=operations[-1].operation_id if len(operations) == limit else None)

    def flush(self, context, session_id=None, timeout=30, retry_failed=False, include_digests=False):
        return operation_payload(self.memory(context).flush_processing(
            session_id, timeout, retry_failed=retry_failed, include_digests=include_digests))


    @timed('recall')
    def recall(self, context, query, lean=True, n_digests=20, as_of=None,
               redact_sensitive=False, answer=False, state=FactState.CURRENT, valid_at=None,
               subject=None, predicate=None):
        memory = self.memory(context)
        output = dict(space_id=context.space_id, as_of=as_of, redacted_sensitive=redact_sensitive)
        facts = _recall_conditions(as_of, state, valid_at, redact_sensitive)
        if subject is not None or predicate is not None:
            facts = replace(facts, subject=subject, predicate=predicate)
        if answer and not lean:
            raise ValueError("Explicit model answers require lean context")
        if not lean:
            result = memory.search(query, facts=facts)
            return dict(**output, answer=result.answer() if result.status == 'answered' else '',
                        status=result.status, facts=[fact.text for fact in result.facts],
                        fact_ids=[fact.id for fact in result.facts], source_ids=result.source_ids,
                        sources=[dict(id=ep.id, session_id=ep.session_id, text=ep.text,
                                      representation=ep.representation) for ep in result.sources],
                        limitations=result.limitations)
        result = memory.lean_context(query, n_digests=n_digests,
                                     facts=facts, reinforce_hits=True, include_full_tokens=answer)
        tokens = _est_tokens(result.context)
        output.update(context=result.context, context_sections=[asdict(section) for section in result.sections],
                      status="evidence_only" if result.sections else "no_evidence",
                      tokens_est=tokens, limitations=result.limitations,
                      coverage_by_space=result.coverage_by_space)
        if answer:
            output['answer'], output['answer_status'] = _answer_from_memory(self.answerer, query, result.context)
            if output['answer_status'] == 'answered':
                output['status'] = 'answered'
            if result.full_tokens is not None:
                output['full_tokens'] = result.full_tokens
        self.metrics.tokens(tokens, output.get('full_tokens'))
        return output

    def recall_multi(self, contexts, query, *, n_digests=20, as_of=None, redact_sensitive=False,
                     state=FactState.CURRENT, valid_at=None):
        ids = [context.space_id for context in contexts]
        result = self.memory(contexts[0]).recall_multi(ids, query, n_digests=n_digests,
                                                       facts=_recall_conditions(as_of, state, valid_at, redact_sensitive))
        return dict(context=result.context, context_sections=[asdict(section) for section in result.sections],
                    status="evidence_only" if result.sections else "no_evidence",
                    tokens_est=_est_tokens(result.context), space_ids=ids,
                    as_of=as_of, redacted_sensitive=redact_sensitive, limitations=result.limitations,
                    coverage_by_space=result.coverage_by_space)

    def aggregate_facts(self, context, predicate, *, operation='count', subject=None, start=None,
                        end=None, query=None, state=FactState.CURRENT, valid_at=None,
                        redact_sensitive=False, limit=10):
        scope = FactQuery(state=state, valid_at=valid_at,
                          exclude_sensitive=redact_sensitive, limit=limit)
        result = self.memory(context).aggregate_facts(
            predicate, operation=operation, subject=subject, start=start, end=end, query=query, facts=scope)
        at = now() if valid_at is None else valid_at
        return dict(count=result.count, facts=[_fact_view(fact, at) for fact in result.facts],
                    candidates=[dict(id=ep.id, session_id=ep.session_id, text=ep.text,
                                     event_time=ep.event_time, role=ep.role,
                                     representation=ep.representation) for ep in result.candidates],
                    limitations=result.limitations)

    def follow_path(self, context, anchor, predicates, *, state=FactState.CURRENT, valid_at=None,
                    redact_sensitive=False, limit=20):
        scope = FactQuery(state=state, valid_at=valid_at,
                          exclude_sensitive=redact_sensitive, limit=limit)
        result = self.memory(context).follow_path(anchor, predicates, facts=scope)
        at = now() if valid_at is None else valid_at
        return dict(paths=[dict(endpoint=path.endpoint, facts=[_fact_view(fact, at) for fact in path.facts])
                           for path in result.paths], limitations=result.limitations)

    def get_episode(self, context, episode_id):
        with self.memory(context).dao.read_snapshot() as view:
            record = view.get(SOURCE, episode_id)
        return _source_view(record) if record is not None else None

    def set_fact_sensitive(self, context, fact_id, sensitive):
        fact = self.memory(context).set_fact_sensitive(fact_id, sensitive)
        return dict(ok=True, id=fact.id, sensitive=fact.sensitive) if fact else None

    def delete_fact(self, context, fact_id):
        return dict(ok=self.memory(context).delete_fact(fact_id))

    def clear_slot(self, context, subject, predicate, expect_count):
        result = self.memory(context).clear_slot(subject, predicate, expect_count=expect_count)
        return dict(ok=True, deleted=result.affected_count, subject=subject, predicate=predicate)

    def forget(self, context):
        self.memory(context).clear_space()
        return dict(ok=True, space_id=context.space_id)

    def get_focus(self, context):
        settings = self.memory(context).get_settings()
        return dict(track=list(settings.track), mute=list(settings.mute))

    def set_focus(self, context, **fields):
        settings = self.memory(context).set_settings(**{k: tuple(v) for k, v in fields.items() if v is not None})
        return dict(ok=True, focus=dict(track=list(settings.track), mute=list(settings.mute)))

    def get_policy(self, context):
        settings = self.memory(context).get_settings()
        return dict(policy={key: getattr(settings, key) for key in POLICY_DEFAULTS}, defaults=POLICY_DEFAULTS)

    def set_policy(self, context, **fields):
        self.memory(context).set_settings(**{k: v for k, v in fields.items() if v is not None})
        return dict(ok=True, **self.get_policy(context))


    def conflicts(self, context):
        return dict(conflicts=[asdict(c) for c in self.memory(context).conflicts()])

    def resolve_conflict(self, context, conflict_id, keep):
        return dict(ok=self.memory(context).resolve_conflict(conflict_id, keep=keep))

    def profile(self, context):
        return self.memory(context).profile()

    def refresh_profile(self, context):
        return self.memory(context).refresh_profile().to_dict()

    def sessions(self, context, limit=None, offset=0, q=''):
        limit, offset = _bounds(limit, offset)
        with self.memory(context).dao.read_snapshot() as view:
            rows, total = view.session_index(text=q.strip(), limit=limit, offset=offset)
            for row in rows:
                row.update(first_event_at_h=fmt_datetime(row['first_event_at']),
                           last_event_at_h=fmt_datetime(row['last_event_at']))
        page = _page(rows, total, offset, limit)
        return dict(ok=True, space_id=context.space_id, sessions=rows, page=page, next_offset=page['next_offset'])

    def session_report(self, context, session_id, include_sensitive=False, *, limit=100, offset=0):
        limit, offset = _bounds(limit, offset)
        with self.memory(context).dao.read_snapshot(session_id) as view:
            rows, _ = view.session_index(session_id=session_id, limit=1)
            records, total = view.browse_facts(FactQuery(state=FactState.ALL, limit=limit),
                                               session_id=session_id, offset=offset,
                                               hide_sensitive_predecessors=not include_sensitive)
            facts = [_fact_view(domain(record), view.at.timestamp()) for record in records]
            sources = view.session_source_ids(session_id, {source for row in facts for source in row['provenance']})
        for row in facts:
            row['provenance'] = [source for source in row['provenance'] if source in sources]
            row['redacted'] = row['sensitive'] and not include_sensitive
            if row['redacted']:
                for key in ('subject', 'predicate', 'object', 'why', 'categories', 'categories_source',
                            'subject_id', 'object_id', 'subject_type', 'object_type', 'relation_id',
                            'supersedes'):
                    row.pop(key, None)
                row['text'] = '[redacted sensitive fact]'
        report = rows[0] if rows else dict(episodes=0, episodes_consolidated=0, episodes_pending=0,
                                          extraction_failures=0, )
        redacted = 0 if include_sensitive else report.get('facts_sensitive', 0)
        page = _page(facts, total, offset, limit)
        return {**report, 'ok': True, 'space_id': context.space_id, 'session_id': session_id,
                'include_sensitive': include_sensitive, 'facts_added': total,
                'facts_redacted': redacted, 'facts': facts, 'facts_page': page}

    def memories(self, context, facts_limit=None, facts_offset=0, episodes_limit=None, episodes_offset=0,
                 status=None, kind=None, q='', include_sensitive=False, categories=None, outcome_kind=None,
                 expected_revision=None, evaluated_at=None):
        facts_limit, facts_offset = _bounds(facts_limit, facts_offset, minimum=0)
        episodes_limit, episodes_offset = _bounds(episodes_limit, episodes_offset, minimum=0)
        if (expected_revision is None) != (evaluated_at is None):
            raise ValueError('Expected both revision and evaluation time')
        if evaluated_at is not None and (not math.isfinite(evaluated_at) or evaluated_at < 0):
            raise ValueError('Invalid evaluation time')
        if outcome_kind is not None and outcome_kind not in OUTCOME_PREDICATES:
            raise ValueError('Unknown outcome kind')
        state = {'live': FactState.CURRENT, 'current': FactState.CURRENT, 'superseded': FactState.HISTORY,
                 'old': FactState.HISTORY, 'history': FactState.HISTORY}.get(status, FactState.ALL)
        with self.memory(context).dao.read_snapshot() as view:
            revision = str(view.snapshot.core_revision)
            if expected_revision is not None and expected_revision != revision:
                raise MemoryRevisionChanged('Records changed; reload the current page')
            at = view.at.timestamp() if evaluated_at is None else evaluated_at
            request = FactQuery(state=state, predicate=outcome_kind,
                                categories=tuple(categories or ()),
                                limit=max(1, facts_limit), exclude_sensitive=not include_sensitive)
            rows, fact_total = view.browse_facts(request, text=q.strip(), offset=facts_offset,
                                                outcomes={'outcomes': True, 'attributes': False}.get(kind),
                                                count_only=facts_limit == 0, evaluation_at=at)
            categories = view.fact_categories(include_sensitive=include_sensitive,
                                              outcomes={'outcomes': True, 'attributes': False}.get(kind))
            facts = [_fact_view(domain(row), at) for row in rows]
            episodes, episode_total = [], 0
            if include_sensitive:
                sources, episode_total = view.browse_sources(limit=episodes_limit, offset=episodes_offset)
                episodes = [_source_view(row) for row in sources]
            counts = view.overview(at)
        fact_page = _page(facts, fact_total, facts_offset, facts_limit)
        source_page = _page(episodes, episode_total, episodes_offset, episodes_limit)
        return dict(space_id=context.space_id, data_revision=revision, evaluated_at=at,
                    counts=counts,
                    facts=facts, fact_categories=categories,
                    episodes=episodes, facts_page=fact_page, episodes_page=source_page,
                    next_offsets=dict(facts=fact_page['next_offset'], episodes=source_page['next_offset']))

    def graph(self, context, as_of=None, include_sensitive=False, q='', live_only=False, limit=None):
        limit, _ = _bounds(limit)
        query = FactQuery(state=FactState.CURRENT if live_only or as_of is not None else FactState.ALL,
                          valid_at=as_of, exclude_sensitive=not include_sensitive, limit=limit + 1)
        with self.memory(context).dao.read_snapshot() as view:
            nodes, edges = view.graph_page(query, text=q.strip())
            at = view.at.timestamp() if as_of is None else as_of
            relations = tuple(map(domain, edges[:limit]))
            facts = {row.ref.record_id: domain(row) for row in view.facts_by_ids(
                [relation.fact_id for relation in relations], query)}
        ids = {value for relation in relations for value in (relation.subject_id, relation.object_id)}
        return dict(nodes=[dict(id=e.id, name=e.name, type=e.type) for e in map(domain, nodes) if e.id in ids],
                    edges=[dict(source=r.subject_id, target=r.object_id, predicate=r.predicate, fact_id=r.fact_id,
                                live=facts[r.fact_id].is_valid_at(at), fact_text=facts[r.fact_id].text,
                                provenance=facts[r.fact_id].provenance, valid_at=r.valid_at, invalid_at=r.invalid_at,
                                valid_at_h=fmt_datetime(r.valid_at),
                                invalid_at_h=fmt_datetime(r.invalid_at) if r.invalid_at is not None else None)
                           for r in relations], has_more=len(edges) > limit)

    def stats(self, context):
        with self.memory(context).dao.read_snapshot() as view:
            counts = view.overview(view.at.timestamp())
            fed_at, sessions = view.agent_feed()
            times = view.time_range()
        feed = dict(last_fed_at=fed_at, last_fed_at_h=fmt_datetime(fed_at) if fed_at is not None else None,
                    sessions=sessions, conclusions=0)
        return dict(space_id=context.space_id, counts=counts, storage='postgres', embedder=self.embedding_model or 'lexical',
                    feed=feed, time_range={**times, **{key + '_h': fmt_datetime(value) if value is not None else None
                                                       for key, value in times.items()}},
                    consolidation_backlog=counts['episodes_pending'] > 0,
                    llm_configured=self.llm is not None, answerer_configured=self.answerer is not None)

    def agent_status(self, context, session_id=None):
        session = dict(id=session_id, episodes=0, episodes_pending=0, extraction_failures=0, )
        if session_id is not None:
            with self.memory(context).dao.read_snapshot(session_id) as view:
                rows, _ = view.session_index(session_id=session_id, limit=1)
                if rows:
                    session.update(rows[0])
        return dict(ok=True, **self.stats(context), session_id=session_id, focus=self.get_focus(context),
                    session=session, mode='content_free_agent_status',
                    processing=self.processing_status(context, session_id),
                    recommended_next_actions=[
                        'Call mnemotrace_recall before relying on past memory.',
                        'Call mnemotrace_flush_processing to finish core processing.',
                    ],
                    tools=dict(read_context='mnemotrace_recall', write_memory='mnemotrace_remember_messages',
                               flush_processing='mnemotrace_flush_processing', inspect_facts='mnemotrace_list_facts',
                               set_fact_sensitive='mnemotrace_set_fact_sensitive', delete_fact='mnemotrace_delete_fact',
                               focus='mnemotrace_get_focus'))

    def audit(self, context, limit=40):
        with self.memory(context).dao.read_snapshot() as view:
            return graph_audit(view, limit)

    def metrics_snapshot(self):
        return self.metrics.snapshot()

    def ready(self):
        with self.database._transaction(read_only=True) as conn:
            conn.execute('SELECT 1')

    def iter_export(self, context, include_sensitive=False):
        from .store._validation import SNAPSHOT_FORMAT_VERSION

        memory = self.memory(context)
        with memory.dao.read_snapshot() as view:
            at = view.at.timestamp()
            settings = view.settings()
            head = dict(format_version=SNAPSHOT_FORMAT_VERSION, space_id=context.space_id, include_sensitive=include_sensitive,
                        redacted_sensitive=not include_sensitive, focus=dict(track=settings.track, mute=settings.mute))
            yield json.dumps(head, ensure_ascii=False)[:-1]
            for name, kind in (('facts', FACT), ('episodes', SOURCE)):
                yield f',"{name}":['
                cursor, first = None, True
                while kind != SOURCE or include_sensitive:
                    batch = (view.portable_facts(after_id=cursor) if kind == FACT and include_sensitive
                             else view.records(kind, after_id=cursor,
                                               hide_sensitive_predecessors=not include_sensitive))
                    if not batch:
                        break
                    for record in batch:
                        if kind == FACT:
                            item = domain(record)
                            if not include_sensitive and (item.sensitive or not item.is_published()):
                                continue
                            payload = asdict(item)
                            payload.pop('embedding', None)
                            payload.pop('user_id', None)
                            payload.update(status='live' if item.is_published() and item.is_valid_at(at) else 'superseded',
                                           valid_at_h=fmt_date(item.valid_at),
                                           invalid_at_h=fmt_date(item.invalid_at) if item.invalid_at is not None else None)
                        else:
                            item = domain(record)
                            payload = _source_view(record)
                            payload.update(digest_coverage=item.digest_coverage, digest_written_at=item.digest_written_at)
                        yield ('' if first else ',') + json.dumps(payload, ensure_ascii=False)
                        first = False
                    cursor = batch[-1].ref.record_id
                yield ']'
            if include_sensitive:
                yield ',"conflicts":['
                cursor, first = None, True
                while batch := view.records(CONFLICT, after_id=cursor):
                    for record in batch:
                        payload = asdict(domain(record))
                        payload.pop('user_id')
                        yield ('' if first else ',') + json.dumps(payload, ensure_ascii=False)
                        first = False
                    cursor = batch[-1].ref.record_id
                yield ']'
            yield ',"graph":{'
            for name, kind in (('nodes', ENTITY), ('edges', RELATION)):
                yield ('"' if kind == ENTITY else ',"') + name + '":['
                cursor, first = None, True
                while batch := view.export_graph(kind, include_sensitive=include_sensitive, after_id=cursor):
                    for item in map(domain, batch):
                        if kind == ENTITY:
                            payload = dict(id=item.id, name=item.name, type=item.type)
                        else:
                            payload = dict(source=item.subject_id, target=item.object_id, predicate=item.predicate,
                                           fact_id=item.fact_id, valid_at_h=fmt_date(item.valid_at),
                                           invalid_at_h=fmt_date(item.invalid_at) if item.invalid_at is not None else None,
                                           live=(item.valid_at is None or item.valid_at <= at)
                                           and (item.invalid_at is None or item.invalid_at > at))
                        yield ('' if first else ',') + json.dumps(payload, ensure_ascii=False)
                        first = False
                    cursor = batch[-1].ref.record_id
                yield ']'
            yield '}}'

    def export(self, context, include_sensitive=False):
        return json.loads(''.join(self.iter_export(context, include_sensitive)))
