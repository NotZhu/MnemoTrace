#!/usr/bin/env python3
"""运行 MnemoTrace 的零配置验证路径。

本脚本刻意只使用 Python 标准库。它是贡献者在安装可选服务或测试依赖之前的快速
信心检查：源码 + 已安装模块的 quickstart、离线 eval、证据日志校验、paper 统计
以及标准库编译检查。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_REQUIREMENTS: dict[Path, tuple[str, ...]] = {
    ROOT / "results/headline_500.jsonl": ("engram_lean", "full_context"),
    ROOT / "results/bb_flash.jsonl": ("engram_lean", "full_context"),
    ROOT / "results/longmemeval_s_engram_lean_v2_final.jsonl": ("engram_lean",),
    ROOT / "results/longmemeval_s_volcano_doubao_deepseekjudge.jsonl": ("full_context",),
}
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
    # 仓库相对路径会对外打印（RESULTS.md 使用正斜杠），因此无论宿主 OS 如何都统一分隔符。
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def run_step(label: str, cmd: list[str]) -> None:
    printable = " ".join(_rel(Path(part)) if part.startswith(str(ROOT)) else part for part in cmd)
    print(f"\n==> {label}", flush=True)
    print(f"$ {printable}", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def public_evidence_logs() -> tuple[Path, ...]:
    results_md = (ROOT / "RESULTS.md").read_text(encoding="utf-8")
    linked = {
        ROOT / match
        for match in re.findall(r"\]\((results/[^)]+\.jsonl)\)", results_md)
    }
    return tuple(sorted(linked | set(EVIDENCE_REQUIREMENTS)))


def format_public_evidence_requirements(paths: tuple[Path, ...]) -> str:
    lines = ["\n==> public evidence requirements"]
    for path in paths:
        systems = EVIDENCE_REQUIREMENTS.get(path)
        if systems:
            lines.append(f"- {_rel(path)}: require {', '.join(systems)}")
        else:
            lines.append(f"- {_rel(path)}: missing required citable systems")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MnemoTrace's zero-setup smoke checks.")
    parser.add_argument(
        "--skip-paper-stats",
        action="store_true",
        help="skip recomputing committed paper statistics from result logs",
    )
    args = parser.parse_args(argv)

    py = sys.executable
    evidence_logs = public_evidence_logs()
    missing = [path for path in evidence_logs if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing public evidence log: {_rel(path)}", file=sys.stderr)
        return 1

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

    print(format_public_evidence_requirements(evidence_logs), flush=True)
    for path in evidence_logs:
        systems = EVIDENCE_REQUIREMENTS.get(path)
        if systems is None:
            print(f"missing evidence requirement for {_rel(path)}", file=sys.stderr)
            return 1
        system_args = [arg for system in systems for arg in ("--system", system)]
        run_step(
            f"public evidence validation: {_rel(path)}",
            [
                py,
                "eval/validate_results.py",
                "--expected-rows",
                "500",
                "--require-complete",
                *system_args,
                str(path),
            ],
        )
    if not args.skip_paper_stats:
        run_step("paper statistics", [py, "paper/compute_stats.py", "--bootstrap-samples", "100"])
    run_step("stdlib compile check", [py, "-m", "compileall", "-q", "mnemotrace", "eval", "examples", "tests"])

    print("\nOK zero-setup verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
