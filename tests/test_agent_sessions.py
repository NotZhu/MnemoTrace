"""写入 Claude Code / Codex 会话日志——此前缺失的一条路径。

owner 值得记忆的材料正是他的 agent 会话（磁盘上放着 1600+ 份 Claude Code transcript 和 1200+ 份
Codex transcript），但一直没有入口：通用 JSONL 读取器保留了工具调用却丢掉了对话内容，实际效果是
几乎什么都没存下来。这些测试钉死了哪些内容会被保留，以及——同样重要的是——哪些会被丢弃。"""
from __future__ import annotations

import pytest

import json
import os

from mnemotrace.connectors import parse, sniff
from mnemotrace.connectors.agent_sessions import parse_agent_session


def _cc(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows)


def test_keeps_the_conversation_and_drops_the_machinery():
    log = _cc([
        {"type": "user", "timestamp": "2026-08-01T10:00:00Z",
         "message": {"role": "user", "content": "I moved the project to Rust for the hot path."}},
        {"type": "assistant", "timestamp": "2026-08-01T10:00:05Z",
         "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "let me consider"}]}},
        {"type": "assistant", "timestamp": "2026-08-01T10:00:09Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Rust for the hot path makes sense."}]}},
        {"type": "assistant", "timestamp": "2026-08-01T10:00:10Z",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", "timestamp": "2026-08-01T10:00:11Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "3 matches"}]}},
        {"type": "queue-operation", "operation": "enqueue", "content": "routing preamble"},
        {"type": "ai-title", "title": "Rust migration"},
    ])
    sessions = parse_agent_session(log, session_id="s1")
    assert len(sessions) == 1
    bodies = [m.content for m in sessions[0].messages]
    assert bodies == ["I moved the project to Rust for the hot path.",
                      "Rust for the hot path makes sense."], bodies
    # 有意排除 thinking：那是 agent 可能已丢弃的推理过程
    assert not any("consider" in b for b in bodies)
    assert sessions[0].messages[0].event_time is not None  # 时间戳要存活下来，供双时间轴层使用


def test_short_confirmations_are_kept_and_tool_echoes_are_dropped():
    log = _cc([
        {"type": "user", "message": {"role": "user", "content": "ok"}},
        {"type": "user", "message": {"role": "user", "content": "<tool_use_result>done</tool_use_result>"}},
        {"type": "user", "message": {"role": "user", "content": "My daughter started at Tsinghua this year."}},
    ])
    bodies = [m.content for m in parse_agent_session(log)[0].messages]
    assert bodies == ["ok", "My daughter started at Tsinghua this year."]


def test_agent_message_identity_time_and_short_confirmation_reach_watcher(monkeypatch):
    from mnemotrace.connectors import watch
    session, = parse_agent_session(_cc([
        {'type': 'assistant', 'uuid': 'proposal', 'timestamp': 100,
         'message': {'role': 'assistant', 'content': 'Use the synthetic local queue.'}},
        {'type': 'user', 'uuid': 'confirmed', 'timestamp': 101,
         'message': {'role': 'user', 'content': 'yes'}},
    ]), session_id='s')
    monkeypatch.setattr(watch, 'load_sessions', lambda *args, **kwargs: [session])
    requests = []
    def post(base, key, path, body, timeout):
        requests.append((path, body))
        return {'ok': True, 'sessions': 1, 'episodes': 2}
    monkeypatch.setattr(watch, '_post', post)
    watch.ingest('http://synthetic.invalid', 'synthetic-key', ['synthetic-path'])
    messages = requests[0][1]['sessions'][0]['messages']
    assert [m['message_id'] for m in messages] == ['proposal', 'confirmed']
    assert [m['event_time'] for m in messages] == [100, 101]
    assert messages[1]['role'] == 'user' and messages[1]['content'] == 'yes'


def test_sub_agent_turns_are_excluded_by_default():
    log = _cc([
        {"type": "user", "message": {"role": "user", "content": "Please refactor the parser module."}},
        {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Sub-agent internal chatter here."}]}},
    ])
    assert len(parse_agent_session(log)[0].messages) == 1
    assert len(parse_agent_session(log, include_sidechains=True)[0].messages) == 2


def test_codex_rollout_format_is_understood():
    """Codex 把每一轮包装成 response_item/payload，而不是 Claude Code 的 `message`。"""
    log = _cc([
        {"type": "session_meta", "payload": {"id": "x"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "<permissions instructions>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "Ship the release notes today."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Release notes are drafted."}]}},
    ])
    bodies = [m.content for m in parse_agent_session(log)[0].messages]
    # developer 轮次是 harness 指令，不属于用户的历史对话
    assert bodies == ["Ship the release notes today.", "Release notes are drafted."], bodies


