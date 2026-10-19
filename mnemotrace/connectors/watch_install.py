"""安装/移除持续喂记忆的调度任务（`mnemotrace-watch --install`）。

纯标准库，所有系统调用都走可注入的 `run`，测试永远不会碰真实的 launchd 或 systemd。`home` 同样可注入：
本模块拥有的文件是

    ~/Library/LaunchAgents/<label>.plist              (macOS, launchd)
    ~/.config/systemd/user/mnemotrace-watch.{service,timer}  (Linux, systemd --user)
    ~/.mnemotrace/watch.key                               (API key, mode 0600)
    ~/.mnemotrace/watch_state.json  ~/.mnemotrace/watch.lock  ~/.mnemotrace/logs/watch.log

为什么用调度器而不是守护进程：transcript 只在会话归于安静后才值得读（connectors/watch.py），所以周期性
的 `--once` 就是全部工作。launchd 的 StartInterval + RunAtLoad，以及 Persistent=true 的 systemd timer，
都会补跑笔记本睡眠期间错过的 tick。
"""
from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import time
from typing import Callable, Optional

MODULE = "mnemotrace.connectors.watch"
DEFAULT_LABEL = "com.mnemotrace.watch"
SYSTEMD_UNIT = "mnemotrace-watch"

Runner = Callable[..., subprocess.CompletedProcess]

_sleep = time.sleep  # 可注入：下面卸载时的轮询不能拖慢测试
UNLOAD_WAIT_S = 10.0

# `python -c` 会把*当前目录*放进 sys.path，所以即使已安装的包缺少该模块，从仓库内跑的 preflight 也会通过
# （第一次真实安装正是这样拿到了绿色 preflight 和一个记录 ModuleNotFoundError 的任务）。导入前先离开
# 仓库：launchd 和 systemd 从 `/` 启动任务。
def _preflight_code(module: str) -> str:
    return f"import os; os.chdir('/'); import {module}"


PREFLIGHT_CODE = _preflight_code(MODULE)


class InstallError(Exception):
    """安装器无法恢复的一步（plist 损坏、缺 key）。CLI 打印它并以 1 退出。"""


