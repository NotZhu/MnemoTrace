"""这段记忆是否成了有人依赖的设施（facility）？——用 L0-L4 阶梯、基于本地信号来度量。

AGENTS.md 的 Bet D 纪律说：无法复现的数字等于不存在。这条纪律至今只对准了*准确率*；这里把它
对准另一个没人度量的问题：记忆正在被快速填充——所有者自己的机器上已有数千条 session 结论——
但可能几乎没被读过。填满一个存储不是设施；被读到、且读得有用，才是。

    python eval/facility.py --days 7 --url http://127.0.0.1:8766 --key-file ~/.mnemotrace/watch.key

所有者定义的阶梯：

    L0 exists       memory 里存有结论                                        （机器可度量）
    L1 connected    >=90% 的已结束 session 被关闭，滞后 < 60s                 （机器可度量）
    L2 read         每周 mnemotrace_recall 调用 > 0 且呈上升趋势                  （机器可度量）
    L3 useful       (i) 结论去重率 > 0  (ii) >=50% 的 recall 改变了答案
                    (i) 机器可度量；(ii) 需要人工标注，报告中标记为 NOT MEASURED
    L4 depended on  关掉一周后会促使再把它打开                                （只能靠人工判断）

为什么没有 `--judge`、没有标注队列：L3(ii) 是对"答案是否被改变"的人工判断。用 LLM judge 会把
模型放到一个二十秒离线指标的关键路径上，而且是在给同族模型的输出打分；一个没有消费者的标注
队列则是为假想未来做的抽象（AGENTS.md 「核心不变量」）。harness 只打印有用性的上限（recall 到底有没有
返回内容），并明确说明有用性本身未被度量。

PRIVACY (CONTRIBUTING)：所有者的 transcript 含同事姓名和业务细节。输出的 schema 里没有任何
字段、表格里没有任何行能容纳 fact、查询、session id、项目名或文件路径。路径只在内存中使用，
从不打印或写盘。测试只用合成夹具。

已知的不精确之处，如实陈述而不是掩盖：

* `_session_label` 把 transcript uuid 截断为 8 个十六进制字符，两个 transcript 可能撞到同一个
  session id。本机实测：2082 个 transcript 中 1 次碰撞（约 0.05%），会让 L1 覆盖率被轻微高估。
* Watcher 滞后是 tick 分辨率的：watch.log 的 `fed` 行在每个 tick 内每次 close 之后写入，所以
  慢 tick 里早期被 ingest 的 session 会被记上整个 tick 的时长。p50 以天为单位时无所谓；一旦
  滞后接近 15-45 分钟的下限就有影响了。
* 本机上 L2 只能看到 Claude Code 和 Codex 的 transcript。它是 MnemoTrace 工具使用量的下界，标记为
  "transcripts on this machine"，不是全局计数。
* 自研排除是路径子串匹配（`--exclude-project-substr`），精度上限就是目录名的精度。它移除了
  多少次调用始终会打印出来。

退出码刻意设计为三值（`--assert-no-regress`）：0 报告已产出，1 设施发生了回退，2 度量工具
本身坏了（没有 control 工具调用、server 不可达、roots 为空）。把 1 和 2 混在一起，就是一个
设施指标在还打印着零的时候悄悄死掉的原因。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace.connectors import watch  # noqa: E402
from mnemotrace.connectors.agent_sessions import _rows, _session_label, find_sessions  # noqa: E402
from mnemotrace.connectors.base import to_epoch  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8766"
DEFAULT_ROOTS = "~/.claude/projects,~/.codex/sessions"
DEFAULT_STATE = "~/.mnemotrace/watch_state.json"
DEFAULT_WATCH_LOG = "~/.mnemotrace/logs/watch.log"
DEFAULT_HOOK_LOG = "~/.mnemotrace/logs/hook.log"
DEFAULT_MCP_CONFIG = "~/.claude.json"
DEFAULT_OUT = "results/facility.jsonl"

TARGET_LAG_S = 60  # 所有者的 L1 目标
TARGET_COVERAGE = 0.90

EXIT_OK = 0
EXIT_REGRESSED = 1
EXIT_INSTRUMENT_BROKEN = 2

# 在 json.loads 之前对每行原始内容做的字节预过滤。这个集合是承重的，不是优化细节：
# `"tool_use_id"` 并不包含 `"tool_use"`（没有闭合引号），所以从这个集合里去掉 `"tool_result"`
# 会让 recall-reach 悄悄变成 0，而其他所有数字看起来都正常。
_CALL_NEEDLES = (
    b'"tool_use"',
    b'"tool_result"',
    b'"function_call"',
    b'"custom_tool_call"',
    b'"local_shell_call"',
)
_CODEX_CALL_TYPES = {"function_call", "custom_tool_call", "local_shell_call"}
# Codex 把调用的 RESULT 作为独立的 payload 行写回，用 `call_id` 关联。它的类型名不包含上面
# 的调用探针（`"function_call_output"` 不包含 `"function_call"` 这串字节——没有闭合引号），
# 所以结果读取器需要自己的这两个探针。
_CODEX_RESULT_TYPES = {"function_call_output", "custom_tool_call_output"}
_CODEX_RESULT_NEEDLES = (b'"function_call_output"', b'"custom_tool_call_output"')
# Codex 把 MCP 工具结果包成 "Wall time: N seconds\nOutput:\n<body>"（实测：本机 421 个 MCP
# 形状的输出中 421 个都是如此）。剥掉它之后，一个 empty-recall 哨兵测试就能同时服务两个
# agent，而不是每个 Codex recall 都靠前导文本长度混过去。
_CODEX_OUTPUT_WRAPPER = re.compile(r"^Wall time: [0-9.]+ seconds\n(?:Output:\n)?")
_MCP_PREFIX = re.compile(r"^mcp__[^_]+(?:_[^_]+)*?__")
# 仅解析只读历史 transcript；运行时配置/环境变量不回退到旧名称。
_TRANSCRIPT_TOOL_PREFIXES = ("mnemotrace_", "engram_")
_TRANSCRIPT_TOOL_NEEDLES = tuple(prefix.encode("ascii") for prefix in _TRANSCRIPT_TOOL_PREFIXES)

# 新工具及历史 engram_recall / engram_search 的空结果哨兵（mnemotrace/mcp/server.py）。
_EMPTY_RECALL = "No relevant memory found for that query."
_EMPTY_RECALL_CHARS = 40  # 比这更短的结果同样没有携带任何检索到的内容

_WATCH_HEADER = re.compile(r"^\d+ session\(s\) to ingest")
_WATCH_FED = re.compile(r"^(\S+) fed \d+ session\(s\)")


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> Optional[float]:
    """最近秩百分位数。空样本返回 None，而不是硬造一个 0。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(math.ceil(q * len(ordered))) - 1))
    return ordered[idx]


