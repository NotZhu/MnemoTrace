"""`mnemotrace-watch --install`：让记忆自动定时更新的调度器。

这里所有测试都运行在注入的 `home` 和录制式的 `run` 之上，因此没有任何测试会加载真实的 launchd
或 systemd 任务——这是本模块在贡献者机器上绝不能发生的事。"""
from __future__ import annotations

import configparser
import os
import plistlib
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from mnemotrace.connectors import watch_install as wi

PY = "/opt/python/bin/python3"
ARGV = ["--once", "--url", "http://127.0.0.1:8000", "--key-file", "/h/.mnemotrace/watch.key",
        "--state", "/h/.mnemotrace/watch_state.json", "--limit", "25"]


def test_preflight_runs_python_with_only_scheduler_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "not-a-python-home"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "ambient-venv"))
    monkeypatch.setenv("MNEMOTRACE_PREFLIGHT_SENTINEL", "must-not-be-inherited")
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.run(cmd, capture_output=True, text=True, **kwargs)

    assert wi.preflight_import(sys.executable, module="json", home=str(tmp_path), run=run) is None
    cmd, kwargs = calls[0]
    assert cmd[0] == sys.executable and cmd[1] == "-c"
    environment = kwargs["env"]
    assert environment["HOME"] == str(tmp_path)
    assert not {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "MNEMOTRACE_PREFLIGHT_SENTINEL"} & environment.keys()
    if os.name == "nt":
        assert environment["USERPROFILE"] == str(tmp_path)
        assert environment["SystemRoot"] == os.environ["SystemRoot"]


def test_preflight_cannot_import_from_cwd_or_pythonpath(tmp_path, monkeypatch):
    module = "synthetic_preflight_cwd_only"
    (tmp_path / f"{module}.py").write_text("VALUE = 'not-installed'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    problem = wi.preflight_import(sys.executable, module=module, home=str(tmp_path))
    assert problem is not None and f"No module named '{module}'" in problem
    assert "pip install -e" in problem


def test_missing_preflight_interpreter_refuses_before_writing(tmp_path):
    with pytest.raises(wi.InstallError, match="cannot import"):
        wi.install_launchd("com.mnemotrace.watch", str(tmp_path / "missing-python"),
                           ARGV, 1800, home=str(tmp_path), key="synthetic-key")
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / ".mnemotrace").exists()


