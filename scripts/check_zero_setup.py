#!/usr/bin/env python3
"""运行 MnemoTrace 的零配置验证路径。

本脚本刻意只使用 Python 标准库。它是贡献者在安装可选服务或测试依赖之前的快速
信心检查：源码与已安装模块的 quickstart、命令入口、离线评测、持久化
及标准库编译检查。文档导航和公开评测日志由对应检查工具独立验证。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_HELP_MODULES = (
    "mnemotrace.agent_doctor",
    "mnemotrace.agent_setup",
    "mnemotrace.connectors",
    "mnemotrace.mcp",
)
CONSOLE_HELP_CALLABLES = (
    (
        "mnemotrace-agent-bootstrap",
        "from mnemotrace.agent_setup import bootstrap_main; bootstrap_main(['--help'])",
    ),
)


def _rel(path: Path) -> str:
    # 操作输出统一使用正斜杠，便于跨平台复制命令。
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def run_step(label: str, cmd: list[str]) -> None:
    printable = " ".join(_rel(Path(part)) if part.startswith(str(ROOT)) else part for part in cmd)
    print(f"\n==> {label}", flush=True)
    print(f"$ {printable}", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MnemoTrace's zero-setup smoke checks.")
    parser.parse_args(argv)

    py = sys.executable

    with tempfile.TemporaryDirectory(prefix="mnemotrace_zero_setup_") as tmp:
        synthetic_log = Path(tmp) / "synthetic_durable.jsonl"
        run_step("source quickstart", [py, "examples/quickstart.py"])
        run_step("installed-module quickstart", [py, "-m", "mnemotrace.quickstart"])
        for module in CONSOLE_HELP_MODULES:
            run_step(f"console help: {module}", [py, "-m", module, "--help"])
        for label, snippet in CONSOLE_HELP_CALLABLES:
            run_step(f"console help: {label}", [py, "-c", snippet])
        run_step("offline synthetic eval", [py, "eval/harness.py"])
        run_step(
            "durable smoke eval",
            [py, "eval/harness.py", "--storage", "durable", "--out", str(synthetic_log)],
        )
        run_step("synthetic report", [py, "eval/report.py", str(synthetic_log)])

    run_step("stdlib compile check", [py, "-m", "compileall", "-q", "mnemotrace", "eval", "examples", "tests"])

    print("\nOK zero-setup verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
