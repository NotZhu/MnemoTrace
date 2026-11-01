"""幂等的批量导入（AGENTS.md 「核心不变量」 "no silent corruption"，应用在导入链路上）：重复导入同一份导出
不得让每条记忆翻倍。指纹与快照一起持久化，所以去重在重启后依然生效。全部离线运行。"""
from __future__ import annotations

import os
import shutil
import tempfile

from mnemotrace.embed import HashingEmbedder
from tests.model_fixtures import Memory
from tests.model_fixtures import MemoryService

_SESSIONS = [
    {"session_id": "s1", "messages": [{"role": "user", "content": "I have a dog named Mochi."}]},
    {"session_id": "s2", "messages": [{"role": "user", "content": "I drive a Tesla Model 3."}]},
]


def test_reimport_is_skipped():
    m = Memory(embedder=HashingEmbedder(64))
    first = m.import_messages(_SESSIONS, user_id="u")
    assert first["episodes"] == 2 and first["skipped"] == 0
    again = m.import_messages(_SESSIONS, user_id="u")
    assert again["episodes"] == 0 and again["skipped"] == 2
    assert len(m.episodes_doc.values()) == 2  # 没有翻倍


def test_dialogue_ids_remain_idempotent_and_new_ids_allow_equal_content():
    m = Memory(embedder=HashingEmbedder(64))
    m.import_messages(_SESSIONS, user_id="u")
    raw = m.import_messages(_SESSIONS, user_id="u", dedupe=False)
    assert raw["episodes"] == 0
    copies = [{**session, 'messages': [{**msg, 'message_id': 'another'} for msg in session['messages']]}
              for session in _SESSIONS]
    assert m.import_messages(copies, user_id='u')["episodes"] == 2
    assert len(m.episodes_doc.values()) == 4


def test_fingerprints_survive_restart():
    d = tempfile.mkdtemp(prefix="mnemotrace_dedupe_")
    try:
        p = os.path.join(d, "u.pkl")
        m = Memory.open(p, embedder=HashingEmbedder(64))
        m.import_messages(_SESSIONS, user_id="u")
        m.save()
        m2 = Memory.open(p, embedder=HashingEmbedder(64))  # 模拟全新进程
        again = m2.import_messages(_SESSIONS, user_id="u")
        assert again["skipped"] == 2 and len(m2.episodes_doc.values()) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_new_sessions_still_ingest_alongside_skips():
    m = Memory(embedder=HashingEmbedder(64))
    m.import_messages(_SESSIONS, user_id="u")
    mixed = m.import_messages(
        _SESSIONS + [{"session_id": "s3", "messages": [{"role": "user", "content": "I started judo."}]}],
        user_id="u")
    assert mixed["skipped"] == 2 and mixed["episodes"] == 1
    assert len(m.episodes_doc.values()) == 3


def test_service_import_is_idempotent():
    d = tempfile.mkdtemp(prefix="mnemotrace_dedupesvc_")
    try:
        svc = MemoryService(data_dir=d, embedder_name="hashing", llm_name="")
        first = svc.import_("u", sessions=_SESSIONS)
        again = svc.import_("u", sessions=_SESSIONS)
        assert first["episodes"] == 2
        assert again["episodes"] == 0 and again["skipped"] == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