class Recorder:
    """模拟的 launchctl/systemctl：记录所有调用，并建模 launchd 中唯一重要的状态——
    `print` 只在 `bootstrap` 和 `bootout` 之间成功。"""

    def __init__(self, fail: tuple[str, ...] = ()):
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str] | None] = []
        self.fail = fail
        self.loaded = False

    def __call__(self, cmd: list[str], *, env=None) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        self.environments.append(env)
        if cmd[0] == "plutil":  # 可用时用真实 plutil 做 lint；plist 必须是合法的 plist
            return subprocess.run(cmd, capture_output=True, text=True)
        code = 1 if any(f in " ".join(cmd) for f in self.fail) else 0
        if cmd[:2] == ["launchctl", "bootstrap"] and not code:
            self.loaded = True
        elif cmd[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        elif cmd[:2] == ["launchctl", "print"] and not self.loaded:
            code = 1
        return subprocess.CompletedProcess(cmd, code, "", "boom" if code else "")


def test_plist_round_trips_and_carries_no_secret():
    text = wi.render_launchd_plist("com.mnemotrace.watch", PY, ARGV, 1800, "/h/.mnemotrace/logs/watch.log")
    doc = plistlib.loads(text.encode("utf-8"))
    assert doc["Label"] == "com.mnemotrace.watch"
    assert doc["ProgramArguments"] == [PY, "-m", "mnemotrace.connectors.watch", *ARGV]
    assert doc["StartInterval"] == 1800 and doc["RunAtLoad"] is True
    assert doc["StandardOutPath"] == doc["StandardErrorPath"] == "/h/.mnemotrace/logs/watch.log"
    assert doc["ProcessType"] == "Background"
    # 密钥只经 0600 权限的 key 文件传递：plist 是全局可读且会被 `launchctl print` 显示的，
    # 所以不放 EnvironmentVariables；也不要 KeepAlive（这是周期性 tick，不是常驻服务）
    for absent in ("EnvironmentVariables", "KeepAlive", "WorkingDirectory"):
        assert absent not in doc
    assert "secret" not in text and "--key " not in text


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
def test_plist_passes_plutil_lint(tmp_path):
    p = tmp_path / "x.plist"
    p.write_text(wi.render_launchd_plist("com.mnemotrace.watch", PY, ARGV, 1800, "/h/log"))
    assert subprocess.run(["plutil", "-lint", str(p)], capture_output=True).returncode == 0


def test_install_launchd_writes_key_0600_and_loads_in_order(tmp_path):
    rec = Recorder()
    info = wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 1800, home=str(tmp_path), run=rec,
                              key="secret-key")
    plist = tmp_path / "Library" / "LaunchAgents" / "com.mnemotrace.watch.plist"
    assert plist.exists() and info["plist"] == str(plist)
    assert plistlib.loads(plist.read_bytes())["Label"] == "com.mnemotrace.watch"
    key = tmp_path / ".mnemotrace" / "watch.key"
    assert key.read_text() == "secret-key\n"
    if os.name == "posix":  # Windows 没有 POSIX 权限位；0600 在那里无法表示
        assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert "secret-key" not in plist.read_text()
    launchctl = [c[1] for c in rec.calls if c[0] == "launchctl"]
    assert launchctl == ["bootout", "bootstrap", "print"], rec.calls
    assert rec.calls[-2] == ["launchctl", "bootstrap", wi._domain(), str(plist)]
    assert info["loaded"] is True and info["key_file"] == str(key)
    # 先 preflight（用调度器的干净环境，而非调用者的环境），再 lint，最后加载
    assert rec.calls[0] == [PY, "-c", wi.PREFLIGHT_CODE]
    assert rec.environments[0]["HOME"] == str(tmp_path)
    assert "PYTHONPATH" not in rec.environments[0]
    assert "chdir('/')" in wi.PREFLIGHT_CODE  # `-c` 时 cwd 在 sys.path 上；launchd 从 `/` 启动
    if shutil.which("plutil"):
        assert rec.calls[1][:2] == ["plutil", "-lint"] and info["lint"] == "ok"

    # 幂等的重复安装：文件相同、key 保留（这次不传 --key），仍然是 bootout->bootstrap
    rec2 = Recorder()
    wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 3600, home=str(tmp_path), run=rec2)
    assert key.read_text() == "secret-key\n"
    assert plistlib.loads(plist.read_bytes())["StartInterval"] == 3600
    assert [c[1] for c in rec2.calls if c[0] == "launchctl"] == ["bootout", "bootstrap", "print"]


def test_install_launchd_without_any_key_refuses(tmp_path):
    with pytest.raises(wi.InstallError):
        wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 1800, home=str(tmp_path), run=Recorder())
    assert not (tmp_path / "Library" / "LaunchAgents" / "com.mnemotrace.watch.plist").exists()


def test_install_refuses_when_the_interpreter_cannot_import_the_watcher(tmp_path):
    """首次真实安装时发现的问题：plist 加载成功、`launchctl print` 正常，但每个 tick 都在报
    `No module named mnemotrace.connectors.watch`。preflight 必须在任何写入之前失败。"""
    rec = Recorder(fail=("import mnemotrace.connectors.watch",))  # 与 PREFLIGHT_CODE 匹配
    with pytest.raises(wi.InstallError, match="pip install -e"):
        wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 1800, home=str(tmp_path), run=rec, key="k")
    assert not (tmp_path / "Library").exists() and not (tmp_path / ".mnemotrace").exists()
    assert not any(c[0] == "launchctl" for c in rec.calls)
    with pytest.raises(wi.InstallError, match="pip install -e"):
        wi.install_systemd(PY, ARGV, 1800, home=str(tmp_path), run=rec, key="k")
    assert not (tmp_path / ".config").exists()