def test_sniff_routes_agent_logs_away_from_the_generic_jsonl_reader():
    """没有这个分流，通用读取器会静默地保留工具调用并丢失对话。"""
    cc = _cc([
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "Where did we land on pricing?"}},
        {"type": "assistant", "isSidechain": False,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "We landed on usage-based."}]}},
    ])
    assert sniff(cc) == "agent_session"
    bodies = [m.content for m in parse(cc)[0].messages]
    assert "We landed on usage-based." in bodies


def test_an_all_machinery_session_yields_nothing_rather_than_junk():
    log = _cc([
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "queue-operation", "operation": "enqueue"},
    ])
    assert parse_agent_session(log) == []


class TestSecretRedaction:
    """Agent 会话里经常有真实凭证——用户粘贴一个 key，某条命令回显了 .env。长期记忆是最不该存放
    凭证的地方：持久、可被每个接入的 agent 检索、还能导出。脱敏必须在写入时进行，因为密钥一旦进了
    存储，就已经泄漏进嵌入、摘要和抽取出的 fact。"""

    def test_common_credential_shapes_are_redacted(self):
        from mnemotrace.connectors.agent_sessions import redact_secrets

        for raw in [
            "DEEPSEEK_API_KEY=sk-EXAMPLENOTAREALKEY000000000000",
            "export ARK_API_KEY=abc123def456ghi789",
            "token: ghp_abcdefghijklmnopqrstuvwxyz1234",
            "AKIA1234567890ABCDEF",
        ]:
            assert "[REDACTED]" in redact_secrets(raw), raw
            assert "EXAMPLENOTAREAL" not in redact_secrets(raw)

    def test_ordinary_technical_prose_is_untouched(self):
        """过度脱敏会悄悄破坏它本想保护的记忆。"""
        from mnemotrace.connectors.agent_sessions import redact_secrets

        for raw in [
            "我们决定用 bge-small 作为 embedder",
            "The extractor is volcano:doubao-seed-1-6-flash-250615",
            "engram_lean scored 84.4% vs full_context 78.8%",
        ]:
            assert redact_secrets(raw) == raw, raw

    def test_redaction_happens_during_parsing_not_after(self):
        from mnemotrace.connectors.agent_sessions import parse_agent_session

        log = json.dumps({"type": "user", "message": {
            "role": "user", "content": "Here is the key: sk-abcdefghijklmnopqrstuvwxyz012345"}})
        body = parse_agent_session(log)[0].messages[0].content
        assert "[REDACTED]" in body and "sk-abcdef" not in body


class TestWatcher:
    def test_a_live_session_is_skipped_until_it_goes_quiet(self, tmp_path, monkeypatch):
        """仍在被写入的 transcript 是个移动目标；现在写入它只会导致之后带着尾巴重新写入一遍。"""
        from mnemotrace.connectors import watch

        live = tmp_path / "live.jsonl"
        live.write_text("x" * 4096)
        monkeypatch.setattr(watch, "find_sessions", lambda **kw: [str(live)])

        now = os.path.getmtime(live) + 60           # 一分钟前写入
        assert watch.pending_sessions({}, now=now, quiet_seconds=900) == []
        later = os.path.getmtime(live) + 3600       # 一小时后，它已经安静下来
        assert watch.pending_sessions({}, now=later, quiet_seconds=900) == [str(live)]

    def test_unchanged_sessions_are_not_reingested(self, tmp_path, monkeypatch):
        from mnemotrace.connectors import watch

        f = tmp_path / "done.jsonl"
        f.write_text("y" * 4096)
        monkeypatch.setattr(watch, "find_sessions", lambda **kw: [str(f)])
        later = os.path.getmtime(f) + 3600

        state = {"seen": {str(f): f.stat().st_size}}
        assert watch.pending_sessions(state, now=later, quiet_seconds=900) == []
        f.write_text("y" * 8192)  # 会话又继续了
        assert watch.pending_sessions(state, now=later, quiet_seconds=900) == [str(f)]


