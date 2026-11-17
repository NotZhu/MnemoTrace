"""运行后端的静态检查和测试。"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """依次执行检查，返回首个失败命令的退出码。"""
    backend = Path(__file__).resolve().parents[1]
    for args in (
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
        ("mypy",),
        ("pytest",),
    ):
        check = subprocess.run([sys.executable, "-m", *args], cwd=backend, check=False)
        if check.returncode:
            return check.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
