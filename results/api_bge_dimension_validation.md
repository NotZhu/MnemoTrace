# API BGE-M3 启动维度修复验收

日期：2026-10-08。范围：`LiteLLMEmbedder` 初始维度及 `MemoryService` 保存/重启加载；不是 benchmark，不产生准确率、token 或延迟主张。

## 离线回归

```bash
python -m pytest tests/test_api_embedder.py tests/test_persist_safety.py tests/test_reembed.py tests/test_service_paths.py -q
```

结果：40 项通过。新增 `tests/test_api_embedder.py` 9 项，全部模拟 LiteLLM API：

- BGE-M3 两种模型标识在首次请求前都是 1024 维。
- OpenAI-compatible 工厂保留模型路由，不替换实际 BGE 模型。
- 原来的 OpenAI 默认维度与 SDK 显式 `dim` 参数行为保留。
- 服务写入、保存、创建新服务、加载的整个生命周期通过；重启加载零 API 调用、零 reembed。
- 不同模型或不同维度存储仍拒绝加载。

修复前新增测试实际复现 `store embedding_dim=1024 does not match current embedder dim=1536`；修复后通过。

## 完整测试

为避免当前工作目录的私有 `.env` 污染默认配置/开放模式测试，完整运行在临时工作目录，环境显式禁用在线模型。可用以下 Python 脚本在仓库根目录复现（已安装 pytest）：

```python
import os
import sys
import tempfile
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root))
os.environ.update(
    ENGRAM_EMBEDDER="hashing", ENGRAM_LLM="", ENGRAM_ANSWERER="", ENGRAM_VISION_LLM=""
)
import pytest

with tempfile.TemporaryDirectory(prefix="engram_offline_tests_") as work:
    os.chdir(work)
    result = pytest.main([str(root / "tests"), "-q", "-o", "addopts="])
    os.chdir(root)
    sys.exit(result)
```

实际输出：

```text
619 passed, 5 skipped in 45.68s
```

根目录下的第一次全量运行有 4 项既有鉴权测试失败：测试删除环境变量后，本机 `.env` 又注入 API key，与测试的开放模式假设相冲突。隔离工作目录后通过；未修改鉴权实现或私有配置。

`python examples/quickstart.py` 在相同离线模型环境下通过。未连接硅基流动/DeepSeek，未读取或迁移真实记忆，不代表在线凭据已验证。