def test_watcher_closes_each_session_so_it_gets_distilled(monkeypatch):
    """只做 import 只会存下 transcript。蒸馏成 decisions/findings/lessons 发生在 close_session，
    所以跳过 close 的 watcher 会让记忆堆满原始轮次而没有结论——这正是整条路径要修复的状态。"""
    from mnemotrace.connectors import watch
    from mnemotrace.connectors.base import ImportMessage, ImportSession

    sessions = [
        ImportSession(session_id="claude-code:a", messages=[ImportMessage(content="a long enough turn")]),
        ImportSession(session_id="codex:b", messages=[ImportMessage(content="another long enough turn")]),
    ]
    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: sessions)
    calls = []

    def fake_post(base, key, path, body, timeout):
        calls.append((path, body))
        if path == "/v1/import":
            return {"ok": True, "sessions": 2, "episodes": 2}
        return {"ok": True, "outcomes": 3}

    monkeypatch.setattr(watch, "_post", fake_post)
    result = watch.ingest("http://x", "k", ["p1", "p2"])

    assert [c[0] for c in calls] == ["/v1/import", "/v1/sessions/close", "/v1/sessions/close"]
    assert {c[1]["session_id"] for c in calls[1:]} == {"claude-code:a", "codex:b"}
    assert all(c[1]["outcomes"] is False for c in calls[1:])
    assert result["outcomes"] == 6  # 每个会话 3 条


def test_one_failed_distil_does_not_cost_the_others(monkeypatch):
    """transcript 到那时已经存好了；蒸馏失败可以重试，并非致命。"""
    from mnemotrace.connectors import watch
    from mnemotrace.connectors.base import ImportMessage, ImportSession

    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [
        ImportSession(session_id=f"s{i}", messages=[ImportMessage(content="a long enough turn")])
        for i in range(3)
    ])

    def flaky(base, key, path, body, timeout):
        if path == "/v1/import":
            return {"ok": True, "sessions": 3}
        if body["session_id"] == "s1":
            raise RuntimeError("model timeout")
        return {"ok": True, "outcomes": 2}

    monkeypatch.setattr(watch, "_post", flaky)
    result = watch.ingest("http://x", "k", ["p"])
    assert result["outcomes"] == 4 and result["distil_failed"] == 1


# ---------------------------------------------------------------------------------------------------
# 门槛机制：agent 会话只存储起来供 close 时蒸馏，绝不交给 LLMExtractor。
# 在加门槛之前实测：12 个真实轮次 -> 11 条垃圾 fact（`The | occupation | nearly empty`），owner 的
# 存储里 88 条 fact 中有 84 条 `occupation` 行，全部来自无 LLM 的 fallback 时期。
# ---------------------------------------------------------------------------------------------------

# 一段形似真实工作会话 transcript 的文本：主语是代词/冠词的句子、markdown、一个配置路径。
# LLMExtractor 会把它变成垃圾（见 test_records_import_requests_model_extraction，它证明了当来源不是
# agent 会话时，同样的文本确实会被抽取）。
_TRANSCRIPT_A = [
    {"role": "user", "content": "The config file is nearly empty. This is now clear: **EKOS is deployed on "
                                "an Ubuntu server** and the backend framework is FastAPI."},
    {"role": "assistant", "content": "This confirms the deployment. The next step is to check nginx."},
]
_TRANSCRIPT_B = [
    {"role": "user", "content": "That module is broken. The extractor is volcano and the judge is deepseek."},
    {"role": "assistant", "content": "It looks like the retry loop is the problem here."},
]


def _agent_sessions(prefix: str = "claude-code") -> list[dict]:
    return [
        {"session_id": f"{prefix}:a", "event_time": 1_756_000_000.0,
         "metadata": {"source": "agent_session"}, "messages": _TRANSCRIPT_A},
        {"session_id": f"{prefix}:b", "event_time": 1_756_000_100.0,
         "metadata": {"source": "agent_session"}, "messages": _TRANSCRIPT_B},
    ]