def _iso_week(ts: float) -> str:
    y, w, _ = datetime.fromtimestamp(ts).isocalendar()
    return f"{y}-W{w:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _fmt_secs(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


# ---------------------------------------------------------------------------
# server（只做无内容的读取）
# ---------------------------------------------------------------------------


def _get(url: str, key: str, path: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url.rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {key}"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServerFacts:
    """harness 需要从写入目标获取的一切——只要计数，绝不取 fact 文本。

    /v1/memories 上的 `facts_limit=0` 不是装饰：任何正的 limit 都会返回存储 fact 的*文本*，
    而那正是绝不能进入本 harness 的内存、输出或台账的东西。
    """

    def __init__(self) -> None:
        self.reachable = False
        self.error = ""
        self.episodes = 0
        self.conclusions = 0
        self.outcomes_live = 0
        self.outcomes_live_incl_sensitive = 0
        self.outcomes_superseded = 0
        self.attribute_facts = 0
        self.session_facts: dict[str, int] = {}

    @classmethod
    def load(cls, url: str, key: str) -> "ServerFacts":
        self = cls()
        try:
            stats = _get(url, key, "/v1/stats")
            counts = stats.get("counts") or {}
            self.episodes = int(counts.get("episodes") or 0)

            base = "/v1/memories?facts_limit=0&episodes_limit=0"
            mem = _get(url, key, base)
            self.conclusions = int((mem.get("counts") or {}).get("facts_outcomes") or 0)
            self.outcomes_live_incl_sensitive = self.conclusions
            live = _get(url, key, base + "&kind=outcomes&status=live")
            self.outcomes_live = int((live.get("facts_page") or {}).get("total") or 0)
            sup = _get(url, key, base + "&kind=outcomes&status=superseded")
            self.outcomes_superseded = int((sup.get("facts_page") or {}).get("total") or 0)
            attrs = _get(url, key, base + "&kind=attributes&status=live")
            self.attribute_facts = int((attrs.get("facts_page") or {}).get("total") or 0)

            offset = 0
            while True:
                page = _get(url, key, f"/v1/sessions?limit=500&offset={offset}")
                rows = page.get("sessions") or []
                for row in rows:
                    sid = row.get("id")
                    if isinstance(sid, str):
                        self.session_facts[sid] = int(row.get("facts_added") or 0)
                info = page.get("page") or {}
                nxt = info.get("next_offset")
                if not info.get("has_more") or nxt is None or not rows:
                    break
                offset = int(nxt)
            self.reachable = True
        except Exception as exc:  # noqa: BLE001 — server 不可达应走退出码 2，而不是崩溃
            self.error = f"{type(exc).__name__}: {exc}"
        return self


# ---------------------------------------------------------------------------
# transcript 扫描
# ---------------------------------------------------------------------------


class Scan:
    """对窗口内的 transcript 扫一遍，产出 L1 和 L2 的原始素材。"""

    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0
        self.seconds = 0.0
        self.calls: Counter = Counter()               # 记忆工具名 -> 次数
        self.calls_by_week: dict[str, Counter] = defaultdict(Counter)
        self.control: Counter = Counter()             # 非记忆工具名 -> 次数
        self.excluded_selfdev_calls = 0
        self.prose_mentions = 0
        self.subagent_files = 0
        self.recall_calls = 0
        self.recall_non_empty = 0
        # 恢复会话会写出一个新的 transcript 文件，重放全部历史——包括 tool_use 块及其 id。
        # 把重放也算进去会报告从未发生过的读取：本语料实测，182 次原始 engram 调用里只有 110
        # 次是独立的，全部虚增都落在 L2 上——这正是所有者想增长的那个阶梯。
        self.seen_call_ids: set[str] = set()
        self.replayed_calls = 0


def _classify(path: str) -> str:
    """claude_code 还是 codex。按来源拆分是因为只有一方有会话结束事件：
    Codex 的 hooks.json 没有 `session_end` 槽位，所以它那半边语料只能靠 watcher 关闭。"""
    return "codex" if (f"{os.sep}.codex{os.sep}" in path
                       or os.path.basename(path).startswith("rollout-")) else "claude_code"


def _tool_calls(data: bytes) -> Iterable[tuple[str, Optional[str], Optional[float]]]:
    """产出一个 transcript 中每个结构性工具调用的 (tool_name, call_id, timestamp)。

    这里直接读 `message.content`，而不是经由 agent_sessions._text_of，因为那个辅助函数的
    `_SKIP_BLOCKS` 会刻意丢弃 `tool_use` 和 `tool_result`——对 ingest 是正确选择（工具机制
    不是记忆），在这里却恰好错了：工具调用本身就是信号。
    """
    for line in data.splitlines():
        if not any(needle in line for needle in _CALL_NEEDLES):
            continue
        for row in _rows(line.decode("utf-8", "replace")):
            ts = to_epoch(row.get("timestamp"))
            message = row.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name")
                        if isinstance(name, str):
                            yield name, block.get("id"), ts
            payload = row.get("payload")
            if isinstance(payload, dict) and payload.get("type") in _CODEX_CALL_TYPES:
                name = payload.get("name")
                if isinstance(name, str):
                    yield name, payload.get("call_id"), ts


def _tool_results(data: bytes) -> dict[str, str]:
    """call id -> 结果文本，用于 recall-reach 关联。两种 agent 的形状都要，不只是 Claude 的。

    门控使用与 `_tool_calls` 相同的 `_CALL_NEEDLES` 集合（外加 Codex 的结果类型名——上面的
    探针匹配不到它们），而不是私有复制一份 `b'"tool_result"'`。如果私有复制，上面那个
    `"tool_result"` 条目就成了摆设——删掉它不改变任何数字——于是"它承重"的注释和声称证明
    这一点的测试，描述的都是一种并不存在的耦合。

    只读 Claude 的形状不是少了个体贴的功能，而是捏造数字：Codex 的 `engram_recall` 也是
    结构性调用，会进入 reach 的分母，但没有关联就永远到不了分子——reach 于是按 Codex 使用
    记忆的比例精确地偏低。本关联存在之前本机实测：26%（13/50），而真实值是 100%（50/50）。
    """
    out: dict[str, str] = {}
    for line in data.splitlines():
        if not any(needle in line for needle in _CALL_NEEDLES) \
                and not any(needle in line for needle in _CODEX_RESULT_NEEDLES):
            continue
        for row in _rows(line.decode("utf-8", "replace")):
            payload = row.get("payload")
            if isinstance(payload, dict) and payload.get("type") in _CODEX_RESULT_TYPES:
                cid = payload.get("call_id")
                body = payload.get("output")
                if isinstance(cid, str):
                    if not isinstance(body, str):
                        body = "" if body is None else json.dumps(body, ensure_ascii=False)
                    out[cid] = _CODEX_OUTPUT_WRAPPER.sub("", body)
            message = row.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tid = block.get("tool_use_id")
                if not isinstance(tid, str):
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") for b in content
                                        if isinstance(b, dict) and isinstance(b.get("text"), str))
                out[tid] = content if isinstance(content, str) else ""
    return out


