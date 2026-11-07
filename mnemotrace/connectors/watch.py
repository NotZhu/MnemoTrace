"""Import quiet local agent transcripts into an explicitly selected memory space.

The scheduler runs the same incremental import as `--once`. SessionEnd can submit
earlier, with the same stable message IDs. The cursor advances after PostgreSQL
confirms the original sources are durable. Use `--immediate` to wait for core work.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone
from typing import Optional

from . import hook_install
from .. import _flock
from .agent_sessions import find_sessions, load_sessions

DEFAULT_STATE = os.path.expanduser("~/.mnemotrace/watch_state.json")
# SessionEnd 钩子那侧的握手：它追加到这里，本模块把它合并进 `seen`。用两个文件而非一个，是因为钩子写进
# watch_state.json 的内容若落在 tick 中间，会被该 tick 自己的保存悄悄抹掉（见 load_claims）。
DEFAULT_CLAIMS = os.path.expanduser("~/.mnemotrace/hook_claims.jsonl")
DEFAULT_URL = "http://127.0.0.1:8000"
DEFAULT_LIMIT = 25
DEFAULT_INTERVAL = "30m"

# 仍在被写入的会话是移动目标：现在导入意味着以后带着新尾巴再导入一次。等它静默一段时间再处理。
QUIET_SECONDS = 15 * 60

# 一个 tick 的 transcript 装不进一个请求。服务器默认把请求体限制在 2 MiB（DEFAULT_MAX_REQUEST_BYTES），
# 而 25 个真实会话序列化后约 18 MB，因此原先那个单次 bulk POST 被 413 拒绝 —— 又因为服务器在读完整个
# 请求体之前就关闭连接，urllib 把它呈现为 "Broken pipe"，调用方把它归类为传输失败并无限重试。在本机
# 实测：两天 115 次相同失败，什么都没导入。按字节数分块（绝不按会话数 —— 会话从 2 KB 到 2.5 MB 不等）
# 还带来失败隔离：一个被拒绝的分块不再让其余会话的导入跟着陪葬。
IMPORT_CHUNK_BYTES = 1_500_000  # 低于服务器 2 MiB 默认值，留出 JSON 开销余量

# 一个 close 一直失败的路径（模型故障、蒸馏器无法消化的 transcript）会在后续 tick 重试，但不是永远：
# 达到此次数后标记为 seen，避免一个坏文件从此占据每个 tick `--limit` 的一个名额。

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_UNREACHABLE = 75  # EX_TEMPFAIL：没有标记任何 seen，下个 tick 重试即可

_sleep = time.sleep  # 供 --every 循环测试注入

# 只读一次，让 `--install-hook` 能区分"调用方传了 --url"和"argparse 填了默认值"。
_URL_DEFAULT = os.environ.get("MNEMOTRACE_URL") or os.environ.get("MNEMOTRACE_API_URL") or DEFAULT_URL


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, path)


def _parse_duration(value: str) -> int:
    """'7d' / '12h' / '30m' -> 秒。裸数字按秒。"""
    value = (value or "").strip()
    if not value:
        raise SystemExit("expected a duration like 30m / 2h / 1d")
    unit = value[-1].lower()
    factor = {"d": 86400, "h": 3600, "m": 60}.get(unit)
    try:
        return int(float(value[:-1]) * factor) if factor else int(float(value))
    except ValueError:
        raise SystemExit(f"expected a duration like 30m / 2h / 1d, got {value!r}")


def _parse_since(value: str) -> Optional[float]:
    """接受 '7d' / '12h' / '30m'（从现在往回的窗口）或 epoch 值。"""
    if not value:
        return None
    unit = value[-1].lower()
    if unit in ("d", "h", "m"):
        return time.time() - _parse_duration(value)
    try:
        return float(value)
    except ValueError:
        raise SystemExit(f"--since expects 7d / 12h / 30m or an epoch, got {value!r}")


def load_claims(path: str, target: Optional[str] = None) -> dict[str, int]:
    """SessionEnd 钩子已喂过的 transcript 及其大小。

    钩子<->监视器握手的读侧（connectors/session_hook.py）。钩子不能写 `watch_state.json`：run_once 在
    watch.lock 下对它读-改-写，钩子的写入若落在 tick 的加载与保存之间就会被无声抹掉。钩子改为为每份喂过
    的 transcript 追加一行 JSON，在 tick 合并之前，一条 claim 的效力等同于一条 `seen` 记录。

    `target` 用于丢弃针对其他 服务器+命名空间 的 claim，理由与 run_once 在 target 变化时清空 `seen`
    相同：旧目标的记忆对这几个文件说了什么，对新目标毫无意义。
    """
    out: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # 钩子边追加我们边读会留下截断的末行；跳过，不报错
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        if target and row.get("target") and row.get("target") != target:
            continue
        try:
            out[row["path"]] = int(row.get("size") or 0)
        except (TypeError, ValueError):
            continue
    return out


def fold_claims(state: dict, path: str, target: Optional[str] = None) -> int:
    """把钩子的 claims 合并进 `seen`。必须持有 watch.lock 时调用。

    先轮转再合并，而不是读完就截断：否则在"我们读完"与"我们 unlink"之间追加的钩子会无声丢失它的 claim。
    钩子若跨轮转持有 fd 仍可能丢 —— 这是权衡中有意接受的一侧，代价是一次重复导入，由 /v1/import 的内容
    消息身份去重吸收；另一种做法的代价则是台账里一个永久的洞。
    """
    merging = path + ".merging"
    try:
        os.replace(path, merging)
    except OSError:
        return 0
    claims = load_claims(merging, target=target)
    seen = state.get("seen") or {}
    seen.update(claims)
    state["seen"] = seen
    try:
        os.unlink(merging)
    except OSError:
        pass
    return len(claims)


def _oversized_key(path: str, max_bytes: int) -> str:
    try:
        return f"{os.stat(path).st_size}:{max_bytes}"
    except OSError:
        return ""


def pending_sessions(state: dict, since: Optional[float] = None,
                     quiet_seconds: int = QUIET_SECONDS,
                     now: Optional[float] = None,
                     max_bytes: int = IMPORT_CHUNK_BYTES,
                     claims: Optional[dict[str, int]] = None) -> list[str]:
    """值得导入的 transcript：上次见过之后有变化，且已不再被写入。"""
    now = now if now is not None else time.time()
    seen = state.get("seen") or {}
    claimed = claims or {}
    out = []
    for path in find_sessions(since=since):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if now - st.st_mtime < quiet_seconds:
            continue  # 仍然活跃；现在导入只会迫使以后重导
        if seen.get(path) == st.st_size or claimed.get(path) == st.st_size:
            continue  # 上次运行后未变，或已由 SessionEnd 钩子喂过
        # 装不进一个请求的 transcript 被跳过，而不是每个 tick 都重试 —— 但记录以 (大小, 上限) 为键，
        # 因此文件变大或调高服务器上限后会被重新排队。
        if (state.get("oversized") or {}).get(path) == f"{st.st_size}:{max_bytes}":
            continue
        out.append(path)
    return out


def _row_bytes(row: dict) -> int:
    return len(json.dumps({"sessions": [row]}).encode("utf-8"))


def chunk_rows(rows: list[dict], max_bytes: int = IMPORT_CHUNK_BYTES) -> tuple[list[list[dict]], list[dict]]:
    """把导入行按请求大小分批，并单独列出完全装不下的那些。

    每个会话对应一个摄入结果。超出单次请求预算的会话单独返回，避免在每个 tick 重试相同输入。
    """
    batches: list[list[dict]] = []
    oversized: list[dict] = []
    current: list[dict] = []
    used = 0
    for row in rows:
        size = _row_bytes(row)
        if size > max_bytes:
            oversized.append(row)
            continue
        if current and used + size > max_bytes:
            batches.append(current)
            current, used = [], 0
        current.append(row)
        used += size
    if current:
        batches.append(current)
    return batches, oversized


def _post(base_url: str, api_key: str, path: str, body: dict, timeout: int) -> dict:
    import urllib.request

    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ingest(base_url: str, api_key: str, paths: list[str], timeout: int = 900,
           *, space_id: str, agent_id: str | None = None, immediate: bool = False,
           max_bytes: int = IMPORT_CHUNK_BYTES) -> dict:
    """Accept historical input and return paths confirmed durable by the server."""
    by_path: dict[str, list[str]] = {}
    sessions = []
    for p in paths:
        by_path[p] = []
        for s in load_sessions([p]):
            by_path[p].append(s.session_id)
            sessions.append(s)
    if not sessions:
        return {"ok": True, "sessions": 0, "episodes": 0, "note": "nothing conversational in those files",
                "sessions_by_path": {}}

    # Preserve source times and stable positions; local file paths are not source identities.
    rows = []
    for s in sessions:
        row: dict = {"session_id": s.session_id, "metadata": {"source": "agent_session"},
                     "messages": [{"message_id": m.message_id or f'import:{index}', "role": m.role,
                                   "content": m.content, "event_time": m.event_time}
                                  for index, m in enumerate(s.messages)]}
        if s.event_time is not None:
            row["event_time"] = s.event_time
        rows.append(row)
    batches, oversized_rows = chunk_rows(rows, max_bytes)
    oversized = [r["session_id"] for r in oversized_rows]
    sent_ids: set[str] = set()
    rejected: set[str] = set()
    response_received = False
    first_error: Optional[BaseException] = None
    result: dict = {"ok": True, "sessions": 0, "episodes": 0, "skipped": 0,
                    "facts_deferred": 0, "batches": len(batches), "import_failed": 0, "processing_failures": []}
    for batch in batches:
        payload: dict = {"sessions": batch, "space_id": space_id, "agent_id": agent_id}
        if immediate:
            payload["consolidate"] = True
        try:
            part = _post(base_url, api_key, "/v1/import", payload, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            # 一个被拒绝的批次不能连累其他批次的导入：其中会话只是不被标记 seen，下个 tick 恰好重试它们。
            # 但服务器整个宕机时每个批次都会失败，那是调用方必须看到的传输问题（exit 75，台账不动），
            # 而不是"今天什么都没导入" —— 所以当一个批次都没成功时，在下方重新抛出该错误。
            result["import_failed"] += len(batch)
            result.setdefault("import_errors", []).append(f"{len(batch)} session(s): {exc}")
            rejected.update(row["session_id"] for row in batch)
            first_error = first_error or exc
            continue
        response_received = True
        confirmed = {row['session_id'] for row in part.get('results', ())
                     if row.get('source_ids')}
        refused = set()
        for failure in part.get('failures', ()):
            if not failure.get('stored_raw'):
                refused.add(failure['session_id'])
                continue
            result['processing_failures'].append({
                'session_id': failure['session_id'],
                'code': failure['code'],
                'operation_ids': failure.get('operation_ids', []),
            })
        sent_ids.update(confirmed)
        rejected.update({row['session_id'] for row in batch} - confirmed | refused)
        result['import_failed'] += len(refused)
        for key in ("sessions", "episodes", "skipped", "facts_deferred"):
            result[key] = result.get(key, 0) + int(part.get(key) or 0)
    if batches and not response_received and first_error is not None:
        raise first_error  # 一个都没成功：交给 main 分类（传输 -> 75，HTTP -> 拒绝）
    if oversized:
        result["oversized"] = oversized
    # 只有解析出的每个会话都真正到达服务器，一份 transcript 才算"完成"。
    result["sessions_by_path"] = {
        path: ids for path, ids in by_path.items() if ids and set(ids) <= sent_ids - rejected
    }
    result["oversized_by_path"] = {
        path: ids for path, ids in by_path.items() if ids and set(ids) & set(oversized)
    }
    result['ok'] = not (result['import_failed'] or result['processing_failures'] or oversized or rejected)
    return result


def _lock(path: str):
    """独占、非阻塞的锁，让重叠的 tick（上一轮慢回填还没跑完、launchd 又触发了下一轮）跳过而不是重复
    提交。返回打开的文件；锁被占时返回 None。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "a+")
    try:
        acquired = _flock.acquire(fh, blocking=False)
    except BaseException:
        fh.close()
        raise
    if not acquired:
        fh.close()
        return None
    return fh


