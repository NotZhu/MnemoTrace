"""SessionEnd 立即把指定会话交给数据库后台处理。

钩子将 Claude Code transcript 发送到 `/v1/import`。只有原文得到持久化确认后才写入 watcher 的认领台账。
用 `mnemotrace-watch --install-hook --space-id <SPACE_ID>` 生成带明确空间的钩子配置。

本模块恪守四条规则。

* **永远以 0 退出。** 大声失败的钩子会被用户关掉，而这里的每种失败在构造上都可恢复：未认领的文件反正
  会在 T+15min 被监视器捡起。所以失败只是一行日志，仅此而已。
* **绝不写 `watch_state.json`。** `watch.run_once` 在 `watch.lock` 下对该文件读-改-写；钩子的写入若落在
  tick 中间，会被该 tick 的过期保存悄悄抹掉 —— 一次无报错的丢失更新，与"钩子时灵时不灵"无法区分。
  它改为追加一行 `hook_claims.jsonl`，监视器则在它已持有的锁下合并 claims（`watch.load_claims` /
  `watch.fold_claims`）。
* **拒绝喂给 watcher 不拥有的服务器。** `~/.claude.json` 把 MCP `mnemotrace` 服务器指向公开 demo；从环境
  解析目标的钩子会悄悄把所有者的 transcript 传上去。`watch_state.json` 里已有的 `target` 哈希是权威，
  不匹配就是拒绝，不是警告。
* **只记录计数和速率，从不记录内容。** 不写路径、项目名、会话标题、结论文本 —— transcript 用
  `sha1(path)[:12]` 标识。（`watch.log` 会打印目录名；那是本模块绝不能照抄的缺陷。）

`SessionEnd` 捕获停止写入的会话。`Stop` 时记录可能仍在增长，子 Agent 记录由导入器的现有规则过滤。
钩子脱离即将退出的 Agent 进程，给持久化请求留出完成时间。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import sys
import time
from typing import Callable, Iterable, Optional

from . import watch
from .. import _flock

DEFAULT_DEADLINE_S = 180
MAX_STDIN_BYTES = 1 << 20  # 钩子载荷只有几百字节；更大的就不是钩子载荷了

# 与 find_sessions() 的根目录及其 `agent-*.jsonl` 规则保持一致。若钩子喂了监视器永远不会碰
# 的文件，就会把覆盖率分母看不见的 transcript 塞进记忆。
HOOK_ROOTS = ("~/.claude/projects", "~/.codex/sessions")

# 可注入：真调用 os.setsid() 的测试会把整个 pytest 进程挪进新会话。本模块其余部分都已参数化，这是唯一
# 的全局量。os.setsid 仅限 Unix；Windows 没有可脱离的控制终端，回退为 no-op —— 模块在那里仍可导入，
# 钩子作为普通子进程运行。
_setsid: Callable[[], int] = getattr(os, "setsid", lambda: 0)


def _path_key(path: str) -> str:
    """transcript 的稳定、不含内容的句柄。日志和锁文件都用它，于是日志可以指认一个文件而不暴露项目、
    worktree 或用户。"""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()


def _say(text: str) -> None:
    """往 stdout 输出一行结构化日志，settings 命令把它重定向到 ~/.mnemotrace/logs/hook.log。

    刻意不再开第二个日志文件：shell 已经持有重定向（`>>'<LOG>' 2>&1`），traceback 和结果行会落在同一处、
    顺序也正确。
    """
    print(f"{watch._ts()} {text}", flush=True)


def _under_roots(path: str, roots: Iterable[str]) -> bool:
    real = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(os.path.expanduser(root))
        if real == base or real.startswith(base + os.sep):
            return True
    return False


def _lock(lock_dir: str, path: str):
    """按 transcript 划分、非阻塞。绝不用 `watch.lock`：那个锁整个 tick 都被持有（实测最长 148s），
    等它的钩子在会话结束后很久还在跑。"""
    os.makedirs(lock_dir, exist_ok=True)
    fh = open(os.path.join(lock_dir, _path_key(path)[:16] + ".lock"), "a+")
    try:
        acquired = _flock.acquire(fh, blocking=False)
    except BaseException:
        fh.close()
        raise
    if not acquired:
        fh.close()
        return None
    return fh


@contextlib.contextmanager
def _deadline(seconds: int):
    """给挂死的 worker 设硬上限。错过它毫无代价 —— 文件不会被认领，监视器下个 tick 会接手 —— 这正是
    alarm 在这里安全、而在 watcher 里不安全的原因。"""
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _fire(_signum, _frame):
        raise TimeoutError(f"deadline of {seconds}s exceeded")

    try:
        previous = signal.signal(signal.SIGALRM, _fire)
    except ValueError:  # 不在主线程；urllib 的 timeout 是唯一可用的上限
        yield
        return
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def claim(claims_path: str, path: str, size: int, target: str, now: Optional[float] = None) -> None:
    """记录这份 transcript 已按这个大小喂给了这个目标。

    单条 `O_APPEND` 写入：这么小的一次写在安装器支持的平台上是原子的，因此钩子边写、监视器边读，最坏
    只会留下一条截断的末行（`load_claims` 会跳过），绝不会交错。0600 是因为这是整个功能里唯一存有路径
    的文件。
    """
    os.makedirs(os.path.dirname(claims_path) or ".", exist_ok=True)
    line = json.dumps({"path": path, "size": int(size),
                       "at": now if now is not None else time.time(), "target": target}) + "\n"
    fd = os.open(claims_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(claims_path, 0o600)  # O_CREAT 不会改动已存在文件的权限


def _parse(argv: Optional[list[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m mnemotrace.connectors.session_hook",
                                 description="Feed one finished Claude Code session to MnemoTrace.")
    ap.add_argument("--url", default=watch.DEFAULT_URL)
    ap.add_argument("--key-file", default=os.path.expanduser("~/.mnemotrace/watch.key"))
    ap.add_argument("--space-id", default=os.environ.get("MNEMOTRACE_SPACE_ID"))
    ap.add_argument("--state", default=watch.DEFAULT_STATE,
                    help="read-only: the watcher's ledger, consulted for `target` and `seen`")
    ap.add_argument("--claims", default=watch.DEFAULT_CLAIMS)
    ap.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_S)
    ap.add_argument("--max-bytes", type=int, default=watch.IMPORT_CHUNK_BYTES)
    return ap.parse_args(argv)


def run(argv: Optional[list[str]] = None, *, stdin: Optional[str] = None,
        ingest: Optional[Callable[..., dict]] = None,
        roots: Iterable[str] = HOOK_ROOTS) -> int:
    """处理一个钩子载荷。永远返回 0 —— 见模块 docstring。"""
    args = _parse(argv)
    ingest = ingest or watch.ingest

    raw = stdin if stdin is not None else sys.stdin.read(MAX_STDIN_BYTES)
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0  # 有别的东西往我们 stdin 里塞了内容；不作声
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionEnd":
        # 静默，不记日志：就算所有者哪天把它接到别的槽位，这个 worker 也无害；而每个 Stop 记一行会是
        # 每个会话约 23 行纯噪音。
        return 0

    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path.strip():
        _say("hook - SessionEnd payload carried no transcript_path")
        return 0
    path = os.path.abspath(os.path.expanduser(path))
    key12 = _path_key(path)[:12]

    # 先分离再做慢事：SessionEnd 时 CLI 正在退出，仍在它会话里的作业会随之而死。macOS 没有
    # /usr/bin/setsid，所以这是唯一能做这件事的地方。
    try:
        _setsid()
    except OSError:
        pass  # 已是会话首进程，或没有控制终端 —— 无可分离

    try:
        with open(args.key_file, encoding="utf-8") as fh:
            key = fh.read().strip()
    except OSError as exc:
        _say(f"hook {key12} refused: cannot read the key file ({exc.__class__.__name__})")
        return 0
    if not key:
        _say(f"hook {key12} refused: the key file is empty")
        return 0

    # 目标守卫。`_target` 与 run_once 用来在服务器或命名空间变化时清空 `seen` 的是同一字段，所以与它
    # 一致正是"钩子与监视器喂同一个记忆"的含义。state 文件在这里以只读方式打开，且本进程仅此一处。
    if not args.space_id:
        _say(f"hook {key12} refused: missing space ID")
        return 0
    target = watch._target(args.url, key, args.space_id)
    state = watch._load_state(args.state)
    installed = state.get("target")
    if installed and installed != target:
        _say(f"hook {key12} refused: target {target} != {installed} — not feeding")
        return 0

    try:
        st = os.stat(path)
    except OSError:
        _say(f"hook {key12} skipped (transcript is gone)")
        return 0
    name = os.path.basename(path)
    if not name.endswith(".jsonl") or name.startswith("agent-") or not _under_roots(path, roots):
        # `agent-*.jsonl` 是子 agent transcript；两个既有层都丢弃它，这里同样。
        _say(f"hook {key12} skipped (not a watched transcript)")
        return 0
    if (state.get("seen") or {}).get(path) == st.st_size:
        _say(f"hook {key12} skipped (seen)")
        return 0
    if watch.load_claims(args.claims, target=target).get(path) == st.st_size:
        _say(f"hook {key12} skipped (seen)")
        return 0

    lock = _lock(os.path.join(os.path.dirname(args.claims) or ".", "hooks"), path)
    if lock is None:
        _say(f"hook {key12} skipped (another hook is feeding it)")
        return 0

    started = time.time()
    mtime = st.st_mtime  # 延迟计时从 transcript 停止增长算起，而不是从我们醒来算起
    try:
        with _deadline(args.deadline):
            result = ingest(args.url, key, [path], space_id=args.space_id,
                            max_bytes=args.max_bytes, timeout=args.deadline)
    except Exception as exc:  # noqa: BLE001 — 现在每个失败都是监视器的事，与用户无关
        _say(f"hook {key12} failed: {exc.__class__.__name__} — nothing claimed, "
             f"the watcher retries this file")
        return 0
    finally:
        lock.close()

    ids = (result.get("sessions_by_path") or {}).get(path) or []
    if not ids:
        _say(f"hook {key12} failed: import was not confirmed — nothing claimed")
        return 0

    claim(args.claims, path, st.st_size, target)
    finished = time.time()
    _say(f"hook {key12} · {st.st_size // 1024}KB · {len(ids)} session · "
         f"{int(result.get('episodes') or 0)} episode(s) · lag_s {round(finished - mtime, 1)} · {round(finished - started, 1)}s")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
