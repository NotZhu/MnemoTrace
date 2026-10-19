"""安装/移除 Claude Code 的 `SessionEnd` 钩子（`mnemotrace-watch --install-hook`）。

与 watch_install.py 一脉相承 —— 纯标准库、所有路径可注入 —— 但这里几乎全是"拒绝写入"的逻辑而非编辑
逻辑，因为要改的文件不是我们的。`~/.claude/settings.json` 里放着所有者的十条互不相干的 hook 条目、一个
statusLine 和一个插件列表；一个"好心"重新格式化它的安装器，或在卸载时误删邻居条目的安装器，其后果远比
不安装更糟。

    mnemotrace-watch --install-hook --dry-run --settings ~/.claude/settings.json
    mnemotrace-watch --install-hook
    mnemotrace-watch --uninstall-hook

三重保险，按触发顺序：

1. **宁可拒绝，绝不重排格式。** 文件必须能往返（round-trip）：用检测到的缩进把 `json.loads` 的结果重新
   序列化后必须与原文一致。JSONC 注释、tab、手工排序的键序 —— 只要出现任何一种，安装器就打印出需要
   手工粘贴的条目并以非零码退出。
2. **先备份**，到 `settings.json.mnemotrace-bak-<ISO8601>`（刻意不用旁边已存在的普通 `.bak` 命名），然后用
   临时文件 + `os.replace`，这样写一半崩溃也不会截断原文件。
3. **靠 command 里的字面模块名识别自己的条目。** 卸载因此只会移除我们写入的条目；只有组被*我们*清空时
   才删组，只有 `SessionEnd` 被我们清空时才删该键，其余所有键原样复制。

目标（url + key 文件）从*已安装的 watcher 任务*解析，绝不读 `~/.claude.json`：那里配置的 MCP `mnemotrace`
服务器指向一个共享 key 的公开 demo，钩子若继承该目标就会把所有者的 transcript 传上去。解析出的值随后被
逐字写进命令字符串，因此会话发往哪个服务器，读 settings.json 即可审计 —— 没有环境变量，没有间接层。
"""
from __future__ import annotations

import copy
import difflib
import json
import os
import shutil
import time
from datetime import datetime
from typing import Optional

HOOK_MODULE = "mnemotrace.connectors.session_hook"
DEFAULT_SETTINGS = "~/.claude/settings.json"
DEFAULT_HOOK_LOG = "~/.mnemotrace/logs/hook.log"
DEFAULT_KEY_FILE = "~/.mnemotrace/watch.key"
DEFAULT_DEADLINE_S = 180

# 不设 `matcher`：对 SessionEnd 而言 matcher 匹配的是 `reason`
# （clear / resume / logout / prompt_input_exit / other / bypass_permissions_disabled）。每种 reason
# 都是一个值得导入的会话，将来新增第七种时也不能被静默跳过。
SLOT = "SessionEnd"

# 5 秒的前台预算。worker 是分离运行的，这点时间只需覆盖 `$(cat)` 加进程启动 —— 实测 sh 前端远低于 10ms。
# 所有者已有的十条条目用的是 10。
TIMEOUT_S = 5


class RefuseError(Exception):
    """所有者文件里我们不会粉饰过去的问题。CLI 打印它并以非零码退出。"""


# --- the command that goes into settings.json ------------------------------------------------------

def _quotable(value: str, what: str) -> str:
    if "'" in value:
        raise RefuseError(f"{what} contains a single quote ({value!r}); the hook command quotes every "
                          "path with '...' and cannot escape one safely")
    return value


def render_command(python: str, url: str, key_file: str, log: str,
                   deadline: int = DEFAULT_DEADLINE_S) -> str:
    """写入 `hooks.SessionEnd` 的精确字符串。

    形状照搬自本机已在运行的一个第三方 Claude Code 钩子：`[ -x ... ]` 让缺失或被移动的解释器退化为
    无操作而不是报错；else 分支把 stdin 排空到 /dev/null，CLI 永远不会看到 broken pipe。`$(cat)` 在
    前台读一次载荷；`&` 把它交给一个比退出的 CLI 活得更久的 worker，因为导入+关闭是一次延迟无上界的
    LLM 调用。
    """
    py = _quotable(python, "--python")
    url_ = _quotable(url, "--url")
    kf = _quotable(key_file, "--key-file")
    lg = _quotable(log, "--hook-log")
    # 仅在与 worker 自身默认值不同时才写出，让常见情况保持为 settings.json 里人能读到的最短可审计字符串。
    extra = f" --deadline {int(deadline)}" if int(deadline) != DEFAULT_DEADLINE_S else ""
    return (
        f"if [ -x '{py}' ]; then p=$({{ command -p cat 2>/dev/null || cat; }}); "
        f"[ -n \"$p\" ] && {{ printf '%s' \"$p\" | nohup '{py}' -m {HOOK_MODULE} "
        f"--url '{url_}' --key-file '{kf}'{extra} >>'{lg}' 2>&1; }} & "
        f"else {{ command -p cat 2>/dev/null || cat; }} >/dev/null 2>&1 || :; fi; exit 0"
    )