def test_install_launchd_bootstrap_failure_is_an_error(tmp_path):
    with pytest.raises(wi.InstallError):
        wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 1800, home=str(tmp_path),
                           run=Recorder(fail=("bootstrap",)), key="k")


def test_uninstall_launchd_removes_plist_and_purge_removes_the_rest(tmp_path):
    rec = Recorder()
    wi.install_launchd("com.mnemotrace.watch", PY, ARGV, 1800, home=str(tmp_path), run=rec, key="k")
    p = wi.paths_for(str(tmp_path))
    for f in ("state", "lock", "log"):
        os.makedirs(os.path.dirname(p[f]), exist_ok=True)
        open(p[f], "w").close()

    out = wi.uninstall_launchd("com.mnemotrace.watch", home=str(tmp_path), run=rec)
    plist = tmp_path / "Library" / "LaunchAgents" / "com.mnemotrace.watch.plist"
    assert not plist.exists() and out["removed"] == [str(plist)]
    assert rec.calls[-2] == ["launchctl", "bootout", f"{wi._domain()}/com.mnemotrace.watch"]
    assert rec.calls[-1] == ["launchctl", "print", f"{wi._domain()}/com.mnemotrace.watch"]
    assert out["unloaded"] is True
    assert os.path.exists(p["key"]) and os.path.exists(p["state"])  # 未被清除

    out = wi.uninstall_launchd("com.mnemotrace.watch", purge=True, home=str(tmp_path), run=rec)
    # --install 创建了日志目录；purge 也要把它收回去，这样 ~/.mnemotrace 就和安装前一样。
    log_dir = os.path.dirname(p["log"])
    assert set(out["removed"]) == {p["key"], p["state"], p["lock"], p["log"], log_dir}
    for f in ("key", "state", "lock", "log"):
        assert not os.path.exists(p[f])
    assert not os.path.exists(log_dir)


def test_uninstall_launchd_waits_for_an_in_flight_tick(tmp_path, monkeypatch):
    """bootout 返回时 launchd 未必已拆掉正在运行的 tick；`launchctl print` 仍会短暂成功。
    由于 RunAtLoad 的存在，这正是 --install 之后片刻的正常状态。"""
    class SlowTeardown(Recorder):
        def __init__(self, polls_until_gone: int):
            super().__init__()
            self.pending = polls_until_gone

        def __call__(self, cmd):
            if cmd[:2] == ["launchctl", "print"] and self.pending > 0:
                self.pending -= 1
                self.calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return super().__call__(cmd)

    naps: list[float] = []
    monkeypatch.setattr(wi, "_sleep", naps.append)
    rec = SlowTeardown(polls_until_gone=2)
    out = wi.uninstall_launchd("com.mnemotrace.watch", home=str(tmp_path), run=rec)
    prints = [c for c in rec.calls if c[:2] == ["launchctl", "print"]]
    assert out["unloaded"] is True and len(prints) == 3 and len(naps) == 2

    rec = SlowTeardown(polls_until_gone=10 ** 6)  # tick 比等待更持久
    out = wi.uninstall_launchd("com.mnemotrace.watch", home=str(tmp_path), run=rec, wait_s=0)
    assert out["unloaded"] is False


