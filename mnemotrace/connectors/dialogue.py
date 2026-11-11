"""Existing history formats adapt explicitly to complete dialogue messages."""
from __future__ import annotations

from dataclasses import asdict

from .base import ImportSession, record_time
from ..consolidate.pipeline import normalized


def batches(sessions, consolidate=None):
    output = []
    for session in sessions:
        record = asdict(session) if isinstance(session, ImportSession) else session
        if not isinstance(record, dict) or not isinstance(record.get('messages'), list):
            raise ValueError('Expected a session with complete messages')
        sid = record.get('session_id')
        if not isinstance(sid, str) or not sid:
            raise ValueError('Imported sessions require session_id')
        if any(not isinstance(item, dict) for item in record['messages']):
            raise ValueError('Expected each message to be an object')
        extract = consolidate is not False
        messages = []
        for index, item in enumerate(record['messages']):
            if isinstance(item.get('content'), str) and not item['content'].strip():
                continue
            stamp = record_time(item, 'event_time', 'timestamp', 'time', 'created_at', 'date')
            messages.append(dict(message_id=item.get('message_id') or f'import:{index}',
                role=item.get('role'), content=item.get('content'), event_time=stamp))
        if messages:
            for start in range(0, len(messages), 1000):
                batch = messages[start:start + 1000]
                normalized(batch, sid, extract)
                output.append((sid, batch, record, extract))
    return output


def import_history(service, context, *, sessions=None, data=None, format='auto', consolidate=None,
            generate_digests=True, agent_id=None):
    from . import parse
    from ..consolidate.pipeline import MessageConflictError
    from ..service import ProcessingFailure
    from ..extraction import LLMNotConfiguredError
    from ..store.postgres.errors import MessageConflict

    sessions = sessions if sessions is not None else parse(data, format=format)
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
            source_metadata = {'digest_requested': generate_digests}
            metadata = source.get('metadata')
            if isinstance(metadata, dict) and metadata.get('source') == 'agent_session':
                source_metadata['source'] = 'agent_session'
            result = service.remember_messages(
                context, messages, session_id, agent_id=agent_id,
                processing='immediate' if consolidate is True else 'buffered',
                extract=extract, source_metadata=source_metadata)
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



def import_messages(memory, sessions, user_id: str = 'default', consolidate: bool | None = None,
                    generate_digests: bool = True) -> dict:
    """Explicitly replay complete dialogue messages through the offline engine.

    Undated messages remain unknown. Positional import IDs are stable within an ordered
    source export; changing an existing position requires an explicit source message ID.
    """
    if memory._pipeline_broken:
        raise RuntimeError('Memory checkpoint failed; create a new instance')
    from ..consolidate.pipeline import PipelineTasks
    from ..extraction import ExtractionError, require_llm
    plans = batches(list(sessions), consolidate)
    tasks = PipelineTasks(memory, user_id)
    if any(extract and tasks.inspect(messages, sid)[1]
           for sid, messages, _, extract in plans):
        require_llm(memory.llm)
    stats = dict(sessions=0, episodes=0, skipped=0, facts_added=0, digests=0,
                 facts_deferred=0, deferred_reason=None)
    before = len(memory.fact_store.values())
    prior_digests = {e.id for e in memory.episodes_doc.values() if e.digest}
    for sid, messages, record, extract in plans:
        result = memory.remember_messages(messages, sid, user_id, extract=extract)
        fresh = result['accepted']
        if not extract:
            stats['facts_deferred'] += fresh
            stats['deferred_reason'] = 'not_requested'
        for row in result['messages']:
            ep = memory.episodes_doc.get(row['episode_id'])
            for key in ('source',):
                if key in record.get('metadata', {}):
                    ep.metadata.setdefault(key, record['metadata'][key])
            memory._sync_episode(ep)
        stats['sessions'] += bool(fresh)
        stats['episodes'] += fresh
        stats['skipped'] += len(messages) - fresh
        if extract and fresh:
            result = memory.flush_messages(user_id, sid)
            if not result['ok']:
                raise ExtractionError(stored_raw=True, episode_ids=[r['episode_id'] for r in result['jobs']])
    stats['facts_added'] = len(memory.fact_store.values()) - before
    stats['digests'] = sum(bool(e.digest) and e.id not in prior_digests for e in memory.episodes_doc.values()
                             if e.user_id == user_id and e.message_id is not None)
    return stats



def import_data(memory, data, format="auto", user_id="default", session_id="imported", **options):
    """Parse and replay history explicitly from the retained connector source."""
    from . import parse

    return import_messages(memory, parse(data, format=format, session_id=session_id), user_id=user_id, **options)
