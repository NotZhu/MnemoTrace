# Python API

公开入口是 `from mnemotrace import Memory, Config`。完整签名见 [memory.py](../../mnemotrace/memory.py)，类型见 [types.py](../../mnemotrace/types.py)。本页说明主要调用及容易混淆的边界。

## 创建与持久化

```python
from mnemotrace import Config, Memory

memory = Memory(config=Config())
episode = memory.add("My name is Alex.", user_id="user123")
memory.consolidate()
memory.save("./my-store")
restored = Memory.open("./my-store")
```

`Memory` 默认内存、`hashing`、规则抽取，无服务和 `key`。`save` 保存状态，`open` 以传入后端恢复；模型身份或维度不匹配明确失败。需要共享进程间读改存时使用 [MemoryService](../architecture/service.md)，裸 `Memory` 不提供相同事务边界。

## 写入与导入

| 方法 | 返回与语义 |
| --- | --- |
| `add(content, user_id, session_id, ...)` | `Episode`；追加无损原文，默认不立即整合 |
| `remember(content, ..., scope="auto")` | 结果 dict；按 `auto`/`long`/`working` 路由并保留来源 |
| `consolidate(episodes=None)` | 计数 dict；抽取待处理 `Episode`、冲突失效与图谱更新 |
| `consolidate_full(...)` | 事实与摘要整合，用于需要分别选择证据的流水线 |
| `import_data(data, format="auto", ...)` | 接收 `chatgpt/messages/records/jsonl/transcript` 等来源 |
| `import_messages(sessions, ...)` | 按会话导入、批量嵌入与去重；保留来源事件时间 |
| `import_snapshot(snapshot, ...)` | 恢复导出记录，保留双时间并重映射来源与替代链 |
| `add_fact(subject, predicate, object, ...)` | `Fact`；用户明确断言，保护其权威性 |

`user_id` 默认 `default`，精确匹配原始值；`session_id` 用于溯源与生命周期，不提供跨用户编号合并。`agent_session` 的逐轮抽取门和降级见[生命周期](../architecture/memory.md)。

## 查询与时间

```python
result = restored.search("What is my name?", user_id="user123")
print(result.answer())
context = restored.lean_context("What is my name?", user_id="user123")
```

`search` 返回 `SearchResult`，包含 `facts`、`scores`、`via` 和 `abstained`；通过 `answer()` 获取答案文本。`lean_context` 返回给下游模型的证据字符串，组合事实、原文和派生层，不等同于完整历史。

`as_of(query, when, ...)` 返回某一时间条件下的 `SearchResult`，when 是 epoch 秒。它转交 `search(as_of=when)`，没有独立 T / K 参数。`history(subject, predicate, ...)` 返回槽位历史；时序语义见[生命周期](../architecture/memory.md#对象与时间)。

`retrieve_episodes` 和 `retrieve_summaries` 分别检索原始会话与摘要。`redact_sensitive=True` 的 `lean_context` 只提供已分类的非敏感结构化事实，不包含可能泄露自由文本的画像、摘要与原文；不能将敏感分类当作完整隐私审计。

## 管理与会话态

| 方法 | 语义 |
| --- | --- |
| `update_fact(id, ...)` | 返回 `Fact` 或 None；元数据和实际内容修改分别处理 |
| `delete_fact(id)` | 显式删除指定事实，返回 bool |
| `structured_profile / build_persona` | 结构化与叙事画像，来源数量不等于置信度 |
| `remember_working / working_memory` | 会话及 TTL 记忆，不自动进入长期画像 |
| `clear_session / consume_working / sweep_working` | 清除会话状态、消费单条或过期清扫 |
| `get_focus / set_focus` | `track`/`mute`；None 保持列表，[] 清空 |
| `get_policy / set_policy` | 用户策略覆盖默认抽取、摘要和画像规则 |
| `pending_conflicts / resolve_conflict` | 候选矛盾与显式 `newer/older/both` 选择 |
| `graph_data` | 当前用户的图视图，支持时间、搜索及敏感范围 |
| `reembed` | 显式重新生成向量；先备份，不能只修改 manifest |

结论的整句 `statement` 仅适用于 `decision/finding/lesson/open_question`；属性事实编辑三元组。元数据保存不复活历史，实际内容修改会成为用户断言。完整规则见[编辑与擦除](../architecture/memory.md#编辑与擦除)。

关闭客户端会话、持久化及可见审计由 `MemoryService.close_session` 和 `MemoryService.session_report` 提供，HTTP / MCP 对应入口见[接入指南](../guides/agents.md)。
