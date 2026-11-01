# 快速开始

本页使用 Python 库完成模型抽取、检索和重开存储。需要 Python 3.10 或更高版本，以及已配置的 LLM。示例使用 DeepSeek，运行前设置 DEEPSEEK_API_KEY；命令行示例另需设置 MNEMOTRACE_LLM=deepseek。

## 安装与运行

在仓库根目录执行：

```sh
python -m pip install ".[llm]"
# Set MNEMOTRACE_LLM and the provider credentials before running.
mnemotrace-quickstart
```

源码检出也可以直接运行 `python examples/quickstart.py`，已安装环境可运行 `python -m mnemotrace.quickstart`。完整示例展示整合、历史与时间查询；其中的名字和会话内容为演示数据。

## 写入并查询

```python
from mnemotrace import Memory
from mnemotrace.llm.providers import make_llm

memory = Memory(llm=make_llm("deepseek"))
memory.remember_messages([
    {"message_id": "m1", "role": "user", "content": "I live in Paris."}
], user_id="user123", session_id="demo")
processing = memory.flush_messages(user_id="user123")
assert processing["ok"]
result = memory.search("Where do I live?", user_id="user123")

print(result.answer())      # Paris
print(memory.profile("user123"))  # {'lives_in': 'Paris'}
```

`remember_messages()` 按消息 ID 保存完整原文及处理任务，`flush_messages()` 固定当前范围并联合提取事实、工作记忆及长消息摘要。来源时间可省略，此时明确为未知；历史对话指定 `mode="historical"`。查询时沿用写入时的 `user_id`。事实由 LLM 从原话生成，默认哈希嵌入主要捕捉词汇重合；真实语料的抽取和召回质量需要单独评测。非对话原文仍可使用 `add()` / `consolidate()`。

## 保存与重开

在同一程序中继续执行：

```python
from tempfile import TemporaryDirectory

with TemporaryDirectory(prefix="mnemotrace-example-") as directory:
    memory.save(directory)
    reopened = Memory.open(directory)
    assert reopened.search("Where do I live?", user_id="user123").answer() == "Paris"
```

临时目录会在示例结束时删除。实际应用应改用自己管理的持久目录，并在写入后显式 `save()`；`Memory.open(path)` 不会自动保存后续修改。服务层会负责命名空间保存，见[部署说明](../deploy/README.md)。

存储记录嵌入模型身份和维度。重开已有库时使用一致的 embedder；更换模型的受支持流程见[持久化契约](architecture/memory.md#嵌入模型一致性)。

## 下一步

- 处理会话、导入与隐私：[使用指南](guides/usage.md)。
- 接入现有 Agent：[Agent 接入](guides/agents.md)。
- 查询具体方法：[Python API](reference/python-api.md)。
- 运行 HTTP 服务和控制台：[部署与运维](../deploy/README.md)。