def test_explicit_raw_agent_session_import_does_not_require_a_model(tmp_path):
    from mnemotrace import Memory
    from mnemotrace.service import MemoryService

    mem = Memory()
    stats = mem.import_messages(_agent_sessions(), user_id="u", consolidate=False)
    assert stats["episodes"] == 4
    assert stats["facts_added"] == 0, stats
    assert stats["facts_deferred"] == 4 and stats["deferred_reason"] == "not_requested", stats
    eps = [ep for ep in mem.episodes_doc.values() if ep.user_id == "u"]
    assert len(eps) == 4
    for ep in eps:
        assert ep.consolidated is True  # 下游绝不能再把它排进 LLMExtractor
        assert ep.message_id is not None
        assert ep.metadata["source"] == "agent_session"
    assert stats["summaries"] == 0  # Offline input remains searchable as original events.

    # close_session 就是那个"排水口"，否则 pending 的 episode 会被逐轮抽取。
    svc = MemoryService(data_dir=str(tmp_path), embedder_name="hashing", llm_name="")
    assert svc.import_("u", sessions=_agent_sessions(), consolidate=False)["facts_deferred"] == 4
    closed = svc.close_session("u", "claude-code:a", outcomes=False)
    assert closed["episodes"] == 2
    assert closed["pending_consolidated"] == 0 and closed["facts_added"] == 0, closed


@pytest.mark.parametrize('consolidate', [None, True])
def test_agent_default_automatic_import_requires_model_before_any_write(tmp_path, consolidate):
    from mnemotrace import Memory
    from mnemotrace.service import MemoryService
    from mnemotrace.extraction import LLMNotConfiguredError
    mem = Memory()
    with pytest.raises(LLMNotConfiguredError):
        mem.import_messages(_agent_sessions(), user_id='u', consolidate=consolidate)
    assert not mem.episodes_doc.values()
    svc = MemoryService(data_dir=str(tmp_path), embedder_name='hashing', llm_name='')
    with pytest.raises(LLMNotConfiguredError):
        svc.import_('u', sessions=_agent_sessions(), consolidate=consolidate)
    assert not svc.get('u').episodes_doc.values()


def test_agent_session_import_with_llm_and_consolidate_true_extracts():
    """有 LLM 时同样的 12 个轮次产出了 10 条干净的 fact（`EKOS | deployed_on | Ubuntu server`），
    所以显式选择加入时仍必须执行逐轮抽取。"""
    from tests.model_fixtures import Memory

    class StubLLM:
        def complete(self, prompt: str, system: str = "", **kw) -> str:
            return '[{"subject": "EKOS", "predicate": "deployed_on", "object": "Ubuntu server"}]'

    mem = Memory(llm=StubLLM())
    stats = mem.import_messages(_agent_sessions(), user_id="u", consolidate=True)
    assert stats["facts_added"] >= 1, stats
    assert stats["facts_deferred"] == 0 and stats["deferred_reason"] is None, stats
    assert all("extraction" not in ep.metadata for ep in mem.episodes_doc.values())


def test_agent_session_consolidate_true_without_llm_is_rejected():
    from mnemotrace import Memory
    from mnemotrace.extraction import LLMNotConfiguredError
    mem = Memory()
    with pytest.raises(LLMNotConfiguredError):
        mem.import_messages(_agent_sessions(), user_id="u", consolidate=True)
    assert not mem.episodes_doc.values()


def test_records_import_requests_model_extraction():
    """记录导入的抽取回归：门槛看的是 source，而不是文本内容。同样的句子以 `records` 形式
    到来时，照旧走 LLMExtractor。"""
    from mnemotrace.connectors import parse
    from tests.model_fixtures import Memory

    records = [{"content": m["content"], "role": m["role"], "session_id": "r1"}
               for m in _TRANSCRIPT_A + _TRANSCRIPT_B]
    records.append({"content": "I moved to Shenzhen last year and I work at Moonshot AI.",
                    "role": "user", "session_id": "r1"})
    mem = Memory()
    stats = mem.import_messages(parse(records, format="records"), user_id="u")
    assert stats["facts_added"] >= 1, stats
    assert stats["facts_deferred"] == 0 and stats["deferred_reason"] is None


# ---------------------------------------------------------------------------------------------------
# Watcher 的线上协议格式与状态维护纪律。
# ---------------------------------------------------------------------------------------------------

def _session(sid: str, when: float | None = 1_756_000_000.0):
    from mnemotrace.connectors.base import ImportMessage, ImportSession
    return ImportSession(session_id=sid, event_time=when,
                         messages=[ImportMessage(content="a long enough turn to survive")],
                         metadata={"source": "agent_session"})


def _args(tmp_path, **over):
    import argparse
    base = dict(url="http://127.0.0.1:9", key="k", key_file="", since="", limit=25, quiet_seconds=0,
                state=str(tmp_path / "state" / "watch_state.json"), no_close=False,
                immediate=False, dry_run=False, once=True, every="")
    base.update(over)
    return argparse.Namespace(**base)