def test_systemd_units_parse_and_install_enables_timer(tmp_path):
    service, timer = wi.render_systemd_units(PY, ARGV, 1800, "/h/log")
    cp = configparser.ConfigParser()
    cp.read_string(service)
    assert cp["Service"]["Type"] == "oneshot"
    assert cp["Service"]["ExecStart"].startswith(f"{PY} -m mnemotrace.connectors.watch --once")
    cp = configparser.ConfigParser()
    cp.read_string(timer)
    assert cp["Timer"]["OnBootSec"] == "2min" and cp["Timer"]["OnUnitActiveSec"] == "1800s"
    assert cp["Timer"]["Persistent"] == "true"

    rec = Recorder()
    info = wi.install_systemd(PY, ARGV, 1800, home=str(tmp_path), run=rec, key="k")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / "mnemotrace-watch.service").exists() and (unit_dir / "mnemotrace-watch.timer").exists()
    assert rec.calls[0] == [PY, "-c", wi.PREFLIGHT_CODE]  # preflight，与 launchd 相同
    assert rec.environments[0]["HOME"] == str(tmp_path)
    assert rec.calls[1:] == [["systemctl", "--user", "daemon-reload"],
                             ["systemctl", "--user", "enable", "--now", "mnemotrace-watch.timer"]]
    assert info["loaded"] is True

    rec = Recorder()
    wi.uninstall_systemd(purge=True, home=str(tmp_path), run=rec)
    assert not (unit_dir / "mnemotrace-watch.timer").exists()
    assert rec.calls[0] == ["systemctl", "--user", "disable", "--now", "mnemotrace-watch.timer"]
    assert not os.path.exists(wi.paths_for(str(tmp_path))["key"])


def test_cron_line():
    line = wi.render_cron_line(PY, ARGV, 1800, "/h/log")
    assert line.startswith("*/30 * * * * ") and line.endswith(">> /h/log 2>&1")
    assert f"{PY} -m mnemotrace.connectors.watch --once --url http://127.0.0.1:8000" in line
    assert wi.render_cron_line(PY, ARGV, 7200, "/h/log").startswith("0 */2 * * * ")


def test_cli_install_dry_run_prints_plist_and_writes_nothing(tmp_path, monkeypatch, capsys):
    from mnemotrace.connectors import watch

    orig = wi.paths_for
    monkeypatch.setattr(wi, "paths_for", lambda home=None: orig(str(tmp_path)))
    preflight = Mock(return_value=None)
    monkeypatch.setattr(wi, "preflight_import", preflight)
    code = watch.main(["--space-id", "synthetic-space", "--install", "--scheduler", "launchd", "--label", "com.mnemotrace.watch.t",
                       "--interval", "1h", "--url", "http://127.0.0.1:9", "--key", "test", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0 and "[dry-run] preflight:" in out
    preflight.assert_called_once_with(sys.executable)
    doc = plistlib.loads(out.split("\n[dry-run]")[0].encode("utf-8"))
    assert doc["Label"] == "com.mnemotrace.watch.t" and doc["StartInterval"] == 3600
    assert "--key-file" in doc["ProgramArguments"] and "test" not in doc["ProgramArguments"]
    assert not (tmp_path / ".mnemotrace").exists() and not (tmp_path / "Library").exists()


def test_cli_install_cron_writes_key_file_and_never_edits_crontab(tmp_path, monkeypatch, capsys):
    """cron 没有可调用的加载器，因此对 cron 而言 --install 只写 key 文件一件事；打印出的行
    从该文件读取密钥，且在第一个 tick 之前它必须以 0600 权限存在。"""
    from mnemotrace.connectors import watch

    orig = wi.paths_for
    monkeypatch.setattr(wi, "paths_for", lambda home=None: orig(str(tmp_path)))
    code = watch.main(["--space-id", "synthetic-space", "--install", "--scheduler", "cron", "--interval", "30m",
                       "--url", "http://127.0.0.1:9", "--key", "test"])
    out = capsys.readouterr().out
    assert code == 0 and "crontab -e" in out and "*/30 * * * * " in out
    key = tmp_path / ".mnemotrace" / "watch.key"
    assert key.read_text() == "test\n"
    if os.name == "posix":  # Windows 没有 POSIX 权限位；0600 在那里无法表示
        assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert "--key-file" in out and "--key test" not in out  # 打印的行只携带文件路径，绝不携带密钥
    # 没有密钥且也没有 key 文件时，安装应拒绝，而不是打印一行根本无法运行的命令
    key.unlink()
    assert watch.main(["--space-id", "synthetic-space", "--install", "--scheduler", "cron", "--url", "http://127.0.0.1:9"]) == 1
