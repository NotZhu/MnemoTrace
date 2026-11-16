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
from .llm.execution import ModelMeter
from .memory import Memory
from .metrics import Metrics, timed
from .store.contracts import FactQuery, FactState
from .store.postgres import Database, DatabaseConfig, Spaces
from .store.postgres.records import ENTITY, FACT, RELATION, SOURCE, domain
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
        task = self.result['task']
        return dict(code=(task and task['error_code']) or 'extraction_failed', message=str(self),
                    stored_raw=self.result['stored_raw'], source_ids=self.result['source_ids'],
                    episode_ids=self.result['source_ids'],
                    status='failed', operation_id=self.result['operation_id'], task=task)


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
    for key in ('embedding', 'user_id', 'source', 'last_access', 'access_count'):
        row.pop(key, None)
    live = fact.is_valid_at(at)
    row.update(valid_at=fmt_datetime(fact.valid_at),
               invalid_at=fmt_datetime(fact.invalid_at) if fact.invalid_at is not None else None,
               status='live' if live else 'superseded',
               why=split_outcome_text(fact.text)[1] if fact.predicate.lower() in OUTCOME_PREDICATES else '')
    return row


def _source_view(record):
    source = domain(record)
    return dict(id=source.id, content=source.content, chars=len(source.content), digest=source.digest,
                date=fmt_date(source.event_time),
                session_id=source.session_id, role=source.role,
                message_id=source.message_id, sequence=source.sequence, event_time=source.event_time,
                ingested_at=source.ingested_at, coverage=source.coverage, digest_coverage=source.digest_coverage,
                agent_id=record.agent_id,
                space_id=record.ref.context.space_id)


