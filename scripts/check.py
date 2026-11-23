"""运行后端的静态检查和测试。"""

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """依次执行静态检查和后端测试。

    Returns:
        全部通过时为 0，否则为首个失败命令的退出码。
    """
    root = Path(__file__).resolve().parents[1]
    paths = ("mnemotrace", "tests", "scripts", "eval")
    for args in (
        ("ruff", "check", *paths),
        ("ruff", "format", "--check", *paths),
        ("mypy",),
        ("pytest",),
    ):
        check = subprocess.run([sys.executable, "-m", *args], cwd=root, check=False)
        if check.returncode:
            return check.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
