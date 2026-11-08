"""Product adapters for one authorized PostgreSQL memory space."""
from collections import Counter
from dataclasses import asdict
import json
import math
import os

from .audit import AUDIT_KIND_PRIORITY, EmbedderCoverage, fact_finding, finding
from .config import Config
from .connectors.documents import document_inputs
from .consolidate.outcomes import OUTCOME_PREDICATES, split_outcome_text
from .consolidate.policy import POLICY_DEFAULTS
from .consolidate.structured import _BASIC, build_structured_profile, user_aliases
from .embed import HashingEmbedder
from .extraction import LLMNotConfiguredError, require_llm
from .llm import providers
from .memory import Memory
from .metrics import Metrics, timed
from .store.contracts import FactQuery, FactState, MessageInput
from .store.postgres import Database, DatabaseConfig, Spaces
from .store.postgres.errors import MessageConflict
from .store.postgres.records import CONFLICT, ENTITY, FACT, RELATION, SOURCE, domain
from .util import fmt_date, fmt_datetime


class MemoryRevisionChanged(ValueError):
    """A later page belongs to a different committed collection."""


# _BASIC 归一化字段中允许多值共存的那些。audit 的 slot_overflow 预扫描（"单值槽位不该有 N 个值"）
# 只对其余字段成立。
_MULTI_VALUED_FIELDS = {"children", "language", "education"}

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


def _answer_from_memory(answerer, query: str, ctx: str) -> str:
    """基于拼装好的精简上下文生成回答——即使用这套记忆的 agent 会说的话。
    尽力而为：缺少 answerer 或模型瞬时出错都不会打断 recall（返回 ""）。"""
    if answerer is None or not ctx.strip():
        return ""
    try:
        return answerer.complete(f"【记忆】\n{ctx}\n\n【问题】{query}", system=_ANSWER_SYSTEM).strip()
    except Exception:  # noqa: BLE001 -- 绝不让回答打断 recall
        return ""


def _est_tokens(text: str) -> int:
    """对中文更公平的 token 估算：每个 CJK 字符 ≈ 1 token，每个非 CJK 单词 ≈ 1 token。
    （中文不按空格分词，`len(text.split())` 会严重低估。）"""
    import re

    cjk = len(re.findall(r"[一-鿿]", text))
    words = len(re.findall(r"[A-Za-z0-9]+", re.sub(r"[一-鿿]", " ", text)))
    return cjk + words


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


def _fact_view(fact, at):
    row = asdict(fact)
    for key in ('embedding', 'user_id'):
        row.pop(key, None)
    row.update(display=fact.display or fact.text, valid_at=fmt_datetime(fact.valid_at),
               invalid_at=fmt_datetime(fact.invalid_at) if fact.invalid_at is not None else None,
               status='live' if fact.is_live(at) else 'superseded',
               why=split_outcome_text(fact.text)[1] if fact.predicate.lower() in OUTCOME_PREDICATES else '')
    return row


def _source_view(record):
    source = domain(record)
    return dict(id=source.id, content=source.content, chars=len(source.content), summary=source.summary,
                date=source.metadata.get('date') or fmt_date(source.event_time),
                session=source.session_id, session_id=source.session_id, role=source.role, mode=source.mode,
                message_id=source.message_id, sequence=source.sequence, event_time=source.event_time,
                ingested_at=source.ingested_at, coverage=source.coverage, agent_id=record.agent_id,
                space_id=record.ref.context.space_id)