class MemoryService:
    def __init__(self, database, *, embedder=None, embedding_model=None, llm=None,
                 answerer=None, captioner=None, extraction_llm=None, config=None):
        self.database = database
        self.spaces = Spaces(database)
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.llm = llm
        self.extraction_llm = extraction_llm
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
                if field == 'llm':
                    models['extraction_llm'] = providers.make_extraction_llm(name, require_context=True)
        config = Config()
        embedder, model = providers.configured_embedder(embedder_name)
        service = cls(Database(DatabaseConfig.from_env()), embedder=embedder,
                      embedding_model=model, config=config, **models)
        service.database.open()
        return service

    def close(self):
        self.database.close()

    def memory(self, context):
        extraction = self.extraction_llm if self.extraction_llm is not None else self.llm
        model = ModelMeter(extraction, self.metrics) if extraction is not None else None
        return Memory(self.database, context, config=self.config, llm=model,
                              embedder=self.embedder, embedding_model=self.embedding_model)

    @timed('remember')
    def remember(self, context, value, session_id, *, wait=False, timeout=30,
                 message_id=None, agent_id=None, event_time=None):
        result = self.memory(context).remember(value, session_id, wait=wait, timeout=timeout,
                                               message_id=message_id, agent_id=agent_id, event_time=event_time)
        task = result['task']
        result['task'] = task.to_dict() if task is not None else None
        if wait and not result['ok']:
            raise ProcessingFailure(result)
        return result

    def _store_originals(self, context, messages, session_id, *, agent_id=None):
        return self.memory(context)._store_originals(messages, session_id, agent_id=agent_id)

    def operation(self, context, operation_id, *, retry=False):
        memory = self.memory(context)
        result = memory.retry_operation(operation_id) if retry else memory.operation(operation_id)
        if result is None:
            return None
        payload = result.to_dict()
        if result.kind.value == 'extract':
            payload['source_ids'] = memory._processing.tasks.input_ids(operation_id)
            if result.core_committed:
                receipt = memory.dao.receipt(operation_id)
                payload['fact_ids'] = receipt.fact_ids if receipt is not None else ()
        return payload

    def processing_status(self, context, session_id=None, *, after_id=None, limit=100):
        memory = self.memory(context)
        operations = memory.list_operations(session_id=session_id, after_id=after_id, limit=limit)
        return dict(operations=[operation.to_dict() for operation in operations],
                    next_after_id=operations[-1].operation_id if len(operations) == limit else None)


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
        return dict(ok=True, space_id=context.space_id, sessions=rows, sessions_page=page)

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
                for key in ('subject', 'predicate', 'object', 'why', 'categories',
                            'subject_id', 'object_id', 'subject_type', 'object_type', 'relation_id',
                            'supersedes'):
                    row.pop(key, None)
                row['text'] = '[redacted sensitive fact]'
        report = rows[0] if rows else dict(episodes=0, episodes_consolidated=0, episodes_pending=0,
                                          extraction_failures=0)
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
                    episodes=episodes, facts_page=fact_page, episodes_page=source_page)

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
        names = {}
        if not include_sensitive:
            for relation in relations:
                fact = facts[relation.fact_id]
                names.setdefault(relation.subject_id, fact.subject)
                names.setdefault(relation.object_id, fact.object)
        return dict(nodes=[dict(id=e.id, name=names.get(e.id, e.name), type=e.type)
                           for e in map(domain, nodes) if e.id in ids],
                    edges=[dict(source=r.subject_id, target=r.object_id, predicate=r.predicate, fact_id=r.fact_id,
                                live=facts[r.fact_id].is_valid_at(at), fact_text=facts[r.fact_id].text,
                                provenance=facts[r.fact_id].provenance, valid_at=r.valid_at, invalid_at=r.invalid_at,
                                valid_at_h=fmt_datetime(r.valid_at),
                                invalid_at_h=fmt_datetime(r.invalid_at) if r.invalid_at is not None else None)
                           for r in relations], has_more=len(edges) > limit)

    def stats(self, context):
        with self.memory(context).dao.read_snapshot() as view:
            counts = view.overview(view.at.timestamp())
            times = view.time_range()
        return dict(space_id=context.space_id, counts=counts, storage='postgres', embedder=self.embedding_model or 'lexical',
                    time_range={**times, **{key + '_h': fmt_datetime(value) if value is not None else None
                                                       for key, value in times.items()}},
                    consolidation_backlog=counts['episodes_pending'] > 0,
                    llm_configured=self.llm is not None, answerer_configured=self.answerer is not None)

    def agent_status(self, context, session_id=None):
        session_counts = dict(episodes=0, episodes_pending=0, extraction_failures=0)
        if session_id is not None:
            with self.memory(context).dao.read_snapshot(session_id) as view:
                rows, _ = view.session_index(session_id=session_id, limit=1)
                if rows:
                    session_counts.update({key: rows[0][key] for key in session_counts})
        return dict(ok=True, **self.stats(context), session_id=session_id, focus=self.get_focus(context),
                    session_counts=session_counts, mode='content_free_agent_status',
                    processing=self.processing_status(context, session_id),
                    recommended_next_actions=[
                        'Call mnemotrace_recall before relying on past memory.',
                        'Pass wait=true or check the returned task for processing results.',
                    ],
                    tools=dict(read_context='mnemotrace_recall', write_memory='mnemotrace_remember',
                               get_operation='mnemotrace_get_operation', inspect_facts='mnemotrace_list_facts',
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
        memory = self.memory(context)
        with memory.dao.read_snapshot() as view:
            at = view.at.timestamp()
            settings = view.settings()
            head = dict(format_version=7, space_id=context.space_id, include_sensitive=include_sensitive,
                        redacted_sensitive=not include_sensitive, focus=dict(track=settings.track, mute=settings.mute))
            yield json.dumps(head, ensure_ascii=False)[:-1]
            for name, kind in (('facts', FACT), ('episodes', SOURCE)):
                yield f',"{name}":['
                cursor, first = None, True
                while kind != SOURCE or include_sensitive:
                    batch = view.records(kind, after_id=cursor, hide_sensitive_predecessors=not include_sensitive)
                    if not batch:
                        break
                    for record in batch:
                        if kind == FACT:
                            item = domain(record)
                            if not include_sensitive and item.sensitive:
                                continue
                            payload = dict(id=item.id, subject=item.subject, predicate=item.predicate,
                                           object=item.object, text=item.text, relation_id=item.relation_id,
                                           subject_id=item.subject_id, object_id=item.object_id,
                                           subject_type=item.subject_type, object_type=item.object_type,
                                           categories=item.categories, sensitive=item.sensitive, retention=item.retention,
                                           salience=item.salience, valid_at=item.valid_at, invalid_at=item.invalid_at,
                                           created_at=item.created_at, expired_at=item.expired_at,
                                           supersedes=item.supersedes, provenance=item.provenance)
                            status = ('scheduled' if item.valid_at is not None and item.valid_at > at else
                                      'live' if item.is_valid_at(at) else 'superseded')
                            payload.update(status=status,
                                           valid_at_h=fmt_date(item.valid_at),
                                           invalid_at_h=fmt_date(item.invalid_at) if item.invalid_at is not None else None)
                        else:
                            item = domain(record)
                            payload = _source_view(record)
                            payload.update(digest_written_at=item.digest_written_at,
                                           extract=item.extract)
                        yield ('' if first else ',') + json.dumps(payload, ensure_ascii=False)
                        first = False
                    cursor = batch[-1].ref.record_id
                yield ']'
            yield ',"graph":{'
            for name, kind in (('nodes', ENTITY), ('edges', RELATION)):
                yield ('"' if kind == ENTITY else ',"') + name + '":['
                cursor, first = None, True
                while batch := view.export_graph(kind, include_sensitive=include_sensitive, after_id=cursor):
                    public_names = (view.public_entity_names([row.ref.record_id for row in batch])
                                    if kind == ENTITY and not include_sensitive else {})
                    for item in map(domain, batch):
                        if kind == ENTITY:
                            name = item.name if include_sensitive else public_names.get(item.id, '')
                            payload = dict(id=item.id, name=name, type=item.type)
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
