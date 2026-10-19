import os
import sys

import pytest

# 确保无论 pytest 以何种方式调用，仓库根目录都可导入。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def pytest_configure(config):
    config.addinivalue_line("markers", "dotenv: test configuration loading with an isolated .env file")


@pytest.fixture(autouse=True)
def isolated_runtime_configuration(monkeypatch, request):
    from mnemotrace.llm import providers

    for name in list(os.environ):
        if name.startswith("MNEMOTRACE_"):
            monkeypatch.delenv(name)
    for name in ("MNEMOTRACE_LLM", "MNEMOTRACE_ANSWERER", "MNEMOTRACE_VISION_LLM"):
        monkeypatch.setenv(name, "")
    # 单元测试只认自己的环境设置；供应商凭据不能从开发者的 .env 重新注入。
    if request.node.get_closest_marker("dotenv") is None:
        monkeypatch.setattr(providers, "load_dotenv", lambda *args, **kwargs: None)