def _run(cmd: list[str], *, env: Optional[dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def paths_for(home: Optional[str] = None) -> dict[str, str]:
    """watcher + 调度器拥有的所有文件，以 `home` 为根（默认真实 home）。"""
    home = home or os.path.expanduser("~")
    mnemotrace = os.path.join(home, ".mnemotrace")
    return {
        "home": home,
        "key": os.path.join(mnemotrace, "watch.key"),
        "state": os.path.join(mnemotrace, "watch_state.json"),
        "lock": os.path.join(mnemotrace, "watch.lock"),
        "log": os.path.join(mnemotrace, "logs", "watch.log"),
        "launch_agents": os.path.join(home, "Library", "LaunchAgents"),
        "systemd_user": os.path.join(home, ".config", "systemd", "user"),
    }


def write_key_file(path: str, key: str) -> None:
    """API key 是这份记忆所属的命名空间；它不能被机器上其他用户读取，也绝不能出现在 plist 里（plist
    全局可读，且 `launchctl print` 会显示它）。创建时即 0600，重装时重新断言，因为 O_CREAT 不会改动
    已存在文件的权限。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.strip() + "\n")
    os.chmod(path, 0o600)


def preflight_import(python: str, *, module: str = MODULE, home: Optional[str] = None,
                     run: Runner = _run) -> Optional[str]:
    """在调度器（或钩子）给出的环境下，`python` 能导入 `module` 吗？能则返回 None，否则返回错误文本。

    第一次真实安装时发现：plist 已加载、`launchctl print` 显示运行中，tick 却记录了
    `No module named mnemotrace.connectors.watch` —— shell 通过 cwd 把仓库放上了 sys.path，editable 安装指向
    一个更旧的 checkout，而 launchd 从 `/` 以干净环境运行。装成功但每个 tick 都失败的安装比拒绝更糟，
    所以这里用调度器的环境（无 PYTHONPATH、无 venv 激活、cwd `/`）运行，而不是调用方的环境。"""
    p = paths_for(home)
    environment = {"HOME": p["home"], "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if os.name == "nt":
        # Windows 启动 Python 需要系统目录；不继承 shell 的 Python 配置或供应商凭据。
        environment["USERPROFILE"] = p["home"]
        for name in ("SystemRoot", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        root = environment.get("SystemRoot")
        environment["PATH"] = (
            os.pathsep.join((os.path.join(root, "System32"), root)) if root else os.defpath
        )
    try:
        res = run([python, "-c", _preflight_code(module)], env=environment)
    except OSError as exc:
        detail = str(exc)
    else:
        if res.returncode == 0:
            return None
        lines = (res.stderr or res.stdout or "").strip().splitlines()
        detail = lines[-1] if lines else "no output"
    return (f"{python} cannot import {module} in a clean environment "
            f"({detail}). launchd/systemd run without your shell's "
            f"PYTHONPATH or cwd, so the package must be installed into that interpreter: "
            f"`{python} -m pip install -e <repo>` (or pass --python <interpreter that has it>)")


# --- renderers -------------------------------------------------------------------------------------

def render_launchd_plist(label: str, python: str, argv: list[str], interval_s: int, log_path: str) -> str:
    """不设 EnvironmentVariables（key 在 0600 文件里，不进 plist）、不设 KeepAlive（这是 tick，不是服务）、
    不设 WorkingDirectory（模块用绝对路径的解释器导入）。"""
    doc = {
        "Label": label,
        "ProgramArguments": [python, "-m", MODULE, *argv],
        "StartInterval": int(interval_s),
        "RunAtLoad": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "ProcessType": "Background",
    }
    return plistlib.dumps(doc, sort_keys=False).decode("utf-8")


def render_systemd_units(python: str, argv: list[str], interval_s: int, log_path: str) -> tuple[str, str]:
    cmd = " ".join(shlex.quote(a) for a in [python, "-m", MODULE, *argv])
    service = (
        "[Unit]\n"
        "Description=MnemoTrace: feed memory from local agent sessions\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={cmd}\n"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=MnemoTrace watch tick\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        f"OnUnitActiveSec={int(interval_s)}s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def render_cron_line(python: str, argv: list[str], interval_s: int, log_path: str) -> str:
    """cron 只有分钟粒度；一小时内的间隔变成 `*/N`，更大的按小时步进。"""
    minutes = max(1, int(interval_s) // 60)
    if minutes < 60:
        spec = f"*/{minutes} * * * *"
    else:
        hours = max(1, minutes // 60)
        spec = f"0 */{hours} * * *" if hours < 24 else "0 3 * * *"
    cmd = " ".join(shlex.quote(a) for a in [python, "-m", MODULE, *argv])
    return f"{spec} {cmd} >> {shlex.quote(log_path)} 2>&1"


# --- launchd ---------------------------------------------------------------------------------------

def _domain() -> str:
    # launchctl 的 gui 域以 uid 为键。Windows 没有 uid，也永远不会跑 launchd；这个回退只是让
    # （测试中已打桩的）管道代码在那里保持可导入。
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return f"gui/{uid}"


def install_launchd(label: str, python: str, argv: list[str], interval_s: int, *,
                    home: Optional[str] = None, run: Runner = _run, log_path: Optional[str] = None,
                    key: Optional[str] = None, key_path: Optional[str] = None) -> dict:
    """写 key 文件（若给了 key），渲染 + lint + 写 plist，然后（重）加载。

    幂等：先 `bootout`（尚未加载时忽略），再 `bootstrap`，因此换间隔或解释器重跑会替换任务，而不是报
    "already loaded"。"""
    p = paths_for(home)
    key_path = key_path or p["key"]
    log_path = log_path or p["log"]
    problem = preflight_import(python, home=home, run=run)
    if problem:
        raise InstallError(problem)
    if key:
        write_key_file(key_path, key)
    elif not os.path.exists(key_path):
        raise InstallError(f"no API key: pass --key once (stored at {key_path}, mode 0600)")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    os.makedirs(p["launch_agents"], exist_ok=True)

    plist_path = os.path.join(p["launch_agents"], f"{label}.plist")
    text = render_launchd_plist(label, python, argv, interval_s, log_path)
    with open(plist_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    lint = "skipped (plutil not found)"
    if shutil.which("plutil"):
        res = run(["plutil", "-lint", plist_path])
        if res.returncode != 0:
            os.remove(plist_path)
            raise InstallError(f"plutil -lint rejected the plist: {(res.stderr or res.stdout).strip()}")
        lint = "ok"

    run(["launchctl", "bootout", f"{_domain()}/{label}"])  # 尚未加载是首次运行的常态
    boot = run(["launchctl", "bootstrap", _domain(), plist_path])
    if boot.returncode != 0:
        raise InstallError(f"launchctl bootstrap failed: {(boot.stderr or boot.stdout).strip()}")
    shown = run(["launchctl", "print", f"{_domain()}/{label}"])
    return {
        "scheduler": "launchd", "label": label, "plist": plist_path, "python": python,
        "interval_s": int(interval_s), "log": log_path, "key_file": key_path, "lint": lint,
        "loaded": shown.returncode == 0,
    }


def uninstall_launchd(label: str, purge: bool = False, *, home: Optional[str] = None,
                      run: Runner = _run, wait_s: float = UNLOAD_WAIT_S) -> dict:
    """bootout，等 launchd 真正放手，然后删除文件。

    launchd 异步地拆除运行中的服务：`bootout` 返回时飞行中的 tick 还在被发送信号，`launchctl print` 会
    再成功一小会儿。RunAtLoad 意味着 `--install` 之后总有个 tick 在飞行中，所以本机第一次卸载报告了成功，
    `launchctl list` 却还显示任务的 PID。轮询（有上限），让"已卸载"意味着真的没了；等待超时后任务仍在，
    则 `unloaded` 为 False。"""
    p = paths_for(home)
    plist_path = os.path.join(p["launch_agents"], f"{label}.plist")
    run(["launchctl", "bootout", f"{_domain()}/{label}"])
    unloaded = _wait_unloaded(label, run, wait_s)
    removed = _remove(plist_path)
    if purge:
        removed += _purge(p)
    return {"scheduler": "launchd", "label": label, "removed": removed, "unloaded": unloaded}


def _wait_unloaded(label: str, run: Runner, wait_s: float) -> bool:
    deadline = time.monotonic() + wait_s
    while True:
        if run(["launchctl", "print", f"{_domain()}/{label}"]).returncode != 0:
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep(0.25)


# --- systemd ---------------------------------------------------------------------------------------

def install_systemd(python: str, argv: list[str], interval_s: int, *,
                    home: Optional[str] = None, run: Runner = _run, log_path: Optional[str] = None,
                    key: Optional[str] = None, key_path: Optional[str] = None) -> dict:
    p = paths_for(home)
    key_path = key_path or p["key"]
    log_path = log_path or p["log"]
    problem = preflight_import(python, home=home, run=run)
    if problem:
        raise InstallError(problem)
    if key:
        write_key_file(key_path, key)
    elif not os.path.exists(key_path):
        raise InstallError(f"no API key: pass --key once (stored at {key_path}, mode 0600)")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    os.makedirs(p["systemd_user"], exist_ok=True)
    service, timer = render_systemd_units(python, argv, interval_s, log_path)
    service_path = os.path.join(p["systemd_user"], f"{SYSTEMD_UNIT}.service")
    timer_path = os.path.join(p["systemd_user"], f"{SYSTEMD_UNIT}.timer")
    with open(service_path, "w", encoding="utf-8") as fh:
        fh.write(service)
    with open(timer_path, "w", encoding="utf-8") as fh:
        fh.write(timer)
    run(["systemctl", "--user", "daemon-reload"])
    res = run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
    if res.returncode != 0:
        raise InstallError(f"systemctl enable failed: {(res.stderr or res.stdout).strip()}")
    return {
        "scheduler": "systemd", "label": SYSTEMD_UNIT, "service": service_path, "timer": timer_path,
        "python": python, "interval_s": int(interval_s), "log": log_path, "key_file": key_path,
        "loaded": True,
    }


def uninstall_systemd(purge: bool = False, *, home: Optional[str] = None, run: Runner = _run) -> dict:
    p = paths_for(home)
    run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"])
    removed = _remove(os.path.join(p["systemd_user"], f"{SYSTEMD_UNIT}.timer"))
    removed += _remove(os.path.join(p["systemd_user"], f"{SYSTEMD_UNIT}.service"))
    run(["systemctl", "--user", "daemon-reload"])
    if purge:
        removed += _purge(p)
    return {"scheduler": "systemd", "label": SYSTEMD_UNIT, "removed": removed}


# --- shared ----------------------------------------------------------------------------------------

def _remove(path: str) -> list[str]:
    try:
        os.remove(path)
        return [path]
    except FileNotFoundError:
        return []


def _purge(p: dict[str, str]) -> list[str]:
    removed: list[str] = []
    for key in ("key", "state", "lock", "log"):
        removed += _remove(p[key])
    # 日志目录是 --install 创建的；把所有者的 ~/.mnemotrace 原样留下。
    log_dir = os.path.dirname(p["log"])
    try:
        if log_dir and not os.listdir(log_dir):
            os.rmdir(log_dir)
            removed.append(log_dir)
    except OSError:
        pass
    return removed