def strip_mcp_prefix(name: str) -> str:
    """移除 MCP 服务前缀；支持新工具名和只读历史 transcript 中的旧工具名。"""
    return _MCP_PREFIX.sub("", name)


def is_mnemotrace_tool(name: str) -> bool:
    return strip_mcp_prefix(name).startswith(_TRANSCRIPT_TOOL_PREFIXES)


def scan_transcripts(files: list[tuple[str, os.stat_result]], excludes: list[str]) -> Scan:
    """统计窗口内 transcript 中的 MnemoTrace 工具调用（以及一个对照集）。

    `agent-*.jsonl` 子 agent 的 transcript 在这里计入、但在 L1 中排除：子 agent 调用
    mnemotrace_recall（含历史旧名）是对记忆的真实读取，但 watcher 永远不会关闭这样的会话。
    """
    scan = Scan()
    started = time.time()
    for path, st in files:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        scan.files += 1
        scan.bytes += len(data)
        if os.path.basename(path).startswith("agent-"):
            scan.subagent_files += 1
        if any(needle in data for needle in _TRANSCRIPT_TOOL_NEEDLES):
            # 同一行提及新旧工具只计一次；正文与结构性计数的差是匹配器过期的绊线。
            scan.prose_mentions += sum(
                1 for line in data.splitlines()
                if any(needle in line for needle in _TRANSCRIPT_TOOL_NEEDLES)
            )
        low = path.lower()
        excluded = any(sub in low for sub in excludes)

        results: Optional[dict[str, str]] = None
        for name, call_id, ts in _tool_calls(data):
            short = strip_mcp_prefix(name)
            if not short.startswith(_TRANSCRIPT_TOOL_PREFIXES):
                # 对照行统计覆盖每一个被扫描的文件，而不只是提到 engram 的文件：如果对照集
                # 共享匹配器的盲区，它会和坏掉的匹配器一样归零，也就不再是绊线。
                scan.control[short] += 1
                continue
            if call_id:
                # 在两个 agent 中 call id 对每次真实调用都是唯一的（实测：本语料 182 次
                # engram 调用中 0 次缺失），所以重复的 id 一定是重放，绝不是第二次读取。
                if call_id in scan.seen_call_ids:
                    scan.replayed_calls += 1
                    continue
                scan.seen_call_ids.add(call_id)
            if excluded:
                scan.excluded_selfdev_calls += 1
                continue
            tool = next(short[len(prefix):] for prefix in _TRANSCRIPT_TOOL_PREFIXES
                        if short.startswith(prefix))
            scan.calls[tool] += 1
            scan.calls_by_week[_iso_week(ts or st.st_mtime)][tool] += 1
            if tool not in ("recall", "search"):
                continue
            # Reach：这次读取到底有没有返回内容？它是有用性的上限，绝不是有用性本身。
            scan.recall_calls += 1
            if results is None:
                results = _tool_results(data)
            body = (results.get(call_id or "") or "").strip()
            if body and not body.startswith(_EMPTY_RECALL) and len(body) >= _EMPTY_RECALL_CHARS:
                scan.recall_non_empty += 1
    scan.seconds = round(time.time() - started, 1)
    return scan


