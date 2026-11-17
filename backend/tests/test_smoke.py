"""检查安装后的后端包能否运行。"""

import subprocess
import sys


def test_package_smoke() -> None:
    """在隔离的 Python 进程中运行后端包。"""
    smoke = subprocess.run(
        [sys.executable, "-I", "-m", "mnemotrace_next"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert smoke.stdout == "ok\n"
