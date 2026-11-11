"""幂等的批量导入（AGENTS.md 「核心不变量」 "no silent corruption"，应用在导入链路上）：重复导入同一份导出
不得让每条记忆翻倍。指纹与快照一起持久化，所以去重在重启后依然生效。全部离线运行。"""
from __future__ import annotations
from mnemotrace.connectors.dialogue import import_messages
import pytest


from tests.embedder_fixtures import MockEmbedder
from tests.model_fixtures import MemoryEngine

_SESSIONS = [
    {"session_id": "s1", "messages": [{"role": "user", "content": "I have a dog named Mochi."}]},
    {"session_id": "s2", "messages": [{"role": "user", "content": "I drive a Tesla Model 3."}]},
]


@pytest.mark.history_import
def test_reimport_is_skipped():
    m = MemoryEngine(embedder=MockEmbedder(64))
    first = import_messages(m, _SESSIONS, user_id="u")
    assert first["episodes"] == 2 and first["skipped"] == 0
    again = import_messages(m, _SESSIONS, user_id="u")
    assert again["episodes"] == 0 and again["skipped"] == 2
    assert len(m.episodes_doc.values()) == 2  # 没有翻倍


@pytest.mark.history_import
def test_dialogue_ids_remain_idempotent_and_new_ids_allow_equal_content():
    m = MemoryEngine(embedder=MockEmbedder(64))
    import_messages(m, _SESSIONS, user_id="u")
    raw = import_messages(m, _SESSIONS, user_id="u")
    assert raw["episodes"] == 0
    copies = [{**session, 'messages': [{**msg, 'message_id': 'another'} for msg in session['messages']]}
              for session in _SESSIONS]
    assert import_messages(m, copies, user_id='u')["episodes"] == 2
    assert len(m.episodes_doc.values()) == 4


@pytest.mark.history_import
def test_new_sessions_still_ingest_alongside_skips():
    m = MemoryEngine(embedder=MockEmbedder(64))
    import_messages(m, _SESSIONS, user_id="u")
    mixed = import_messages(m,
        _SESSIONS + [{"session_id": "s3", "messages": [{"role": "user", "content": "I started judo."}]}],
        user_id="u")
    assert mixed["skipped"] == 2 and mixed["episodes"] == 1
    assert len(m.episodes_doc.values()) == 3