# ---------------------------------------------------------------------------
# L1：会话关闭覆盖率与滞后
# ---------------------------------------------------------------------------


def finished_sessions(roots: list[str], since: Optional[float], quiet_seconds: int,
                      now: float) -> list[tuple[str, os.stat_result]]:
    """已进入静默、符合 ingest 条件的 transcript——L1 的分母。

    刻意使用 `find_sessions()` 的资格条件（>=2048 字节、非 `agent-*`）加上静默检查，即恰好是
    `watch.pending_sessions` 去掉台账检查，这样分母与 watcher 对"什么是一个会话"永远不会分歧。
    """
    out = []
    for root in roots:
        for path in find_sessions(root=root, since=since):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if now - st.st_mtime < quiet_seconds:
                continue
            out.append((path, st))
    return out


def parse_watch_lag(log_path: str, state: dict, index: dict[tuple[str, str], str],
                    now: float) -> list[float]:
    """来自 watch.log 的滞后样本：close 返回时刻减去 transcript 的 mtime。

    一个块由无时间戳的 `N session(s) to ingest` 头、每个路径一行缩进的 `<dir>/<file>`、以带
    时间戳的 `... fed ...` 行结尾构成。只有以 `fed` 结尾的块才算数——日志里 `server unreachable`
    行和头部远多于 `fed` 行，给未终止的块记账会为从未存储过的 session 报告滞后。
    """
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    seen = state.get("seen") or {}
    samples: list[float] = []
    block: list[str] = []
    in_block = False
    for line in lines:
        if _WATCH_HEADER.match(line):
            block, in_block = [], True
            continue
        if in_block and line.startswith("  ") and "/" in line:
            block.append(line.strip().rstrip(" +"))
            continue
        fed = _WATCH_FED.match(line)
        if in_block and fed:
            fed_at = to_epoch(fed.group(1))
            if fed_at is not None:
                for key in block:
                    dirname, _, basename = key.rpartition("/")
                    path = index.get((dirname, basename))
                    if not path:
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    # 只采样自那之后文件未变过的：台账记录了这个精确大小，这正是今天的
                    # mtime 等于 ingest 时刻 mtime 的依据。
                    if seen.get(path) != st.st_size:
                        continue
                    lag = fed_at - st.st_mtime
                    if lag > 0:
                        samples.append(lag)
        block, in_block = [], False
    return samples


