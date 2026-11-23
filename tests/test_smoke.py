"""检查安装后的后端包能否导入。"""

import subprocess
import sys


def test_package_smoke() -> None:
    """在隔离的 Python 进程中导入已安装的包。"""
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", "from mnemotrace import Memory"],
        check=True,
    )
