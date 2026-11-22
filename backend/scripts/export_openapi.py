"""从本地 HTTP v1 应用导出 OpenAPI 契约。"""

import argparse
import json
from pathlib import Path

from mnemotrace_next.runtime_settings import RuntimeSettings
from mnemotrace_next.server.app import create_app
from mnemotrace_next.server.settings import Settings


def main() -> None:
    """导出 OpenAPI JSON，无需启动 ASGI 生命周期。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    app = create_app(Settings(RuntimeSettings("dbname=unused")))
    args.output.write_bytes(
        (json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


if __name__ == "__main__":
    main()