def parse_hook_lag(log_path: str) -> list[float]:
    """来自 hook.log 的滞后样本，它自带 `lag_s`（hook 同时知道两个时间戳）。

    读取 hook 自己记录的数字而不是重新推导，使得在 hook 喂入的那半边分布里，transcript 路径
    完全不进入本 harness。
    """
    samples: list[float] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    for line in lines:
        value: Optional[float] = None
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                row = json.loads(stripped)
                if isinstance(row, dict) and row.get("lag_s") is not None:
                    value = float(row["lag_s"])
            except (ValueError, TypeError):
                value = None
        if value is None:
            m = re.search(r"lag_s[=:\s]+([0-9]+(?:\.[0-9]+)?)", line)
            if m:
                value = float(m.group(1))
        if value is not None and value > 0:
            samples.append(value)
    return samples


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _read_target(mcp_config: str) -> tuple[str, str]:
    """所有者的 agent 实际从哪里 READ 的 URL+key。只读；本 harness 绝不修改它。"""
    try:
        with open(os.path.expanduser(mcp_config), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "", ""
    server = ((data.get("mcpServers") or {}).get("mnemotrace") or {})
    env = server.get("env") or {}
    return str(env.get("MNEMOTRACE_API_URL") or ""), str(env.get("MNEMOTRACE_API_KEY") or "")


def _previous_line(out_path: str, window_days: Optional[int]) -> Optional[dict]:
    """同一窗口最近的一条台账记录。

    拿 `--days 7` 的运行和 `--all` 的运行比较，每次窗口变化都会报出覆盖率悬崖：覆盖率在排水
    前沿之上约 100%、之下约 0%，两个窗口不是同一种测量，绝不能做差。
    """
    last = None
    try:
        with open(out_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("window_days") == window_days:
                    last = row
    except OSError:
        return None
    return last


def build_report(args) -> dict:
    now = time.time()
    roots = [os.path.expanduser(r.strip()) for r in args.roots.split(",") if r.strip()]
    roots = [r for r in roots if os.path.isdir(r)]
    since = None if args.all else now - args.days * 86400
    excludes = [s.lower() for s in (args.exclude_project_substr or [])]

    key = args.key or ""
    if not key and args.key_file:
        try:
            with open(os.path.expanduser(args.key_file), encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError:
            key = ""
    if not key:
        key = os.environ.get("MNEMOTRACE_API_KEY", "")

    server = ServerFacts.load(args.url, key)
    state = watch._load_state(os.path.expanduser(args.state))

    # --- L1 -----------------------------------------------------------------
    finished = finished_sessions(roots, since, args.quiet_seconds, now)
    by_source: dict[str, dict] = {"claude_code": {"finished": 0, "closed": 0},
                                  "codex": {"finished": 0, "closed": 0}}
    closed = distilled = 0
    for path, _st in finished:
        source = _classify(path)
        by_source.setdefault(source, {"finished": 0, "closed": 0})
        by_source[source]["finished"] += 1
        label = _session_label(path)
        if label in server.session_facts:
            closed += 1
            by_source[source]["closed"] += 1
            if server.session_facts[label] > 0:
                distilled += 1
    for row in by_source.values():
        row["coverage"] = _rate(row["closed"], row["finished"])

    # watcher 自己眼中还欠着什么，所以 `claims` 必须按 run_once 的方式折算进去：SessionEnd
    # hook 已经喂过的 transcript 不是积压，只要 claims 文件还没折算，把它算进去就会高估排水
    # ETA。注意这里始终是 watcher 的默认 roots（pending_sessions 不接受 root 参数），不是
    # --roots。
    backlog = len(watch.pending_sessions(state, quiet_seconds=args.quiet_seconds, now=now,
                                         claims=watch.load_claims(watch.DEFAULT_CLAIMS)))
    tick_s = watch._parse_duration(args.tick_interval)
    limit = watch.DEFAULT_LIMIT
    # 是下限，不是预测：它假设每个 tick 都能吃满 `limit` 个全新 session。日志并非如此——
    # 本机积压在 39 个 tick 内从 2064 降到 1596，净速度约 12/tick，因为每个 tick 都要花一部分
    # 预算重新喂增长过的 transcript，而新 session 还在源源到来。因此用 ">=" 打印；更紧的数字
    # 都会与 watch.log 矛盾。
    ticks = -(-backlog // limit) if backlog else 0
    drain_eta_h = round(ticks * tick_s / 3600.0, 1)

    # 台账只是声称，不是证据（MAX_CLOSE_FAILURES 之后的放弃也会被记录）。打印它声称的与
    # server 实际持有的之间的差值。
    seen = state.get("seen") or {}
    ledger_done = ledger_disagree = 0
    for path, size in seen.items():
        try:
            if os.stat(path).st_size != size:
                continue
        except OSError:
            continue
        ledger_done += 1
        if _session_label(path) not in server.session_facts:
            ledger_disagree += 1

    # watch.log 把每个路径记为 `<父目录名>/<basename>`——足以关联回完整路径，这也是本
    # harness 唯一需要接触路径字符串的原因。索引从 --roots 构建，以便能用夹具树来测试关联。
    index = {(os.path.basename(os.path.dirname(p)), os.path.basename(p)): p
             for root in roots for p in find_sessions(root=root)}
    watch_lag = parse_watch_lag(os.path.expanduser(args.watch_log), state, index, now)
    hook_lag = parse_hook_lag(os.path.expanduser(args.hook_log))
    all_lag = watch_lag + hook_lag

    # --- L2 -----------------------------------------------------------------
    scan_files: list[tuple[str, os.stat_result]] = []
    for root in roots:
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if since is not None and st.st_mtime < since:
                    continue
                scan_files.append((path, st))
    scan = scan_transcripts(scan_files, excludes)

    # 当前 ISO 周始终存在，没有调用就补零。"每周 recall > 0 且呈上升趋势"是对"现在"的断言；
    # 如果让序列终止在碰巧有调用的最后一周，六月的尖峰就会被永远读成健康趋势。
    scan.calls_by_week.setdefault(_iso_week(now), Counter())
    weeks = {week: {"recall": c.get("recall", 0), "remember": c.get("remember", 0),
                    "close_session": c.get("close_session", 0),
                    "other": sum(v for k, v in c.items()
                                 if k not in ("recall", "remember", "close_session"))}
             for week, c in sorted(scan.calls_by_week.items())}

    # --- 各阶梯 --------------------------------------------------------------
    coverage = _rate(closed, len(finished))
    lag_p50 = _pct(all_lag, 0.5)
    recall_week_counts = [w["recall"] for _k, w in sorted(weeks.items())]
    l2_met = bool(recall_week_counts) and recall_week_counts[-1] > 0 and (
        len(recall_week_counts) < 2 or recall_week_counts[-1] >= recall_week_counts[-2])
    supersession_rate = _rate(server.outcomes_superseded,
                              server.outcomes_superseded + server.outcomes_live)
    rungs = {
        "L0": "met" if server.conclusions > 0 and server.episodes > 0 else "not_met",
        "L1": "met" if (coverage >= TARGET_COVERAGE and lag_p50 is not None
                        and lag_p50 < TARGET_LAG_S) else "not_met",
        "L2": "met" if l2_met else "not_met",
        # L3 需要两半同时满足；(ii) 没有机器代理指标，所以这个阶梯在这里永远读不出 "met"。
        "L3": "not_met" if supersession_rate == 0 else "not_measurable",
        "L4": "not_measurable",
    }

    read_url, read_key = _read_target(args.mcp_config)
    last_result = state.get("last_result") or {}
    last_fed_at = last_result.get("at")

    return {
        "run_id": _now_iso(),
        "window_days": None if args.all else args.days,
        "scan": {"files": scan.files, "bytes": scan.bytes, "seconds": scan.seconds},
        "targets": {
            "write_url": args.url,
            "write_target": watch._target(args.url, key),
            "read_url": read_url,
            "read_target": watch._target(read_url, read_key) if read_url else "",
            "same_memory": bool(read_url) and read_url == args.url and read_key == key,
        },
        "rungs": rungs,
        "l0": {"episodes": server.episodes, "conclusions": server.conclusions,
               "fed_sessions": len(server.session_facts),
               "last_fed_age_h": round((now - last_fed_at) / 3600.0, 1) if last_fed_at else None},
        "l1": {
            "by_source": by_source,
            "finished": len(finished), "closed": closed, "coverage": coverage,
            "distilled": distilled, "yield": _rate(distilled, closed),
            "backlog": backlog, "drain_eta_h": drain_eta_h,
            "ledger_done": ledger_done, "ledger_disagree": ledger_disagree,
            "lag": {
                "n": len(all_lag), "p50_s": _pct(all_lag, 0.5), "p95_s": _pct(all_lag, 0.95),
                "min_s": min(all_lag) if all_lag else None,
                "by_source": {
                    "watch": {"n": len(watch_lag), "p50_s": _pct(watch_lag, 0.5)},
                    "hook": {"n": len(hook_lag), "p50_s": _pct(hook_lag, 0.5)},
                },
            },
            "lag_floor_watch_s": [args.quiet_seconds, args.quiet_seconds + tick_s],
            "target_lag_s": TARGET_LAG_S,
            "target_lag_reachable_watch": False,  # 仅 QUIET_SECONDS 一项就是 60s 目标的 15 倍
            "target_lag_reachable_hook": True,
        },
        "l2": {
            "weeks": weeks,
            "calls_total": int(sum(scan.calls.values())),
            "by_tool": dict(scan.calls.most_common()),
            "control": dict(scan.control.most_common(3)),
            "control_total": int(sum(scan.control.values())),
            "control_distinct": len(scan.control),
            "excluded_selfdev_calls": scan.excluded_selfdev_calls,
            "replayed_calls": scan.replayed_calls,
            "subagent_files_included": scan.subagent_files > 0,
            "prose_mentions": scan.prose_mentions,
        },
        "l3": {
            "outcomes_live": server.outcomes_live,
            "outcomes_live_incl_sensitive": server.outcomes_live_incl_sensitive,
            "outcomes_superseded": server.outcomes_superseded,
            "supersession_rate": supersession_rate,
            "attribute_facts": server.attribute_facts,
            "recall_calls": scan.recall_calls,
            "recall_non_empty": scan.recall_non_empty,
            "recall_reach": _rate(scan.recall_non_empty, scan.recall_calls),
            "useful_rate": None,
            "useful_note": "not measured — needs human labelling; no machine proxy",
        },
        "l4": {"measurable": False, "last_ab": None},
        "_instrument": {"server_reachable": server.reachable, "server_error": server.error,
                        "roots": len(roots)},
    }


def render(report: dict) -> str:
    t, l0, l1, l2, l3 = (report["targets"], report["l0"], report["l1"], report["l2"], report["l3"])
    lag = l1["lag"]
    window = "all time" if report["window_days"] is None else f"{report['window_days']}d window"
    lines = []
    lines.append(f"mnemotrace facility · {report['run_id']} · {window} · "
                 f"scan {report['scan']['files']} files / "
                 f"{report['scan']['bytes'] / 1e9:.2f} GB / {report['scan']['seconds']}s")
    marker = "" if t["same_memory"] else "   <-- DIFFERENT MEMORY"
    lines.append(f"  write target : {t['write_url']} [{t['write_target']}]")
    lines.append(f"  read  target : {t['read_url'] or '(no mnemotrace MCP server configured)'} "
                 f"[{t['read_target']}]{marker}")
    lines.append("")
    lines.append(f"L0 exists      {report['rungs']['L0'].upper():<14} "
                 f"{l0['conclusions']} conclusions · {l0['episodes']} episodes · "
                 f"{l0['fed_sessions']} fed sessions · last fed "
                 f"{'never' if l0['last_fed_age_h'] is None else str(l0['last_fed_age_h']) + 'h ago'}")
    src = " · ".join(f"{name} {row['closed']}/{row['finished']} ({row['coverage'] * 100:.1f}%)"
                     for name, row in sorted(l1["by_source"].items()))
    lines.append(f"L1 connected   {report['rungs']['L1'].upper():<14} "
                 f"coverage {l1['closed']}/{l1['finished']} = {l1['coverage'] * 100:.1f}% "
                 f"(goal {TARGET_COVERAGE * 100:.0f}%)")
    lines.append(f"                             by source: {src}")
    lines.append(f"                             yield {l1['distilled']}/{l1['closed']} = "
                 f"{l1['yield'] * 100:.1f}% distilled · backlog {l1['backlog']} "
                 f"(drain ETA >={l1['drain_eta_h']}h at {watch.DEFAULT_LIMIT}/tick; observed "
                 f"net drain is slower) · ledger {l1['ledger_done']} done, "
                 f"{l1['ledger_disagree']} disagree")
    lines.append(f"                             lag n={lag['n']} min {_fmt_secs(lag['min_s'])} "
                 f"p50 {_fmt_secs(lag['p50_s'])} p95 {_fmt_secs(lag['p95_s'])} "
                 f"(goal {TARGET_LAG_S}s)")
    lines.append(f"                             watch n={lag['by_source']['watch']['n']} "
                 f"p50 {_fmt_secs(lag['by_source']['watch']['p50_s'])} · "
                 f"hook n={lag['by_source']['hook']['n']} "
                 f"p50 {_fmt_secs(lag['by_source']['hook']['p50_s'])} · "
                 f"watcher floor {_fmt_secs(l1['lag_floor_watch_s'][0])}-"
                 f"{_fmt_secs(l1['lag_floor_watch_s'][1])} → 60s reachable: "
                 f"watch={l1['target_lag_reachable_watch']} hook={l1['target_lag_reachable_hook']}")
    weeks = " · ".join(f"{w} recall {c['recall']}/remember {c['remember']}/close {c['close_session']}"
                       f"/other {c['other']}" for w, c in sorted(l2["weeks"].items())) or "(none)"
    lines.append(f"L2 read        {report['rungs']['L2'].upper():<14} "
                 f"{l2['calls_total']} MnemoTrace call(s) in transcripts on this machine "
                 f"(lower bound; {l2['excluded_selfdev_calls']} self-dev, "
                 f"{l2.get('replayed_calls', 0)} resume-replay calls excluded)")
    lines.append(f"                             {weeks}")
    ctrl = " · ".join(f"{k} {v}" for k, v in l2["control"].items()) or "(none)"
    lines.append(f"                             control: {ctrl} "
                 f"({l2['control_total']} calls, {l2['control_distinct']} distinct tools)")
    if l2["prose_mentions"] and not l2["calls_total"] and not l2["excluded_selfdev_calls"]:
        lines.append(f"                             !! matcher may be stale: {l2['prose_mentions']} "
                     "lines mention mnemotrace_* or historical engram_* but 0 structural calls")
    lines.append(f"L3 useful      {report['rungs']['L3'].upper():<14} "
                 f"supersession {l3['outcomes_superseded']}/{l3['outcomes_live']} = "
                 f"{l3['supersession_rate'] * 100:.1f}% "
                 f"({l3['outcomes_live_incl_sensitive']} incl. sensitive) · "
                 f"attribute facts {l3['attribute_facts']}")
    if l3["supersession_rate"] == 0:
        # 只在比率确实为 0 时出现：一条比它要解释的数字活得更久的硬编码解释，比没有解释更糟，
        # 因为它等于告诉读者不用再查了。
        lines.append("                             reason: conclusions are keyed subject=session_id, so "
                     "no two ever contend")
        lines.append("                                     for a (subject, predicate) slot — 0% is a "
                     "design consequence, not a tuning failure")
    lines.append(f"                             recall reach {l3['recall_non_empty']}/"
                 f"{l3['recall_calls']} = {l3['recall_reach'] * 100:.0f}% "
                 "(a CEILING on usefulness, not usefulness)")
    lines.append(f"                             useful_rate: {l3['useful_note']}")
    lines.append(f"L4 depended on {report['rungs']['L4'].upper():<14} "
                 "not measurable by machine — needs an off/on week; last A/B: never")
    return "\n".join(lines)


def check_regression(report: dict, previous: Optional[dict]) -> tuple[int, list[str]]:
    """退出码 2 = 度量工具坏了；退出码 1 = 设施回退了。绝不能混为一谈。"""
    broken = []
    if not report["_instrument"]["server_reachable"]:
        broken.append("server unreachable")
    if not report["_instrument"]["roots"]:
        broken.append("no transcript roots found")
    if report["l2"]["control_total"] == 0:
        broken.append("control tool count is 0 — the tool-call matcher is not seeing anything")
    if broken:
        return EXIT_INSTRUMENT_BROKEN, broken

    if not previous:
        return EXIT_OK, []
    regressed = []
    prev_cov = (previous.get("l1") or {}).get("coverage")
    if isinstance(prev_cov, (int, float)) and report["l1"]["coverage"] < prev_cov - 0.05:
        regressed.append(f"L1 coverage {prev_cov * 100:.1f}% -> {report['l1']['coverage'] * 100:.1f}%")
    prev_lag = ((previous.get("l1") or {}).get("lag") or {}).get("p50_s")
    now_lag = report["l1"]["lag"]["p50_s"]
    if isinstance(prev_lag, (int, float)) and prev_lag > 0 and isinstance(now_lag, (int, float)) \
            and now_lag > 2 * prev_lag:
        regressed.append(f"lag p50 {_fmt_secs(prev_lag)} -> {_fmt_secs(now_lag)}")
    for rung, value in report["rungs"].items():
        if (previous.get("rungs") or {}).get(rung) == "met" and value != "met":
            regressed.append(f"{rung} dropped from met")
    prev_recall = sum(w.get("recall", 0) for w in ((previous.get("l2") or {}).get("weeks") or {}).values())
    now_recall = sum(w["recall"] for w in report["l2"]["weeks"].values())
    if prev_recall > 0 and now_recall == 0:
        regressed.append("weekly recall fell to 0")
    return (EXIT_REGRESSED, regressed) if regressed else (EXIT_OK, [])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("MNEMOTRACE_URL") or DEFAULT_URL,
                    help="write target (the server the watcher/hook feeds)")
    ap.add_argument("--key", default="", help="API key; precedence --key > --key-file > MNEMOTRACE_API_KEY")
    ap.add_argument("--key-file", default="", help="read the API key from this file")
    ap.add_argument("--days", type=int, default=7, help="scan window in days (default 7)")
    ap.add_argument("--all", action="store_true", help="scan the whole corpus instead of a window")
    ap.add_argument("--roots", default=DEFAULT_ROOTS)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--watch-log", default=DEFAULT_WATCH_LOG)
    ap.add_argument("--hook-log", default=DEFAULT_HOOK_LOG)
    ap.add_argument("--quiet-seconds", type=int, default=watch.QUIET_SECONDS)
    ap.add_argument("--tick-interval", default=watch.DEFAULT_INTERVAL,
                    help="scheduler interval; sets the watcher's lag floor")
    ap.add_argument("--exclude-project-substr", action="append", default=None,
                    help="repeatable; transcripts whose path contains this are self-development "
                         "(default: mnemotrace)")
    ap.add_argument("--mcp-config", default=DEFAULT_MCP_CONFIG,
                    help="read-only, to report which memory the agents actually read from")
    ap.add_argument("--json", action="store_true", help="print the JSON object instead of the table")
    ap.add_argument("--out", default=DEFAULT_OUT, help="append one JSON line per run")
    ap.add_argument("--no-out", action="store_true", help="do not append to the ledger")
    ap.add_argument("--assert-no-regress", action="store_true",
                    help="exit 1 if the facility regressed, 2 if the instrument is broken")
    args = ap.parse_args(argv)
    if args.exclude_project_substr is None:
        args.exclude_project_substr = ["mnemotrace"]

    report = build_report(args)
    previous = (_previous_line(args.out, report["window_days"])
                if args.out and not args.no_out else None)
    code, reasons = check_regression(report, previous)

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(render(report))
        if reasons:
            label = "INSTRUMENT BROKEN" if code == EXIT_INSTRUMENT_BROKEN else "REGRESSED"
            print(f"\n{label}: " + "; ".join(reasons))

    if args.out and not args.no_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report, ensure_ascii=False) + "\n")

    return code if args.assert_no_regress else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
