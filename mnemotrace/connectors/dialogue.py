"""Existing history formats adapt explicitly to complete dialogue messages."""
from __future__ import annotations

from dataclasses import asdict

from .base import ImportSession, to_epoch
from ..consolidate.messages import normalized


def batches(sessions, consolidate=None):
    output = []
    for session in sessions:
        record = asdict(session) if isinstance(session, ImportSession) else session
        if not isinstance(record, dict) or not isinstance(record.get('messages'), list):
            raise ValueError('Expected a session with complete messages')
        sid = record.get('session_id')
        if not isinstance(sid, str) or not sid:
            raise ValueError('Imported sessions require session_id')
        if record['messages'] and all(m.get('role') == 'document' for m in record['messages']):
            output.append((sid, None, record, False))
            continue
        extract = consolidate is not False
        messages = []
        for index, item in enumerate(record['messages']):
            if isinstance(item.get('content'), str) and not item['content'].strip():
                continue
            stamp = next((item[k] for k in ('event_time', 'timestamp', 'time', 'created_at', 'date')
                          if item.get(k) is not None), None)
            messages.append(dict(message_id=item.get('message_id') or f'import:{index}',
                role=item.get('role'), content=item.get('content'), event_time=to_epoch(stamp)))
        if messages:
            for start in range(0, len(messages), 1000):
                batch = messages[start:start + 1000]
                normalized(batch, sid, 'historical', 'auto' if extract else 'raw')
                output.append((sid, batch, record, extract))
    return output