class MemoryService:
    def __init__(self, database, *, embedder=None, embedding_model='hashing-v1', llm=None,
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
        embedder = embedder_name or os.environ.get('MNEMOTRACE_EMBEDDER', 'hashing')
        models = {}
        for field, variable in (('llm', 'LLM'), ('answerer', 'ANSWERER'), ('captioner', 'VISION_LLM')):
            if name := (llm_name if field == 'llm' and llm_name is not None
                        else os.environ.get(f'MNEMOTRACE_{variable}', '')).strip():
                models[field] = providers.make_llm(name)
        config = Config.from_env()
        service = cls(Database(DatabaseConfig.from_env()), embedder=providers.make_embedder(embedder),
                      embedding_model='hashing-v1' if embedder == 'hashing' else embedder, config=config, **models)
        service.database.open()
        return service

    def close(self):
        self.database.close()

    def memory(self, context):
        return Memory(self.database, context, config=self.config, llm=self.llm,
                              embedder=self.embedder, embedding_model=self.embedding_model)

    @timed('remember_messages')
    def remember_messages(self, context, messages, session_id, mode='live', processing='buffered',
                          scope='auto', *, agent_id=None, timeout=30, source_metadata=None):
        items = tuple(MessageInput(**message, agent_id=agent_id, mode=mode, scope=scope) for message in messages)
        result = operation_payload(self.memory(context).remember_messages(
            session_id, items, processing=processing, timeout=timeout, source_metadata=source_metadata))
        if processing == 'immediate' and not result['ok']:
            raise ProcessingFailure(result)
        return result

    @timed('remember')
    def remember(self, context, content, session_id, scope='auto', processing='immediate', *,
                 agent_id=None, timeout=30):
        memory = self.memory(context)
        if scope == 'raw':
            episode = memory.remember_raw(content, session_id, agent_id=agent_id)
            return dict(ok=True, status='stored', stored_raw=True, source_ids=[episode.id],
                        operation_ids=[], operations=[])
        result = operation_payload(memory.remember(content, session_id, scope=scope, processing=processing,
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

    def flush(self, context, session_id=None, timeout=30, retry_failed=False, include_summaries=False):
        return operation_payload(self.memory(context).flush_processing(
            session_id, timeout, retry_failed=retry_failed, include_summaries=include_summaries))

    def import_(self, context, *, sessions=None, data=None, format='auto', consolidate=None,
                summarize=True, dedupe=True, agent_id=None):
        from .connectors import parse
        from .connectors.dialogue import batches
        from .consolidate.pipeline import MessageConflictError

        sessions = sessions if sessions is not None else parse(data, format=format)
        memory = self.memory(context)
        results, failures, accepted_sessions = [], [], set()
        episodes = skipped = 0
        plans = []
        for index, session in enumerate(sessions):
            try:
                plans.extend(batches((session,), consolidate))
            except (ValueError, MessageConflictError) as exc:
                session_id = session.get('session_id') if isinstance(session, dict) else getattr(session, 'session_id', None)
                failures.append(dict(session_id=session_id if isinstance(session_id, str) else None,
                                     session_index=index, code=getattr(exc, 'code', 'invalid_parameter'),
                                     stored_raw=False, operation_ids=[]))
        for session_id, messages, source, extract in plans:
            try:
                if messages is None:
                    document, = document_inputs([source])
                    result = operation_payload(memory.import_document(
                        document['content'], session_id, event_time=document['event_time'],
                        metadata=document['metadata'], agent_id=agent_id, extract=consolidate is not False,
                        summarize=summarize, dedupe=dedupe,
                        processing='immediate' if consolidate is True else 'buffered'))
                else:
                    source_metadata = {'summary_requested': summarize}
                    metadata = source.get('metadata')
                    if isinstance(metadata, dict) and metadata.get('source') == 'agent_session':
                        source_metadata['source'] = 'agent_session'
                    result = self.remember_messages(
                        context, messages, session_id, mode='historical', agent_id=agent_id,
                        processing='immediate' if consolidate is True else 'buffered',
                        scope='auto' if extract else 'raw', source_metadata=source_metadata)
            except ProcessingFailure as exc:
                result = exc.result
            except (MessageConflict, LLMNotConfiguredError, ValueError) as exc:
                if isinstance(exc, LLMNotConfiguredError) and not results:
                    raise
                failures.append(dict(session_id=session_id, code=getattr(exc, 'code', 'invalid_parameter'),
                                     stored_raw=False, operation_ids=[]))
                continue
            results.append(dict(session_id=session_id, **result))
            added = result['accepted']
            episodes += added
            skipped += len(result['source_ids']) - added
            if added:
                accepted_sessions.add(session_id)
            if not result['ok']:
                failures.append(dict(session_id=session_id, code=result['status'], stored_raw=bool(result['source_ids']),
                                     operation_ids=result['operation_ids'], operations=result['operations']))
        return dict(ok=not failures, sessions=len(accepted_sessions), episodes=episodes, skipped=skipped,
                    failed=len(failures), failures=failures, results=results,
                    queued=any(result['operations'] and result['status'] == 'accepted' for result in results))

    def import_document(self, context, data, *, filename=None, content_type=None, session_id='document',
                        agent_id=None):
        from .connectors.documents import detect_kind, document_text, to_data_url, to_session
        from .llm.vision import caption_image

        require_llm(self.llm)
        kind = detect_kind(data, filename, content_type)
        if kind == 'image':
            caption = caption_image(self.captioner, to_data_url(data, content_type))
            text = f'[image] {caption}' if caption else '[image] (no caption configured)'
        else:
            text = document_text(data, kind)
        if not text:
            raise ValueError('Document has no extractable text')
        source = to_session(text, filename=filename, session_id=session_id,
                            metadata={'kind': kind, 'media_type': content_type or ''})
        return dict(kind=kind, **self.import_(context, sessions=[source], agent_id=agent_id))

    @timed('recall')
    def recall(self, context, query, lean=True, n_chunks=6, session_id=None, as_of=None,
               redact_sensitive=False, answer=False):
        memory = self.memory(context)
        output = dict(space_id=context.space_id, as_of=as_of, redacted_sensitive=redact_sensitive)
        facts = FactQuery(valid_at=as_of, exclude_sensitive=redact_sensitive)
        if not lean:
            result = memory.search(query, facts=facts)
            return dict(**output, answer=result.answer(), facts=[fact.text for fact in result.facts],
                        limitations=result.limitations)
        result = memory.lean_context(query, n_chunks=n_chunks, session_id=session_id,
                                     facts=facts, reinforce_hits=as_of is None)
        tokens = _est_tokens(result.context)
        output.update(context=result.context, tokens_est=tokens, limitations=result.limitations,
                      coverage_by_space=result.coverage_by_space)
        if answer:
            output['answer'] = _answer_from_memory(self.answerer, query, result.context)
            if not redact_sensitive:
                with memory.dao.read_snapshot() as view:
                    output['full_tokens'] = view.full_context_tokens(as_of)
        self.metrics.tokens(tokens, output.get('full_tokens'))
        return output

    def recall_multi(self, contexts, query, *, n_chunks=6, session_id=None, as_of=None, redact_sensitive=False):
        ids = [context.space_id for context in contexts]
        result = self.memory(contexts[0]).recall_multi(ids, query, n_chunks=n_chunks, session_id=session_id,
                                                       facts=FactQuery(valid_at=as_of, exclude_sensitive=redact_sensitive))
        return dict(context=result.context, tokens_est=_est_tokens(result.context), space_ids=ids,
                    as_of=as_of, redacted_sensitive=redact_sensitive, limitations=result.limitations,
                    coverage_by_space=result.coverage_by_space)

    def get_episode(self, context, episode_id):
        with self.memory(context).dao.read_snapshot() as view:
            record = view.get(SOURCE, episode_id)
        return _source_view(record) if record is not None else None

    def add_fact(self, context, subject, predicate, object, **fields):
        fact = self.memory(context).add_fact(subject, predicate, object, return_existing=True, **fields)
        return dict(ok=True, id=fact.id, text=fact.text, topic=fact.topic, topic_source=fact.topic_source)

    def update_fact(self, context, fact_id, **fields):
        fact = self.memory(context).update_fact(fact_id, **fields)
        if fact is None:
            return None
        return dict(ok=True, id=fact.id, text=fact.text, display=fact.display, source=fact.source,
                    source_roles=fact.source_roles,
                    topic=fact.topic, topic_source=fact.topic_source)

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

    def add_working(self, context, content, **fields):
        item = self.memory(context).remember_working(content, **fields)
        return dict(ok=True, id=item.id, kind=item.kind, session_id=item.session_id)

    def working_memory(self, context, session_id=None):
        rows = self.memory(context).working_memory(session_id=session_id)
        return dict(items=[dict(id=w.id, content=w.content, kind=w.kind, session_id=w.session_id,
                                created=fmt_date(w.created_at), expires_at=fmt_date(w.expires_at) if w.expires_at else None)
                           for w in rows])

    def clear_working(self, context, session_id):
        return dict(ok=True, cleared=self.memory(context).clear_working(session_id))

    def conflicts(self, context):
        return dict(conflicts=[asdict(c) for c in self.memory(context).conflicts()])

    def resolve_conflict(self, context, conflict_id, keep):
        return dict(ok=self.memory(context).resolve_conflict(conflict_id, keep=keep))

    def structured_profile(self, context):
        memory = self.memory(context)
        with memory.dao.read_snapshot() as view:
            subject = view.settings().subject_name or 'user'
            facts = view.focused().profile_facts(user_aliases(subject, context.user_id))
            return build_structured_profile(map(domain, facts), subject, context.user_id)

    def profile(self, context):
        with self.memory(context).dao.read_snapshot() as view:
            facts = [domain(record).text for record in view.focused().facts(FactQuery(limit=50, exclude_sensitive=True))]
        return dict(profile='\n'.join(facts), facts=facts)

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
                                               session_id=session_id, offset=offset)
            facts = [_fact_view(domain(record), view.at.timestamp()) for record in records]
            sources = view.session_source_ids(session_id, {source for row in facts for source in row['provenance']})
        for row in facts:
            row['provenance'] = [source for source in row['provenance'] if source in sources]
            row['redacted'] = row['sensitive'] and not include_sensitive
            if row['redacted']:
                for key in ('subject', 'predicate', 'object', 'why', 'category', 'topic', 'topic_source'):
                    row.pop(key, None)
                row['text'] = row['display'] = '[redacted sensitive fact]'
        report = rows[0] if rows else dict(episodes=0, episodes_consolidated=0, episodes_pending=0,
                                          extraction_failures=0, working_live=0)
        redacted = 0 if include_sensitive else report.get('facts_sensitive', 0)
        page = _page(facts, total, offset, limit)
        return {**report, 'ok': True, 'space_id': context.space_id, 'session_id': session_id,
                'include_sensitive': include_sensitive, 'facts_added': total,
                'facts_redacted': redacted, 'facts': facts, 'facts_page': page}

    def memories(self, context, facts_limit=None, facts_offset=0, episodes_limit=None, episodes_offset=0,
                 status=None, kind=None, q='', include_sensitive=False, category=None, outcome_kind=None,
                 expected_revision=None, evaluated_at=None, topic=None):
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
            request = FactQuery(state=state, valid_at=at, predicate=outcome_kind,
                                limit=max(1, facts_limit), exclude_sensitive=not include_sensitive)
            rows, fact_total = view.browse_facts(request, text=q.strip(), topic=topic, offset=facts_offset,
                                                category=(category or '').strip(),
                                                outcomes={'outcomes': True, 'attributes': False}.get(kind),
                                                count_only=facts_limit == 0)
            categories = view.fact_categories(include_sensitive=include_sensitive,
                                              outcomes={'outcomes': True, 'attributes': False}.get(kind))
            facts = [_fact_view(domain(row), at) for row in rows]
            episodes, episode_total = [], 0
            if include_sensitive:
                sources, episode_total = view.browse_sources(text=q.strip(), limit=episodes_limit, offset=episodes_offset)
                episodes = [_source_view(row) for row in sources]
            counts = view.overview(at)
        fact_page = _page(facts, fact_total, facts_offset, facts_limit)
        source_page = _page(episodes, episode_total, episodes_offset, episodes_limit)
        return dict(space_id=context.space_id, data_revision=revision, evaluated_at=at, profile='',
                    counts=counts,
                    facts=facts, fact_categories=categories,
                    episodes=episodes, facts_page=fact_page, episodes_page=source_page,
                    next_offsets=dict(facts=fact_page['next_offset'], episodes=source_page['next_offset']))

    def graph(self, context, as_of=None, include_sensitive=False, q='', live_only=False, limit=None):
        limit, _ = _bounds(limit)
        query = FactQuery(state=FactState.CURRENT if live_only else FactState.ALL,
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
                                live=facts[r.fact_id].is_live(at), fact_text=facts[r.fact_id].text,
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
        return dict(space_id=context.space_id, counts=counts, storage='postgres', embedder=self.embedding_model,
                    feed=feed, time_range={**times, **{key + '_h': fmt_datetime(value) if value is not None else None
                                                       for key, value in times.items()}},
                    consolidation_backlog=counts['episodes_pending'] > 0,
                    llm_configured=self.llm is not None, answerer_configured=self.answerer is not None)

    def agent_status(self, context, session_id=None):
        session = dict(id=session_id, episodes=0, episodes_pending=0, extraction_failures=0, working_live=0)
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
                               correct_fact='mnemotrace_update_fact', delete_fact='mnemotrace_delete_fact',
                               focus='mnemotrace_get_focus'))

    def audit(self, context, limit=40):
        predicates = [name for name, (field, _) in _BASIC.items() if field not in _MULTI_VALUED_FIELDS]
        memory = self.memory(context)
        coverage = EmbedderCoverage(self.embedder if self.embedder is not None else HashingEmbedder())
        by_kind = Counter()
        by_priority = [[] for _ in range(4)]

        def add(row):
            kind = row['kind']
            by_kind[kind] += 1
            bucket = by_priority[AUDIT_KIND_PRIORITY[kind]]
            if len(bucket) < limit:
                bucket.append(row)

        with memory.dao.read_snapshot() as view:
            overflowing = set()
            for slot in view.overflowing_slots(predicates):
                subject, predicate = slot['subject'], slot['predicate']
                overflowing.add((subject, predicate))
                add(dict(kind='slot_overflow', why='Several extracted facts occupy a single-valued slot',
                         action='Review the facts before clearing the slot', subject=subject,
                         predicate=predicate, label=_BASIC[predicate][1], count=slot['count']))

            cursor, checked_facts = None, 0
            while batch := view.facts(FactQuery(after_id=cursor)):
                for record in batch:
                    fact = domain(record)
                    coverage.add(fact.text, is_fact=True)
                    predicate = fact.predicate.strip().lower()
                    if predicate in OUTCOME_PREDICATES or (
                        fact.source != 'user' and (fact.subject, predicate) in overflowing
                    ):
                        continue
                    if row := fact_finding(fact):
                        add(row)
                checked_facts += len(batch)
                cursor = batch[-1].ref.record_id

            if coverage.enabled:
                for content in view.recent_source_texts():
                    coverage.add(content, is_fact=False)
                if row := coverage.finding(postgres=True):
                    add(row)

            orphans, orphan_count = view.orphan_entities(limit)
            for row in orphans:
                name = row['name']
                add(finding('orphan_entity', f"entity '{name}' has no relations",
                            'No fact connects this entity', entity=name))
            if orphan_count:
                by_kind['orphan_entity'] = orphan_count
            checked_entities = view.counts()[ENTITY]
            findings = [row for bucket in by_priority for row in bucket][:limit]
            for row in findings:
                if row['kind'] == 'slot_overflow':
                    row['samples'] = view.slot_samples(row['subject'], row['predicate'])
        total = by_kind.total()
        return dict(space_id=context.space_id, checked=dict(facts=checked_facts, entities=checked_entities),
                    total_findings=total, by_kind=dict(by_kind), findings=findings, truncated=total > len(findings))

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
            head = dict(space_id=context.space_id, include_sensitive=include_sensitive,
                        redacted_sensitive=not include_sensitive, focus=dict(track=settings.track, mute=settings.mute))
            yield json.dumps(head, ensure_ascii=False)[:-1]
            for name, kind in (('facts', FACT), ('episodes', SOURCE)):
                yield f',"{name}":['
                cursor, first = None, True
                while kind != SOURCE or include_sensitive:
                    batch = (view.portable_facts(after_id=cursor) if kind == FACT and include_sensitive
                             else view.records(kind, after_id=cursor))
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
                            payload.update(status='live' if item.is_live(at) else 'superseded',
                                           valid_at_h=fmt_date(item.valid_at),
                                           invalid_at_h=fmt_date(item.invalid_at) if item.invalid_at is not None else None)
                        else:
                            payload = _source_view(record)
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
