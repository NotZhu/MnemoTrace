"""忆痕 使用已配置 LLM 的快速上手示例的源码运行入口。

    python examples/quickstart.py

以安装包方式运行时，等价演示命令为：

    mnemotrace-quickstart
"""
from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemotrace.quickstart import main  # noqa: E402


if __name__ == "__main__":
    main()