def _quiet_file(tmp_path, name: str = "s.jsonl", age: int = 3600) -> str:
    f = tmp_path / name
    f.write_text("x" * 4096)
    old = f.stat().st_mtime - age
    os.utime(f, (old, old))
    return str(f)


def test_watcher_payload_carries_source_and_session_time(monkeypatch):
    """服务端靠 `metadata.source` 把 transcript 从逐轮抽取中分流出去；`event_time` 维持双时间轴；
    不带 `consolidate` 键意味着由服务端决定（只产出 outcomes）。每条消息的时间戳和文件路径绝不
    离开本机。"""
    from mnemotrace.connectors import watch

    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("claude-code:a")])
    bodies = []

    def fake_post(base, key, path, body, timeout):
        bodies.append((path, body))
        return {"ok": True, "sessions": 1, "episodes": 1, "outcomes": 1}

    monkeypatch.setattr(watch, "_post", fake_post)
    result = watch.ingest("http://x", "k", ["p1"])
    path, body = bodies[0]
    assert path == "/v1/import"
    row = body["sessions"][0]
    assert row["metadata"] == {"source": "agent_session"}
    assert row["event_time"] == 1_756_000_000.0
    assert set(row["messages"][0]) == {"message_id", "role", "content", "event_time"}
    assert row['messages'][0]['message_id'] == 'import:0'
    assert row['messages'][0]['event_time'] is None
    assert "consolidate" not in body
    assert result["closed_ok"] == ["claude-code:a"] and result["close_failed"] == []
    assert result["sessions_by_path"] == {"p1": ["claude-code:a"]}

    bodies.clear()
    watch.ingest("http://x", "k", ["p1"], immediate=True)
    assert bodies[0][1]["consolidate"] is True

    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("codex:b", when=None)])
    bodies.clear()
    watch.ingest("http://x", "k", ["p2"])
    assert "event_time" not in bodies[0][1]["sessions"][0]


def test_watcher_does_not_strand_sessions_older_than_last_run(tmp_path, monkeypatch):
    """用 last_run 推导 `since` 会静默丢掉所有 --limit 限额没轮到的 transcript。大小台账才是幂等
    机制；`since` 只能是显式指定的时间窗口。"""
    from mnemotrace.connectors import watch

    old = _quiet_file(tmp_path, age=3 * 86400)
    mtime = os.path.getmtime(old)
    monkeypatch.setattr(watch, "find_sessions",
                        lambda since=None, **kw: [old] if since is None or mtime >= since else [])
    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("s")])
    monkeypatch.setattr(watch, "_post", lambda *a, **k: {"ok": True, "episodes": 1, "outcomes": 0})

    state_path = tmp_path / "state" / "watch_state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps({"seen": {}, "last_run": mtime + 86400}))
    out = watch.run_once(_args(tmp_path))
    assert out["exit"] == 0 and out["sessions"] == 1, out
    assert old in json.loads(state_path.read_text())["seen"]


def test_failed_close_is_not_marked_seen_until_third_failure(tmp_path, monkeypatch):
    """已存储但未蒸馏的 transcript 必须重试，否则记忆里只剩原始轮次而没有结论——但要有限度，
    免得一个无法消化的文件占掉每轮 --limit 的一个名额。"""
    from mnemotrace.connectors import watch

    path = _quiet_file(tmp_path)
    monkeypatch.setattr(watch, "find_sessions", lambda **kw: [path])
    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("s")])

    def flaky(base, key, route, body, timeout):
        if route == "/v1/import":
            return {"ok": True, "episodes": 1}
        raise RuntimeError("model timeout")

    monkeypatch.setattr(watch, "_post", flaky)
    args = _args(tmp_path)
    for n in (1, 2):
        out = watch.run_once(args)
        assert out["exit"] == 0 and out["close_failed"] == 1
        state = watch._load_state(args.state)
        assert path not in state["seen"], state
        assert state["close_failures"][path] == n
        assert state["last_result"]["close_failed"] == 1
    watch.run_once(args)
    state = watch._load_state(args.state)
    assert path in state["seen"] and path not in state["close_failures"], state