def entry_for(command: str, timeout: int = TIMEOUT_S) -> dict:
    """一个 hook 组，形状与所有者已有的六个无 matcher 槽位完全一致。"""
    return {"hooks": [{"type": "command", "command": command, "timeout": int(timeout)}]}


# --- reading the owner's file ----------------------------------------------------------------------

def dumps(data: dict, indent: int) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False) + "\n"


def _detect_indent(text: str) -> int:
    for line in text.splitlines():
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def load_settings(path: str) -> tuple[dict, int, str]:
    """(data, indent, 原始文本)。除非能保证重写文件时不改动任何未被要求改动的内容，否则抛 RefuseError。

    往返断言就是全部意义所在：它让 `--install-hook` 敢承诺文件之后唯一的差异就是新增的条目。一个无法
    逐字节复现的文件，就是一个我们拒绝写入的文件。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}, 2, ""
    except OSError as exc:
        raise RefuseError(f"cannot read {path}: {exc}")
    if not raw.strip():
        return {}, 2, raw
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RefuseError(f"{path} is not plain JSON ({exc}) — it may carry // or /* comments, which "
                          "json cannot round-trip")
    if not isinstance(data, dict):
        raise RefuseError(f"{path} is not a JSON object")
    indent = _detect_indent(raw)
    if dumps(data, indent).rstrip("\n") != raw.rstrip("\n"):
        raise RefuseError(f"{path} does not round-trip through json (indent {indent}); rewriting it "
                          "would reformat lines this installer was not asked to touch")
    return data, indent, raw


# --- editing ---------------------------------------------------------------------------------------

def _is_ours(leaf) -> bool:
    return isinstance(leaf, dict) and HOOK_MODULE in str(leaf.get("command") or "")


def find_command(data: dict) -> Optional[str]:
    """已安装的 忆痕 命令，没有则 None。供 `--status` 使用，并让安装幂等。"""
    slot = ((data.get("hooks") or {}).get(SLOT)) or []
    for group in slot if isinstance(slot, list) else []:
        leaves = group.get("hooks") if isinstance(group, dict) else None
        for leaf in leaves if isinstance(leaves, list) else []:
            if _is_ours(leaf):
                return str(leaf.get("command"))
    return None


def _strip(data: dict) -> int:
    """只移除 忆痕 的 leaf，无论它在哪里，其余一概不动。返回移除的 leaf 数。

    容器只有在被*我们*清空时才修剪，绝不因为"恰好为空"而删：一个带空 `hooks` 列表的第三方组，或一个
    本来就为空的 `SessionEnd`，是所有者自己的事，必须原样经历安装/卸载的往返。
    """
    hooks = data.get("hooks")
    slot = hooks.get(SLOT) if isinstance(hooks, dict) else None
    if not isinstance(slot, list):
        return 0
    removed = 0
    kept: list = []
    for group in slot:
        leaves = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(leaves, list):
            kept.append(group)
            continue
        surviving = [leaf for leaf in leaves if not _is_ours(leaf)]
        dropped = len(leaves) - len(surviving)
        removed += dropped
        if not dropped:
            kept.append(group)
        elif surviving:
            kept.append({**group, "hooks": surviving})
    if not removed:
        return 0
    if kept:
        hooks[SLOT] = kept
    else:
        hooks.pop(SLOT, None)
    # 安装前没有 `hooks` 对象的 settings.json，卸载后必须看起来原封未动；这个分支只会从安装前就有空
    # `"hooks": {}` 的文件里删掉它。
    if not hooks:
        data.pop("hooks", None)
    return removed


def install(data: dict, command: str, timeout: int = TIMEOUT_S) -> dict:
    """添加（或替换）我们的 SessionEnd 条目。幂等：换一个 --url 重跑会替换旧条目，而不是留下两个
    分别喂两台服务器的钩子。"""
    out = copy.deepcopy(data)
    _strip(out)
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RefuseError("settings.json `hooks` is not an object")
    slot = hooks.setdefault(SLOT, [])
    if not isinstance(slot, list):
        raise RefuseError(f"settings.json `hooks.{SLOT}` is not a list")
    slot.append(entry_for(command, timeout))
    return out


def uninstall(data: dict) -> tuple[dict, int]:
    out = copy.deepcopy(data)
    return out, _strip(out)


# --- writing ---------------------------------------------------------------------------------------

def diff(old: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        fromfile=path, tofile=f"{path} (after)"))


def _real(path: str) -> str:
    """真正要编辑的文件，跟随符号链接。

    `~/.claude/settings.json` 经常是指向 dotfiles 仓库的符号链接。`os.replace(tmp, link)` 会删掉链接、
    原地放一个普通文件：安装报告成功，dotfiles 副本却永远看不到这条目，而下次 `stow`/`git checkout` 又会
    悄悄把钩子拿走。先解析真实路径，意味着我们编辑的是所有者真正纳入版本管理的那个文件。
    """
    return os.path.realpath(path) if os.path.islink(path) else path


def backup(path: str, now: Optional[float] = None) -> Optional[str]:
    """先复制，并用我们自己的命名：`.bak` 和 `.bak.<epoch>` 已经在真文件旁边存在，一边声称在备份一边
    覆盖别人的备份是两头都输。"""
    path = _real(path)
    if not os.path.exists(path):
        return None
    stamp = datetime.fromtimestamp(now if now is not None else time.time()).strftime("%Y-%m-%dT%H-%M-%S")
    dest = f"{path}.mnemotrace-bak-{stamp}"
    # 同一秒内的两次安装不能只留一份备份：第一份才是原始文件，也正是需要备份的人想要的那份。
    suffix = 1
    while os.path.exists(dest):
        suffix += 1
        dest = f"{path}.mnemotrace-bak-{stamp}-{suffix}"
    shutil.copy2(path, dest)
    return dest


def write(path: str, text: str) -> None:
    """原子替换，同时保住所有者原有的文件属性：同一 inode 目标、同样的权限位。

    所有者的 `~/.claude/settings.json` 是 0600，而 settings.json 这种文件合法地携带带 API token 的
    `env` 块。朴素的临时文件 + `os.replace` 会按 umask（这里是 0644）写新文件，于是装个钩子就悄悄把
    所有者的配置变成全局可读并一直保持 —— 一个没人要求、也没人会注意到的权限变更。临时文件同样以
    0600 打开，因此即使写到一半，内容也不会短暂地被他人读到。
    """
    path = _real(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        mode = 0o600  # 新文件：默认私有，与 settings.json 本来的属性一致
    tmp = f"{path}.{os.getpid()}.mnemotrace-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # 绝不把写了一半的 `.mnemotrace-tmp` 留在 ~/.claude 里让所有者纳闷。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- target resolution -----------------------------------------------------------------------------

def _from_argv(argv: list[str]) -> tuple[str, str]:
    url = key_file = ""
    for i, token in enumerate(argv):
        if token == "--url" and i + 1 < len(argv):
            url = argv[i + 1]
        elif token == "--key-file" and i + 1 < len(argv):
            key_file = argv[i + 1]
    return url, key_file


def _from_launchd(home: str, label: str) -> tuple[str, str]:
    import plistlib

    path = os.path.join(home, "Library", "LaunchAgents", f"{label}.plist")
    try:
        with open(path, "rb") as fh:
            doc = plistlib.load(fh)
    except (OSError, ValueError):
        return "", ""
    argv = doc.get("ProgramArguments")
    return _from_argv([str(a) for a in argv]) if isinstance(argv, list) else ("", "")


def _from_systemd(home: str) -> tuple[str, str]:
    import shlex

    path = os.path.join(home, ".config", "systemd", "user", "mnemotrace-watch.service")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return "", ""
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            try:
                return _from_argv(shlex.split(line.split("=", 1)[1]))
            except ValueError:
                return "", ""
    return "", ""


def resolve_target(url: str = "", key_file: str = "", *, home: Optional[str] = None,
                   label: str = "com.mnemotrace.watch", env: Optional[dict] = None) -> tuple[str, str, str]:
    """钩子的喂送目标，按唯一不可能选中公开 demo 的顺序解析。

    已安装的 watcher 任务是权威来源，因为钩子必须与它的 `seen` 台账和 `target` 哈希一致 —— 不一致时钩子
    在运行时本来就会拒绝。绝不查 `~/.claude.json`：它的 MCP `mnemotrace` 服务器指向共享的公开 demo，钩子若
    继承那个目标就会把所有者的 transcript 传上去。

    返回 (url, key_file, 来源说明)。
    """
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    if url and key_file:
        return url, key_file, "the flags you passed"

    job_url, job_key = _from_launchd(home, label)
    job_source = f"the installed launchd job {label}"
    if not (job_url or job_key):
        job_url, job_key = _from_systemd(home)
        job_source = "the installed systemd unit mnemotrace-watch.service"

    if url:
        resolved_url, where = url, "the flags you passed"
    elif job_url:
        resolved_url, where = job_url, job_source
    elif env.get("MNEMOTRACE_URL") or env.get("MNEMOTRACE_API_URL"):
        resolved_url, where = env.get("MNEMOTRACE_URL") or env.get("MNEMOTRACE_API_URL"), "$MNEMOTRACE_URL"
    else:
        resolved_url, where = "", "nowhere — no watcher job is installed and $MNEMOTRACE_URL is unset"
    resolved_key = key_file or job_key or os.path.join(home, ".mnemotrace", "watch.key")
    return resolved_url, resolved_key, where
