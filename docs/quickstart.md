# 快速开始

使用 Python 3.10 或更新版本，在仓库根目录运行：

```bash
python examples/quickstart.py
```

该示例不需要 API `key` 或第三方服务，使用内存存储、哈希嵌入和规则抽取，演示原始消息、事实整合、冲突失效与检索。

需要在项目中导入包时安装当前检出：

```bash
python -m pip install -e .
mnemotrace-quickstart
```

`mnemotrace-memory` 是发行包名，本仓库的安装说明不代表已有可用的 `PyPI/npm` 发布。

## 最小 Python 使用

```python
from mnemotrace import Memory

memory = Memory()
memory.add("My name is Alex and I work at Acme.", user_id="user123")
memory.add("I now work at Example Labs.", user_id="user123")
memory.consolidate()
result = memory.search("Where does Alex work?", user_id="user123")
print(result.answer())
```

`add` 保存 `Episode`，`consolidate` 抽取 `Fact`、处理冲突并更新图谱。明确更新使旧事实失效并保留历史；规则抽取只适合其覆盖的简单表达，不等同于真实会话理解。

数据按传入的原始 `user_id` 隔离。跨会话共享使用同一个用户编号；不同编号不会自动合并。完整 API 和双时间边界见 [Python 参考](reference/python-api.md)。

## 下一步

- 需要重启后保留数据：[存储与配置](reference/configuration.md#存储与嵌入)。
- 需要给多个 Agent 使用：[接入指南](guides/agents.md)。
- 需要 HTTP 与控制台：[部署说明](../deploy/README.md)，启动后访问 `http://127.0.0.1:8000/ui/`。
- 需要开发验证：[贡献指南](../CONTRIBUTING.md)。