def test_unreachable_server_exits_75_and_leaves_state_untouched(tmp_path, monkeypatch):
    import urllib.error

    from mnemotrace.connectors import watch

    path = _quiet_file(tmp_path)
    monkeypatch.setattr(watch, "find_sessions", lambda **kw: [path])
    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("s")])

    def down(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(watch, "_post", down)
    args = _args(tmp_path)
    assert watch.run_once(args)["exit"] == 75
    assert not os.path.exists(args.state)


def test_every_loop_runs_n_times_with_injected_sleep(tmp_path, monkeypatch):
    from mnemotrace.connectors import watch

    runs = []
    monkeypatch.setattr(watch, "run_once", lambda args: runs.append(args.state) or {"exit": 0})
    naps = []

    def fake_sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(watch, "_sleep", fake_sleep)
    code = watch.main(["--every", "2m", "--key", "k", "--state", str(tmp_path / "s.json")])
    assert code == 0 and len(runs) == 3 and naps == [120, 120, 120]


def test_lock_makes_second_run_skip(tmp_path, monkeypatch):
    """launchd 不管上一个 backfill tick 是否还在发送，每 30 分钟照常触发一次。"""
    from mnemotrace import _flock
    from mnemotrace.connectors import watch

    path = _quiet_file(tmp_path)
    monkeypatch.setattr(watch, "find_sessions", lambda **kw: [path])
    monkeypatch.setattr(watch, "load_sessions", lambda paths, **kw: [_session("s")])
    posted = []
    monkeypatch.setattr(watch, "_post", lambda *a, **k: posted.append(a) or {"ok": True})

    args = _args(tmp_path)
    os.makedirs(os.path.dirname(args.state))
    holder = open(os.path.join(os.path.dirname(args.state), "watch.lock"), "a+")
    assert _flock.acquire(holder, blocking=False)  # 与 watch._lock 用的是同一原语，随平台而定
    try:
        out = watch.run_once(args)
        assert out == {"exit": 0, "skipped": "locked"} and posted == []
    finally:
        holder.close()
    assert watch.run_once(args)["exit"] == 0 and posted  # 已释放 -> 下一个 tick 可以继续


def test_key_file_and_url_env_alias(tmp_path, monkeypatch):
    from mnemotrace.connectors import watch

    kf = tmp_path / "watch.key"
    kf.write_text("from-file\n")
    assert watch._resolve_key(_args(tmp_path, key="", key_file=str(kf))) == "from-file"
    assert watch._resolve_key(_args(tmp_path, key="explicit", key_file=str(kf))) == "explicit"
    monkeypatch.setenv("MNEMOTRACE_API_KEY", "from-env")
    assert watch._resolve_key(_args(tmp_path, key="", key_file="")) == "from-env"
    assert watch._parse_duration("30m") == 1800 and watch._parse_duration("2h") == 7200
    assert watch._parse_duration("1d") == 86400


def test_a_tick_larger_than_one_request_is_split_not_refused():
    """25 份真实 transcript 序列化后约 18 MB，而服务端上限是 2 MiB。作为单个请求体发送会被 413
    拒绝，而且由于服务端在请求体中途断开连接，urllib 抛出 "Broken pipe"，调用方把它当成"服务端
    不可达"并无限重试——两天内 115 次相同的失败，什么都没写入。"""
    from mnemotrace.connectors import watch
    rows = [{"session_id": f"s{i}", "messages": [{"role": "user", "content": "x" * 400_000}]}
            for i in range(6)]
    batches, oversized = watch.chunk_rows(rows, max_bytes=1_000_000)

    assert not oversized
    assert len(batches) > 1 and sum(len(b) for b in batches) == 6   # 拆分了，且一条都没丢
    assert all(sum(watch._row_bytes(r) for r in b) <= 1_000_000 for b in batches)


def test_a_session_too_large_to_send_is_named_not_retried_forever():
    """会话是最小原子——拆开一个等于把半截对话交给抽取器——所以超限的 transcript 会被报告出来，
    而不是静默丢弃或无限重试。"""
    from mnemotrace.connectors import watch
    rows = [{"session_id": "small", "messages": [{"role": "user", "content": "hi there"}]},
            {"session_id": "huge", "messages": [{"role": "user", "content": "x" * 3_000_000}]}]
    batches, oversized = watch.chunk_rows(rows, max_bytes=1_000_000)

    assert [r["session_id"] for r in oversized] == ["huge"]
    assert [r["session_id"] for b in batches for r in b] == ["small"]