def _target(url: str, key: str, space_id: str) -> str:
    """这份台账属于哪个 服务器+命名空间。target 变化必须从空 `seen` 重新开始：旧服务器对这些文件的
    记忆对新服务器毫无意义。"""
    return hashlib.sha256(json.dumps([url, key, space_id]).encode("utf-8")).hexdigest()


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(text: str) -> None:
    print(f"{_ts()} {text}", flush=True)


def _resolve_key(args) -> str:
    """--key > --key-file > MNEMOTRACE_API_KEY。"""
    if args.key:
        return args.key
    if args.key_file:
        try:
            with open(args.key_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise SystemExit(f"cannot read --key-file {args.key_file}: {exc}")
    return os.environ.get("MNEMOTRACE_API_KEY", "")


def run_once(args) -> dict:
    """一个 tick。返回带 `exit` 码的运行摘要（见 EXIT_*）。"""
    key = _resolve_key(args)
    if (not key or not args.space_id) and not args.dry_run:
        print("Provide --space-id and an API key")
        return {"exit": EXIT_USAGE, "error": "no key"}

    state = _load_state(args.state)
    target = _target(args.url, key, args.space_id)
    if state.get("target") and state.get("target") != target:
        state["seen"], state["oversized"] = {}, {}
    # `since` 只取显式窗口。从 `last_run` 推导会困住上一个 tick 没够到的所有会话（3000 个文件的
    # `--limit 25` 回填会"见过"25 个并悄悄丢掉其余）；大小台账已经让重扫足够廉价。
    since = _parse_since(args.since) if args.since else None
    claims_path = getattr(args, "claims", DEFAULT_CLAIMS)
    paths = pending_sessions(state, since=since, quiet_seconds=args.quiet_seconds,
                             max_bytes=getattr(args, "max_bytes", IMPORT_CHUNK_BYTES),
                             claims=load_claims(claims_path, target=target))
    backlog = len(paths)
    if args.limit:
        paths = paths[:args.limit]
    lock_path = os.path.join(os.path.dirname(args.state) or ".", "watch.lock")

    if not paths:
        _log(f"nothing new to ingest (backlog {backlog})")
        if not args.dry_run:
            # 仍要拿锁：总得有人把钩子的 claims 合并进 `seen`，而没有可导入内容的 tick 正是做这件事的
            # 空闲时机。没有这一步，claims 文件只会在繁忙的 tick 被清空，并在钩子跟得上的机器上越积越多。
            lock = _lock(lock_path)
            if lock is None:
                return {"exit": EXIT_OK, "skipped": "locked"}
            try:
                folded = fold_claims(state, claims_path, target=target)
                state.update({"last_run": time.time(), "target": target})
                _save_state(args.state, state)
            finally:
                lock.close()
            if folded:
                _log(f"folded {folded} hook claim(s) into the ledger")
        return {"exit": EXIT_OK, "sessions": 0, "backlog": backlog}

    seen_before = state.get("seen") or {}
    fresh = sum(1 for p in paths if p not in seen_before)
    grown = len(paths) - fresh
    detail = f"{fresh} new" + (f", {grown} grown since last run" if grown else "")
    print(f"{len(paths)} session(s) to ingest ({detail}; backlog {backlog}):")
    for p in paths:
        mark = " +" if p in seen_before else ""
        print(f"  {os.path.basename(os.path.dirname(p))}/{os.path.basename(p)}{mark}")
    if args.dry_run:
        sessions = load_sessions(paths)
        turns = sum(len(s.messages) for s in sessions)
        print(f"\n[dry-run] would send {len(sessions)} session(s), {turns} turn(s) — nothing was stored")
        return {"exit": EXIT_OK, "sessions": len(sessions), "dry_run": True, "backlog": backlog}

    lock = _lock(lock_path)
    if lock is None:
        _log("already running, skipping")
        return {"exit": EXIT_OK, "skipped": "locked"}

    started = time.time()
    try:
        # 在锁下、在任何读取 `seen` 之前：钩子已喂过的 transcript 不能只因为列出积压时它还处于
        # "已认领但未合并"状态就在这里被重复 close。
        fold_claims(state, claims_path, target=target)
        try:
            sizes = {}
            for path in paths:
                try:
                    sizes[path] = os.stat(path).st_size
                except OSError:
                    continue
            result = ingest(args.url, key, list(sizes), space_id=args.space_id,
                            agent_id=args.agent_id, immediate=args.immediate,
                            max_bytes=getattr(args, "max_bytes", IMPORT_CHUNK_BYTES))
        except urllib.error.HTTPError as exc:
            # 服务器应答了 —— 是拒绝。不是传输问题，所以不是 exit 75；但也没有存下任何东西：
            # 台账保持原样，并转述服务器的说法。
            _log(f"server rejected the import ({args.url}): HTTP {exc.code}; nothing marked seen")
            state["last_result"] = {"at": started, "sessions": len(paths), "error": f"HTTP {exc.code}",
                                    "seconds": round(time.time() - started, 1)}
            state.update({"last_run": started, "target": target})
            _save_state(args.state, state)
            return {"exit": EXIT_USAGE, "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, OSError) as exc:
            _log(f"server unreachable ({args.url}): nothing marked seen, retry next tick — {exc}")
            return {"exit": EXIT_UNREACHABLE, "error": str(exc)}

        # `skipped` 是服务器告诉我们它已经有多少轮次：重发变大过的会话是正常且廉价的，
        # 因为 /v1/import 对内容做指纹，不信任调用方。
        seen = state.get("seen") or {}
        by_path = result.get("sessions_by_path", {})
        # 单个请求装不下的 transcript：记录 (大小, 上限)，让它们不再每个 tick 占一个名额，
        # 并带着能放行它们的旋钮提示说一次。
        over_by_path = result.get("oversized_by_path") or {}
        if over_by_path:
            ledger = dict(state.get("oversized") or {})
            for p in over_by_path:
                ledger[p] = _oversized_key(p, getattr(args, "max_bytes", IMPORT_CHUNK_BYTES))
            state["oversized"] = ledger
            _log(f"{len(over_by_path)} transcript(s) exceed the {getattr(args, 'max_bytes', IMPORT_CHUNK_BYTES):,}-byte request budget and "
                 f"were skipped; raise MNEMOTRACE_MAX_REQUEST_BYTES on the server and --max-bytes here to include them")
        for path, sessions in by_path.items():
            if sessions and path not in over_by_path and path in sizes:
                seen[path] = sizes[path]
        # 给台账设上限，避免在有数千份 transcript 的机器上无限增长。
        if len(seen) > 5000:
            seen = dict(sorted(seen.items())[-5000:])
        seconds = round(time.time() - started, 1)
        processing_failures = result.get("processing_failures") or []
        operation_ids = list(dict.fromkeys(operation_id for failure in processing_failures
                                           for operation_id in failure["operation_ids"]))
        summary = {
            "at": started, "sessions": len(paths), "new": fresh, "grown": grown,
            "episodes": int(result.get("episodes") or 0),
            "import_failed": int(result.get("import_failed") or 0), "error": None, "seconds": seconds,
            "processing_failed": len(processing_failures), "operation_ids": operation_ids,
        }
        state.update({"seen": seen, "last_run": started,
                      "target": target, "last_result": summary})
        _save_state(args.state, state)
        _log(f"fed {len(paths)} session(s) ({fresh} new) · {summary['episodes']} episode(s) · "
             f"{summary['import_failed']} import failure(s) · "
             f"{summary['processing_failed']} core processing failure(s) · "
             f"{seconds}s")
        if operation_ids:
            _log("inspect or retry operation(s): " + ", ".join(operation_ids))
        return {"exit": EXIT_USAGE if processing_failures else EXIT_OK,
                **summary, "result": result, "backlog": backlog - len(paths)}
    finally:
        lock.close()


def _run_every(args, interval_s: int) -> int:
    """前台循环，给宁愿开着终端也不装调度器的人。"""
    try:
        while True:
            run_once(args)
            _sleep(interval_s)
    except KeyboardInterrupt:
        return EXIT_OK


# --- scheduler management --------------------------------------------------------------------------

def _job_argv(args, paths: dict) -> list[str]:
    """调度任务运行的参数。固定形状：命令行上不带 key（它在 0600 文件里），不带逐消息时间戳，
    也不带它自己状态文件之外的任何文件路径。"""
    argv = ["--once", "--url", args.url, "--key-file", paths["key"], "--state", paths["state"],
            "--limit", str(args.limit if args.limit is not None else DEFAULT_LIMIT)]
    argv += ["--space-id", args.space_id]
    if args.agent_id:
        argv += ["--agent-id", args.agent_id]
    if args.immediate:
        argv.append("--immediate")
    if args.since:
        argv += ["--since", args.since]
    return argv


def _pick_scheduler(name: str) -> str:
    if name != "auto":
        return name
    if sys.platform == "darwin":
        return "launchd"
    import shutil
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    return "cron"


def _install(args) -> int:
    from . import watch_install as wi

    paths = wi.paths_for()
    scheduler = _pick_scheduler(args.scheduler)
    python = args.python or sys.executable
    interval_s = _parse_duration(args.interval)
    log_path = os.path.expanduser(args.log) if args.log else paths["log"]
    argv = _job_argv(args, paths)
    key = _resolve_key(args)

    if args.dry_run or scheduler == "cron":
        if scheduler == "launchd":
            print(wi.render_launchd_plist(args.label, python, argv, interval_s, log_path))
        elif scheduler == "systemd":
            service, timer = wi.render_systemd_units(python, argv, interval_s, log_path)
            print(f"# {paths['systemd_user']}/{wi.SYSTEMD_UNIT}.service\n{service}\n"
                  f"# {paths['systemd_user']}/{wi.SYSTEMD_UNIT}.timer\n{timer}")
        else:
            # 绝不编辑 crontab：那是所有者的文件。打印这一行，并说明该放到哪里。
            print(f"# add to `crontab -e` (the key is read from {paths['key']}, mode 0600):")
            print(wi.render_cron_line(python, argv, interval_s, log_path))
        if args.dry_run:
            problem = wi.preflight_import(python)
            print(f"\n[dry-run] preflight: {'ok' if not problem else 'WOULD REFUSE — ' + problem}")
            print("[dry-run] nothing was written or loaded")
            return EXIT_OK
        # cron：打印出的这一行从 key 文件读 key，所以那个文件是这条路径唯一要写的东西 —— 否则第一个
        # tick 就会因 "cannot read --key-file" 失败。
        if key:
            wi.write_key_file(paths["key"], key)
        elif not os.path.exists(paths["key"]):
            print(f"no API key: pass --key once (stored at {paths['key']}, mode 0600)")
            return EXIT_USAGE
        return EXIT_OK

    try:
        if scheduler == "launchd":
            info = wi.install_launchd(args.label, python, argv, interval_s, log_path=log_path,
                                      key=key or None, key_path=paths["key"])
        else:
            info = wi.install_systemd(python, argv, interval_s, log_path=log_path,
                                      key=key or None, key_path=paths["key"])
    except wi.InstallError as exc:
        print(f"install failed: {exc}")
        return EXIT_USAGE
    print(f"installed {info['scheduler']} job {info['label']}")
    print(f"  interpreter : {info['python']}")
    print(f"  interval    : every {args.interval} ({info['interval_s']}s)")
    print(f"  log         : {info['log']}")
    print(f"  key file    : {info['key_file']} (0600)")
    if info.get("lint"):
        print(f"  plutil -lint: {info['lint']}")
    print(f"  loaded      : {'yes' if info.get('loaded') else 'no'}")
    print("next: mnemotrace-watch --status")
    return EXIT_OK


def _uninstall(args) -> int:
    from . import watch_install as wi

    scheduler = _pick_scheduler(args.scheduler)
    if scheduler == "launchd":
        info = wi.uninstall_launchd(args.label, purge=args.purge)
    elif scheduler == "systemd":
        info = wi.uninstall_systemd(purge=args.purge)
    else:
        paths = wi.paths_for()
        print("cron: remove the mnemotrace-watch line from `crontab -e` yourself")
        info = {"scheduler": "cron", "removed": wi._purge(paths) if args.purge else []}
    print(f"uninstalled {info['scheduler']} job; removed: {', '.join(info['removed']) or 'nothing'}")
    if info.get("unloaded") is False:
        # 文件已删但 launchd 还没放手（有个 tick 在飞行中且活过了等待）。在这里说"已卸载"并 exit 0，
        # 正是任务被遗留在系统里的方式。
        print(f"but launchd still reports {info['label']} loaded (a tick is in flight); "
              "re-run --uninstall in a moment")
        return EXIT_USAGE
    return EXIT_OK


# --- session-close hook management ------------------------------------------------------------------

def _hook_settings(args) -> tuple[str, str, str, str, str]:
    """钩子安装器用的 (settings 路径, python, url, key 文件, target 来源)。"""
    from . import hook_install as hi

    # `--url` 带默认值，所以"调用方传了吗"只能通过与默认值比较来回答 —— 而这必须能回答，因为未传的
    # --url 必须落到已安装的 watcher 任务，而不是 $MNEMOTRACE_URL 或 127.0.0.1:8000。
    explicit_url = args.url if args.url != _URL_DEFAULT else ""
    url, key_file, source = hi.resolve_target(explicit_url, args.key_file)
    return os.path.expanduser(args.settings), args.python or sys.executable, url, key_file, source


def _hook_target(url: str, key_file: str, state_path: str, space_id: str) -> tuple[str, str]:
    """(target 哈希, 与监视器台账的比较结论)。不含内容：一个哈希加一个判定。"""
    try:
        with open(key_file, encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError:
        return "?", f"cannot read the key file {key_file} — the hook would refuse at runtime"
    if not key:
        return "?", f"the key file {key_file} is empty — the hook would refuse at runtime"
    target = _target(url, key, space_id)
    installed = _load_state(state_path).get("target")
    if not installed:
        return target, "the watcher has no target yet (it has never run)"
    if installed == target:
        return target, "matches watch_state.json"
    return target, f"DOES NOT MATCH watch_state.json ({installed}) — the hook would refuse at runtime"


def _install_hook(args) -> int:
    from . import hook_install as hi
    from . import watch_install as wi

    settings, python, url, key_file, source = _hook_settings(args)
    log = os.path.expanduser(args.hook_log)
    if not url:
        print(f"no server to feed: {source}. Pass --url (and --key-file), or install the watcher first "
              f"with `mnemotrace-watch --install --key <api-key>`.")
        return EXIT_USAGE
    try:
        command = hi.render_command(python, url, key_file, log, args.deadline, space_id=args.space_id)
    except hi.RefuseError as exc:
        print(f"refusing to build the hook command: {exc}")
        return EXIT_USAGE

    target, verdict = _hook_target(url, key_file, args.state, args.space_id)
    # 用 hook 模块而不是 watcher 自己的：settings 命令实际运行的是
    # `python -m mnemotrace.connectors.session_hook`，而过期的可编辑安装可能只有其中一个。
    problem = wi.preflight_import(python, module="mnemotrace.connectors.session_hook")
    print(f"settings    : {settings}")
    print(f"interpreter : {python}")
    print(f"url         : {url}  (from {source})")
    print(f"key file    : {key_file}")
    print(f"target      : {target} — {verdict}")
    print(f"hook log    : {log}")
    print(f"preflight   : {'ok' if not problem else 'WOULD REFUSE — ' + problem}")

    try:
        data, indent, raw = hi.load_settings(settings)
        after = hi.dumps(hi.install(data, command), indent)
    except hi.RefuseError as exc:
        print(f"\nrefusing to edit {settings}: {exc}")
        print(f"\nadd this to hooks.{hi.SLOT} by hand instead:")
        print(json.dumps(hi.entry_for(command), indent=2))
        return EXIT_USAGE

    print(f"\n{hi.diff(raw, after, settings) or '(no change: this entry is already installed)'}")
    if args.dry_run:
        print("[dry-run] nothing was written; no backup was made")
        return EXIT_OK
    if problem:
        print(f"refusing to install: {problem}")
        return EXIT_USAGE
    if raw == after:
        print("already installed; nothing to do")
        return EXIT_OK
    saved = hi.backup(settings)
    hi.write(settings, after)
    print(f"installed the {hi.SLOT} hook" + (f"; backup at {saved}" if saved else ""))
    print("next: mnemotrace-watch --status")
    return EXIT_OK


def _uninstall_hook(args) -> int:
    from . import hook_install as hi

    settings = os.path.expanduser(args.settings)
    try:
        data, indent, raw = hi.load_settings(settings)
        stripped, removed = hi.uninstall(data)
    except hi.RefuseError as exc:
        print(f"refusing to edit {settings}: {exc}")
        print(f"remove the hooks.{hi.SLOT} entry whose command contains "
              f"`{hi.HOOK_MODULE}` by hand instead")
        return EXIT_USAGE
    if not removed:
        print(f"no MnemoTrace entry in {settings}; nothing to remove")
        return EXIT_OK
    after = hi.dumps(stripped, indent)
    print(hi.diff(raw, after, settings))
    if args.dry_run:
        print("[dry-run] nothing was written; no backup was made")
        return EXIT_OK
    saved = hi.backup(settings)
    hi.write(settings, after)
    print(f"removed {removed} MnemoTrace entry/entries" + (f"; backup at {saved}" if saved else ""))
    return EXIT_OK


def _hook_status(args) -> None:
    """`--status` 中钩子自己的行：是否已安装，claims 文件落后多少。"""
    from . import hook_install as hi

    settings = os.path.expanduser(args.settings)
    try:
        data, _indent, _raw = hi.load_settings(settings)
        command = hi.find_command(data)
    except hi.RefuseError as exc:
        print(f"hook        : cannot read {settings} — {exc}")
        return
    if not command:
        print(f"hook        : not installed in {settings} "
              f"(add it with `mnemotrace-watch --install-hook`, or --dry-run first)")
    else:
        url, _key = hi._from_argv(command.split())
        print(f"hook        : SessionEnd installed in {settings} → {url or 'unknown url'}")
    claims_path = getattr(args, "claims", DEFAULT_CLAIMS)
    pending = len(load_claims(claims_path))
    print(f"hook claims : {pending} pending fold at {claims_path}")


def _status(args) -> int:
    from . import watch_install as wi

    paths = wi.paths_for()
    scheduler = _pick_scheduler(args.scheduler)
    loaded: Optional[bool] = None
    interval_s = _parse_duration(args.interval)
    if scheduler == "launchd":
        res = wi._run(["launchctl", "print", f"{wi._domain()}/{args.label}"])
        loaded = res.returncode == 0
        plist = os.path.join(paths["launch_agents"], f"{args.label}.plist")
        if os.path.exists(plist):
            import plistlib
            with open(plist, "rb") as fh:
                interval_s = int(plistlib.load(fh).get("StartInterval") or interval_s)
    elif scheduler == "systemd":
        res = wi._run(["systemctl", "--user", "is-active", f"{wi.SYSTEMD_UNIT}.timer"])
        loaded = res.returncode == 0
    print(f"scheduler   : {scheduler} — {'loaded' if loaded else 'not loaded' if loaded is not None else 'n/a'}")

    state = _load_state(args.state)
    last = state.get("last_result") or {}
    if last:
        at = datetime.fromtimestamp(last.get("at", 0)).isoformat(timespec="seconds")
        if last.get("error"):
            print(f"last run    : {at} — error: {last['error']}")
        else:
            print(f"last run    : {at} — fed {last.get('sessions', 0)} session(s), "
                  f"{last.get('import_failed', 0)} import failure(s), "
                  f"{last.get('processing_failed', 0)} core processing failure(s), "
                  f"{last.get('seconds', 0)}s")
            if last.get("operation_ids"):
                print("operations  : " + ", ".join(last["operation_ids"]))
    else:
        print("last run    : never")
    since = _parse_since(args.since) if args.since else None
    backlog = len(pending_sessions(state, since=since, quiet_seconds=args.quiet_seconds))
    limit = args.limit or DEFAULT_LIMIT
    ticks = -(-backlog // limit) if backlog else 0
    eta = ticks * interval_s
    print(f"backlog     : {backlog} session(s) → {ticks} tick(s) at {limit}/tick, "
          f"~{eta // 3600}h{(eta % 3600) // 60:02d}m at one tick every {interval_s}s")
    _hook_status(args)
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=_URL_DEFAULT)
    ap.add_argument("--key", default="", help="API key; precedence --key > --key-file > MNEMOTRACE_API_KEY")
    ap.add_argument("--key-file", default="", help="read the API key from this file")
    ap.add_argument("--since", default="", help="7d / 12h / 30m; default = every unseen transcript")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="max sessions per run (keeps a first run bounded)")
    ap.add_argument("--quiet-seconds", type=int, default=QUIET_SECONDS,
                    help="how long a transcript must be idle before it counts as finished")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--claims", default=DEFAULT_CLAIMS,
                    help="where the SessionEnd hook records the transcripts it fed")
    ap.add_argument("--max-bytes", type=int, default=IMPORT_CHUNK_BYTES,
                    help="per-request import budget; keep it under the server's MNEMOTRACE_MAX_REQUEST_BYTES")
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--agent-id")
    ap.add_argument("--immediate", action="store_true",
                    help="process imported message windows immediately instead of accepting buffered work")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true", help="run one tick (the default)")
    ap.add_argument("--every", default="", metavar="DUR",
                    help="keep running in the foreground, one tick every 30m / 2h / 1d")
    sched = ap.add_argument_group("scheduler")
    sched.add_argument("--install", action="store_true", help="install a scheduled tick")
    sched.add_argument("--uninstall", action="store_true", help="remove the scheduled tick")
    sched.add_argument("--status", action="store_true", help="is it loaded, last result, backlog")
    sched.add_argument("--scheduler", choices=["auto", "launchd", "systemd", "cron"], default="auto")
    sched.add_argument("--label", default="com.mnemotrace.watch", help="launchd label")
    sched.add_argument("--interval", default=DEFAULT_INTERVAL, help="tick interval (30m / 2h / 1d)")
    sched.add_argument("--python", default="", help="interpreter for the job (default: this one)")
    sched.add_argument("--log", default="", help="log file (default ~/.mnemotrace/logs/watch.log)")
    sched.add_argument("--purge", action="store_true",
                       help="with --uninstall: also remove the key file, state, lock and log")
    hook = ap.add_argument_group(
        "SessionEnd import hook (Claude Code)",
        "Submit ended sessions before the next scheduled tick.")
    hook.add_argument("--install-hook", action="store_true",
                      help="add the SessionEnd entry to ~/.claude/settings.json (use --dry-run first)")
    hook.add_argument("--uninstall-hook", action="store_true", help="remove that entry")
    hook.add_argument("--settings", default=hook_install.DEFAULT_SETTINGS,
                      help="which settings file to edit (default ~/.claude/settings.json)")
    hook.add_argument("--hook-log", default=hook_install.DEFAULT_HOOK_LOG,
                      help="where the hook appends its counts-only log")
    hook.add_argument("--deadline", type=int, default=hook_install.DEFAULT_DEADLINE_S,
                      help="hard bound on one hook run; missing it just leaves the file to the watcher")
    args = ap.parse_args(argv)

    modes = [m for m in ("install", "uninstall", "status", "install_hook", "uninstall_hook")
             if getattr(args, m)]
    if args.every:
        modes.append("every")
    if len(modes) > 1:
        ap.error("--install / --uninstall / --status / --install-hook / --uninstall-hook / --every "
                 "are mutually exclusive")
    if (args.install or args.install_hook) and not args.space_id:
        ap.error('Select --space-id')
    if args.install:
        return _install(args)
    if args.uninstall:
        return _uninstall(args)
    if args.install_hook:
        return _install_hook(args)
    if args.uninstall_hook:
        return _uninstall_hook(args)
    if args.status:
        return _status(args)
    if args.every:
        return _run_every(args, _parse_duration(args.every))
    return run_once(args)["exit"]


if __name__ == "__main__":
    sys.exit(main())
