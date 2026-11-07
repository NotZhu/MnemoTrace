"""针对 eval/facility.py（L0-L4 facility harness）的测试。

仅使用合成夹具。该 harness 读取的真实语料（owner 的 Claude Code 与 Codex 转录）包含同事姓名和业务
细节，因此任何测试都不得触碰它：每个测试都构建一个微型的假 $HOME，并把所有路径指向它。每个指标都
有一个测试，一旦指标以"看似合理但错误"的方式计算就会失败——例如工具名的子串匹配、缺失的
`"tool_result"` 预过滤、未终止的 watch.log 块、把 ledger 当成 close 的证据。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from argparse import Namespace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval import facility  # noqa: E402

PROJECT_DIR = "-Users-nobody-SECRETPROJ"
CLAUDE_UUID = "aaaaaaaa-1111-2222-3333-444444444444"
SESSION_LABEL = "claude-code:SECRETPROJ:aaaaaaaa"
SECRET_TEXT = "SECRETFACTTEXT the owner told the agent about a colleague"
PAD = "x" * 2600  # 让文件大小超过 find_sessions 的 2048 字节下限


def _cc_row(**kw) -> str:
    row = {"type": "assistant", "timestamp": "2026-09-01T10:00:00.000Z",
           "isSidechain": False, "cwd": "/nowhere"}
    row.update(kw)
    return json.dumps(row)


def _write(path: str, lines: list[str], mtime_age_s: float = 7200.0) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    old = time.time() - mtime_age_s
    os.utime(path, (old, old))
    return path


def claude_transcript(home: str, name: str = f"{CLAUDE_UUID}.jsonl",
                      project: str = PROJECT_DIR, recall_name: str = "mcp__mnemotrace__mnemotrace_recall",
                      with_result: bool = True, mtime_age_s: float = 7200.0) -> str:
    """一份 Claude Code 转录：一次真实的 mnemotrace_recall tool_use、对应的 tool_result、散文提及、填充内容。

    散文块三次点名同一个工具。对原始字节做子串匹配会数出 4 次调用；结构化匹配必须只数 1 次。
    """
    lines = [
        _cc_row(message={"role": "user", "content": [{"type": "text", "text": PAD}]}),
        _cc_row(message={"role": "assistant", "content": [
            {"type": "text",
             "text": f"I will call {recall_name} now. Note that {recall_name} is the read tool; "
                     f"{recall_name} takes a query."},
        ]}),
        _cc_row(message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_01", "name": recall_name, "input": {"query": "q"}},
        ]}),
        _cc_row(message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_02", "name": "Read", "input": {"file_path": "/tmp/a"}},
        ]}),
    ]
    if with_result:
        # 这一行在预过滤可见的 token 只有 "tool_result" / "tool_use_id"。两者都不包含 `"tool_use"`
        # 这段字节（"tool_use_id" 里缺少右引号），所以省略 `"tool_result"` 的预过滤会丢掉这一行，
        # reach 会因此静默读成 0。
        result = _cc_row(type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01",
             "content": f"Relevant memory (~120 tokens):\n\n{SECRET_TEXT}"},
        ]})
        assert b'"tool_use"' not in result.encode()  # 这正是要单独设置 needle 的全部意义
        lines.append(result)
    return _write(os.path.join(home, ".claude", "projects", project, name), lines, mtime_age_s)


def codex_transcript(home: str, name: str = "rollout-2026-09-01T10-00-00-01a05881-aaaa.jsonl",
                     mtime_age_s: float = 7200.0) -> str:
    """一份 Codex 转录：一次真实的（非 mnemotrace）function_call，加上仅在散文中被提及的 mnemotrace_*。"""
    lines = [
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T10:00:00.000Z",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": PAD}]}}),
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T10:00:01.000Z",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text",
                                             "text": "mcp__mnemotrace__mnemotrace_recall would help here"}]}}),
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T10:00:02.000Z",
                    "payload": {"type": "function_call", "name": "exec_command",
                                "call_id": "call_1", "arguments": "{}"}}),
    ]
    return _write(os.path.join(home, ".codex", "sessions", "2026", "09", "01", name),
                  lines, mtime_age_s)


def _stamp(epoch: float) -> str:
    """watch.py 自己的时间戳格式：带时区偏移的本地时间（connectors/watch.py::_ts）。"""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _files(paths: list[str]) -> list[tuple[str, os.stat_result]]:
    return [(p, os.stat(p)) for p in paths]


# ---------------------------------------------------------------------------
# 1. 匹配器：只统计结构化调用，剥离 mcp__ 前缀
# ---------------------------------------------------------------------------


def test_matcher_counts_structural_calls_not_prose(tmp_path):
    home = str(tmp_path)
    cc = claude_transcript(home)
    cx = codex_transcript(home)

    raw = open(cc, "rb").read() + open(cx, "rb").read()
    assert raw.count(b"mnemotrace_recall") >= 4, "fixture must inflate a substring matcher"

    scan = facility.scan_transcripts(_files([cc, cx]), excludes=[])
    assert scan.calls["recall"] == 1          # 不是 4，尽管有 mcp__mnemotrace__ 前缀也不是 0
    assert sum(scan.calls.values()) == 1
    assert scan.control["Read"] == 1 and scan.control["exec_command"] == 1
    assert scan.prose_mentions >= 2           # tripwire 的分子仍然能看到散文提及


def test_strip_mcp_prefix_is_structural_not_substring():
    assert facility.strip_mcp_prefix("mcp__mnemotrace__mnemotrace_recall") == "mnemotrace_recall"
    assert facility.strip_mcp_prefix("mcp__my_server__mnemotrace_stats") == "mnemotrace_stats"
    assert facility.strip_mcp_prefix("mnemotrace_remember") == "mnemotrace_remember"
    assert facility.is_mnemotrace_tool("mcp__mnemotrace__mnemotrace_recall")
    assert not facility.is_mnemotrace_tool("Bash")


def test_self_development_calls_are_excluded_and_counted(tmp_path):
    home = str(tmp_path)
    cc = claude_transcript(home, project="-Users-nobody-super-memory")
    scan = facility.scan_transcripts(_files([cc]), excludes=["super-memory"])
    assert scan.calls == {} and scan.excluded_selfdev_calls == 1


# ---------------------------------------------------------------------------
# 2. reach：tool_result 的关联取决于预过滤 needle 自身
# ---------------------------------------------------------------------------


def test_recall_reach_joins_tool_results(tmp_path):
    home = str(tmp_path)
    cc = claude_transcript(home)
    scan = facility.scan_transcripts(_files([cc]), excludes=[])
    assert scan.recall_calls == 1
    assert scan.recall_non_empty == 1


def test_recall_reach_is_zero_without_the_tool_result_needle(tmp_path, monkeypatch):
    """从预过滤中去掉 `"tool_result"` 会让 reach 崩塌——这正是 needle 集合必须固定的原因。"""
    home = str(tmp_path)
    cc = claude_transcript(home)
    # 只 patch needle 集合本身。如果连 `_tool_results` 也一并 stub 掉，这个测试在 needle 失效的
    # 构建上同样能通过，而那正是它要捕捉的故障。
    monkeypatch.setattr(facility, "_CALL_NEEDLES",
                        tuple(n for n in facility._CALL_NEEDLES if n != b'"tool_result"'))
    scan = facility.scan_transcripts(_files([cc]), excludes=[])
    assert scan.recall_calls == 1 and scan.recall_non_empty == 0


def codex_mnemotrace_transcript(home: str, name: str = "rollout-2026-09-01T11-00-00-01a05881-bbbb.jsonl",
                            call_id: str = "call_mnemotrace_1", empty: bool = False,
                            mtime_age_s: float = 7200.0) -> str:
    """一份 mnemotrace_recall 被调用且被应答的 Codex 转录。

    应答以独立的 `function_call_output` payload 行返回，外层还包着 Codex 的
    "Wall time: N seconds\nOutput:\n" 前导——Claude 的 `tool_result` 关联逻辑看不到这种形态。
    """
    body = (facility._EMPTY_RECALL if empty
            else "Relevant memory (~200 tokens):\n\n" + SECRET_TEXT)
    lines = [
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T11:00:00.000Z",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": PAD}]}}),
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T11:00:01.000Z",
                    "payload": {"type": "function_call", "name": "mcp__mnemotrace__mnemotrace_recall",
                                "call_id": call_id, "arguments": "{}"}}),
        json.dumps({"type": "response_item", "timestamp": "2026-09-01T11:00:02.000Z",
                    "payload": {"type": "function_call_output", "call_id": call_id,
                                "output": "Wall time: 0.2139 seconds\nOutput:\n" + body}}),
    ]
    out = _write(os.path.join(home, ".codex", "sessions", "2026", "09", "01", name),
                 lines, mtime_age_s)
    raw = open(out, "rb").read()
    # 这种形态之所以需要专属 needle 的全部原因：所有 call needle 都不会出现在其中。
    assert b'"function_call_output"' in raw and b'"tool_result"' not in raw
    return out


def test_codex_recall_is_joined_to_its_own_result_row(tmp_path):
    """Codex 的 recall 必须能算作 REACH，而不只是落进分母。

    没有 `function_call_output` 关联时，Codex 的 recall 只会被计为一次调用、永远无法计为已应答，
    reach 就会按 Codex 使用记忆的比例成比例地偏低。在 owner 的语料上曾打印出 13/50 = 26%，
    而真相是 50/50 = 100%。
    """
    home = str(tmp_path)
    cx = codex_mnemotrace_transcript(home)
    scan = facility.scan_transcripts(_files([cx]), excludes=[])
    assert scan.calls["recall"] == 1
    assert (scan.recall_calls, scan.recall_non_empty) == (1, 1)


def test_codex_empty_recall_is_not_reached_through_the_wall_time_preamble(tmp_path):
    """必须剥掉前导文本，否则每个 Codex recall 仅凭前导长度就能通过。"""
    home = str(tmp_path)
    cx = codex_mnemotrace_transcript(home, empty=True)
    scan = facility.scan_transcripts(_files([cx]), excludes=[])
    assert (scan.recall_calls, scan.recall_non_empty) == (1, 0)


def test_a_resumed_transcript_replays_calls_and_must_not_recount_them(tmp_path):
    """恢复（resume）会话会写入一个新文件，把旧的 tool_use 块连同 id 一起重放。

    把重放也计数会虚报从未发生的读取。在 owner 语料上实测：182 次原始 mnemotrace 调用中只有 110 次
    是不同的——L2（正在成长的档位）被高估了 65%。
    """
    home = str(tmp_path)
    first = claude_transcript(home)
    resumed = claude_transcript(home, name="bbbbbbbb-1111-2222-3333-444444444444.jsonl")
    assert b"toolu_01" in open(resumed, "rb").read()   # 相同的 call id：就是同一次调用被重放

    scan = facility.scan_transcripts(_files([first, resumed]), excludes=[])
    assert scan.calls["recall"] == 1, "the replay is not a second read"
    assert scan.replayed_calls == 1
    assert (scan.recall_calls, scan.recall_non_empty) == (1, 1)

    # 两个真正不同的调用仍会计数两次——防护作用于 id，而不是文件。
    other = claude_transcript(home, name="cccccccc-1111-2222-3333-444444444444.jsonl")
    with open(other, "r+", encoding="utf-8") as fh:
        body = fh.read().replace("toolu_01", "toolu_99")
        fh.seek(0), fh.write(body), fh.truncate()
    scan2 = facility.scan_transcripts(_files([first, other]), excludes=[])
    assert scan2.calls["recall"] == 2 and scan2.replayed_calls == 0


def test_empty_recall_result_does_not_count_as_reached(tmp_path):
    home = str(tmp_path)
    path = os.path.join(home, ".claude", "projects", PROJECT_DIR, CLAUDE_UUID + ".jsonl")
    _write(path, [
        _cc_row(message={"role": "user", "content": [{"type": "text", "text": PAD}]}),
        _cc_row(message={"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_01", "name": "mcp__mnemotrace__mnemotrace_recall",
             "input": {"query": "q"}}]}),
        _cc_row(type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_01",
             "content": "No relevant memory found for that query."}]}),
    ])
    scan = facility.scan_transcripts(_files([path]), excludes=[])
    assert scan.recall_calls == 1 and scan.recall_non_empty == 0


# ---------------------------------------------------------------------------
# 3-4. 来自 watch.log 的滞后，以及不能当作证据的 ledger
# ---------------------------------------------------------------------------


def _watch_log(tmp_path, fed_iso: str, key: str) -> str:
    path = str(tmp_path / "watch.log")
    with open(path, "w", encoding="utf-8") as fh:
        # 一个 --dry-run 块：列出了路径，从未真正 fed
        fh.write("2 session(s) to ingest (2 new; backlog 9):\n")
        fh.write(f"  {key}\n")
        fh.write("\n[dry-run] would send 2 session(s), 40 turn(s) — nothing was stored\n")
        # 一个被服务器拒绝的块
        fh.write("1 session(s) to ingest (1 new; backlog 9):\n")
        fh.write(f"  {key}\n")
        fh.write("2026-09-01T09:00:00+08:00 server unreachable (http://x): nothing marked seen\n")
        # 唯一真正写入了内容的块
        fh.write("1 session(s) to ingest (1 new; backlog 8):\n")
        fh.write(f"  {key}\n")
        fh.write(f"{fed_iso} fed 1 session(s) (1 new) · 1 episode(s) · 2 conclusion(s) · "
                 "0 close failure(s) · 3.0s\n")
    return path


def test_lag_only_counts_fed_terminated_blocks(tmp_path):
    home = str(tmp_path / "home")
    cc = claude_transcript(home, mtime_age_s=3600.0)
    key = f"{os.path.basename(os.path.dirname(cc))}/{os.path.basename(cc)}"
    index = {(os.path.basename(os.path.dirname(cc)), os.path.basename(cc)): cc}
    state = {"seen": {cc: os.stat(cc).st_size}}
    fed_at = os.stat(cc).st_mtime + 600
    log = _watch_log(tmp_path, _stamp(fed_at), key)

    samples = facility.parse_watch_lag(log, state, index, time.time())
    assert len(samples) == 1                      # 不是 3：dry-run 和服务器不可达的块都不是 feed
    assert 595 <= samples[0] <= 605


def test_lag_skips_a_transcript_that_grew_since_it_was_fed(tmp_path):
    home = str(tmp_path / "home")
    cc = claude_transcript(home, mtime_age_s=3600.0)
    key = f"{os.path.basename(os.path.dirname(cc))}/{os.path.basename(cc)}"
    index = {(os.path.basename(os.path.dirname(cc)), os.path.basename(cc)): cc}
    fed_at = os.stat(cc).st_mtime + 600
    log = _watch_log(tmp_path, _stamp(fed_at), key)

    stale = {"seen": {cc: os.stat(cc).st_size - 10}}  # 文件变大过：今天的 mtime 不是 ingest 时间
    assert facility.parse_watch_lag(log, stale, index, time.time()) == []


def test_hook_lag_is_read_from_the_hook_log(tmp_path):
    log = str(tmp_path / "hook.log")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "fed", "lag_s": 12.5}) + "\n")
        fh.write("2026-09-07T10:00:00+08:00 fed 1 session lag_s=31 outcomes=2\n")
        fh.write("2026-09-07T10:01:00+08:00 refused: target mismatch\n")
    assert facility.parse_hook_lag(log) == [12.5, 31.0]
    assert facility.parse_hook_lag(str(tmp_path / "missing.log")) == []


# ---------------------------------------------------------------------------
# stub 服务器 + 完整报告的夹具
# ---------------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    sessions: list[dict] = []

    def do_GET(self):  # noqa: N802
        path = self.path
        if path.startswith("/v1/stats"):
            body = {"counts": {"episodes": 3}}
        elif path.startswith("/v1/sessions"):
            body = {"sessions": self.sessions,
                    "page": {"total": len(self.sessions), "has_more": False, "next_offset": None}}
        elif "kind=outcomes&status=superseded" in path:
            body = {"counts": {}, "facts_page": {"total": 0}}
        elif "kind=attributes" in path:
            body = {"counts": {}, "facts_page": {"total": 0}}
        elif "kind=outcomes&status=live" in path:
            body = {"counts": {}, "facts_page": {"total": 5}}
        else:
            body = {"counts": {"facts_outcomes": 5}, "facts_page": {"total": 5}}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_a):
        pass


@pytest.fixture()
def stub_server():
    _Stub.sessions = [{"id": SESSION_LABEL, "facts_added": 3}]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _args(home: str, url: str, **over) -> Namespace:
    mcp = os.path.join(home, "claude.json")
    with open(mcp, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {"mnemotrace": {"env": {"MNEMOTRACE_API_URL": "http://elsewhere:8456",
                                                     "MNEMOTRACE_API_KEY": "other"}}}}, fh)
    args = Namespace(
        url=url, space_id="synthetic-space", key="k", key_file="", days=7, all=False,
        roots=f"{home}/.claude/projects,{home}/.codex/sessions",
        state=os.path.join(home, "watch_state.json"),
        watch_log=os.path.join(home, "watch.log"),
        hook_log=os.path.join(home, "hook.log"),
        quiet_seconds=900, tick_interval="30m",
        exclude_project_substr=["super-memory"], mcp_config=mcp,
        json=False, out="", no_out=True, assert_no_regress=False,
    )
    for k, v in over.items():
        setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# 4b. 在 MAX_CLOSE_FAILURES 之后写入的 `seen` 条目算 ledger-done，不算 L1-closed
# ---------------------------------------------------------------------------


def test_gave_up_session_is_ledger_done_but_not_closed(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    closed = claude_transcript(home)
    # 第二份被 watcher 放弃的转录：它出现在 `seen` 中（watch.py 在 3 次 close 失败后标记它，
    # 免得它每个 tick 都占用一个槽位），但服务器从未收到它对应的 session。
    gave_up = claude_transcript(home, name="bbbbbbbb-9999-0000-1111-222222222222.jsonl")
    with open(os.path.join(home, "watch_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"seen": {closed: os.stat(closed).st_size,
                            gave_up: os.stat(gave_up).st_size}}, fh)

    report = facility.build_report(_args(home, stub_server))
    assert report["l1"]["finished"] == 2
    assert report["l1"]["closed"] == 1            # 放弃（give-up）并不是 close 的证据
    assert report["l1"]["ledger_done"] == 2
    assert report["l1"]["ledger_disagree"] == 1
    assert report["l1"]["yield"] == 1.0           # 唯一 closed 的 session 有 facts_added > 0


# ---------------------------------------------------------------------------
# 5. 黄金输出：只有计数和比率，绝无内容
# ---------------------------------------------------------------------------


def test_report_never_prints_a_path_a_fact_or_a_session_id(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    codex_transcript(home)
    report = facility.build_report(_args(home, stub_server))
    text = facility.render(report) + "\n" + json.dumps(report, ensure_ascii=False)

    for forbidden in ("SECRETPROJ", "SECRETFACTTEXT", SESSION_LABEL, CLAUDE_UUID,
                      "rollout-", home, ".jsonl"):
        assert forbidden not in text, f"{forbidden!r} leaked into the report"
    assert "L0 exists" in text and "L4 depended on" in text
    assert "<-- DIFFERENT MEMORY" in text        # 写/读分离是高于档位的主线，不是脚注
    assert report["l3"]["useful_rate"] is None
    assert report["rungs"]["L4"] == "not_measurable"


def test_rungs_read_off_the_measured_numbers(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    report = facility.build_report(_args(home, stub_server))
    assert report["rungs"]["L0"] == "met"                  # conclusions + episodes 已存在
    assert report["rungs"]["L1"] == "not_met"              # coverage 达标，但 lag 没有样本
    assert report["rungs"]["L2"] == "not_met"              # recall 落在过去的 ISO 周里
    assert report["rungs"]["L3"] == "not_met"              # supersession 率为 0
    assert report["l3"]["supersession_rate"] == 0.0
    # 当前 ISO 周始终在序列中，因此过期的尖峰不会被误读成健康的趋势。
    assert facility._iso_week(time.time()) in report["l2"]["weeks"]


# ---------------------------------------------------------------------------
# 6. 退出码：2 = 仪器损坏，1 = facility 回归。绝不混为一谈。
# ---------------------------------------------------------------------------


def test_zero_control_calls_is_instrument_broken_not_a_result(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    report = facility.build_report(_args(home, stub_server))
    assert report["l2"]["control_total"] > 0
    code, reasons = facility.check_regression(report, previous=None)
    assert code == facility.EXIT_OK

    report["l2"]["control_total"] = 0
    code, reasons = facility.check_regression(report, previous=None)
    assert code == facility.EXIT_INSTRUMENT_BROKEN
    assert any("control" in r for r in reasons)


def test_unreachable_server_is_instrument_broken(tmp_path, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    report = facility.build_report(_args(home, "http://127.0.0.1:1"))
    code, reasons = facility.check_regression(report, previous=None)
    assert code == facility.EXIT_INSTRUMENT_BROKEN
    assert any("unreachable" in r for r in reasons)


def test_coverage_drop_over_five_points_is_a_regression(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    claude_transcript(home, name="bbbbbbbb-9999-0000-1111-222222222222.jsonl")
    report = facility.build_report(_args(home, stub_server))
    assert report["l1"]["coverage"] == 0.5

    code, reasons = facility.check_regression(report, {"l1": {"coverage": 0.54}, "rungs": {}})
    assert code == facility.EXIT_OK                       # 4 个百分点在容差带内

    code, reasons = facility.check_regression(report, {"l1": {"coverage": 0.9}, "rungs": {}})
    assert code == facility.EXIT_REGRESSED
    assert any("coverage" in r for r in reasons)

    code, reasons = facility.check_regression(report, {"rungs": {"L1": "met"}})
    assert code == facility.EXIT_REGRESSED and any("L1 dropped" in r for r in reasons)


def test_lag_doubling_and_recall_falling_to_zero_are_regressions(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    report = facility.build_report(_args(home, stub_server))
    report["l1"]["lag"]["p50_s"] = 400.0
    code, reasons = facility.check_regression(report, {"l1": {"lag": {"p50_s": 100.0}}, "rungs": {}})
    assert code == facility.EXIT_REGRESSED and any("lag p50" in r for r in reasons)

    previous = {"rungs": {}, "l2": {"weeks": {"2026-W20": {"recall": 3}}}}
    report["l1"]["lag"]["p50_s"] = None
    for week in report["l2"]["weeks"].values():
        week["recall"] = 0
    code, reasons = facility.check_regression(report, previous)
    assert code == facility.EXIT_REGRESSED and any("recall fell to 0" in r for r in reasons)


def test_bare_run_exits_zero_even_when_the_instrument_is_broken(tmp_path, monkeypatch, capsys):
    """`--assert-no-regress` 负责把审计发现转化为退出码；裸运行始终只报告。"""
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    mcp = os.path.join(home, "claude.json")
    with open(mcp, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {}}, fh)
    argv = ["--url", "http://127.0.0.1:1", "--key", "k", "--space-id", "synthetic-space", "--no-out", "--mcp-config", mcp,
            "--roots", f"{home}/.claude/projects,{home}/.codex/sessions",
            "--state", os.path.join(home, "watch_state.json"),
            "--watch-log", os.path.join(home, "watch.log"),
            "--hook-log", os.path.join(home, "hook.log")]
    assert facility.main(argv) == facility.EXIT_OK
    assert facility.main(argv + ["--assert-no-regress"]) == facility.EXIT_INSTRUMENT_BROKEN
    assert "INSTRUMENT BROKEN" in capsys.readouterr().out


def test_ledger_appends_one_line_per_run(tmp_path, stub_server, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    claude_transcript(home)
    mcp = os.path.join(home, "claude.json")
    with open(mcp, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {}}, fh)
    out = os.path.join(home, "facility.jsonl")
    argv = ["--url", stub_server, "--key", "k", "--space-id", "synthetic-space", "--out", out, "--mcp-config", mcp,
            "--roots", f"{home}/.claude/projects,{home}/.codex/sessions",
            "--state", os.path.join(home, "watch_state.json"),
            "--watch-log", os.path.join(home, "watch.log"),
            "--hook-log", os.path.join(home, "hook.log")]
    facility.main(argv)
    facility.main(argv)
    rows = [json.loads(line) for line in open(out, encoding="utf-8") if line.strip()]
    assert len(rows) == 2 and all("rungs" in r for r in rows)


def test_trend_compares_like_windows_only(tmp_path):
    """`--days 7` 的运行不能和 `--all` 的运行做差值比较：两者的度量口径不同。"""
    out = str(tmp_path / "ledger.jsonl")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"window_days": 7, "l1": {"coverage": 0.77}}) + "\n")
        fh.write(json.dumps({"window_days": None, "l1": {"coverage": 0.23}}) + "\n")
    assert facility._previous_line(out, 7)["l1"]["coverage"] == 0.77
    assert facility._previous_line(out, None)["l1"]["coverage"] == 0.23
    assert facility._previous_line(out, 30) is None
    assert facility._previous_line(str(tmp_path / "nope.jsonl"), 7) is None
