# Python API

面向嵌入式应用的公共入口为 `from mnemotrace import Memory, Config`；还导出 Episode、Fact、Entity 和 Relation。核心不需要第三方运行时依赖。入门步骤见[快速开始](../quickstart.md)。

## 构造与运行配置

`Memory(config=None, embedder=None, llm=None, reranker=None, vector_store_factory=..., graph_store_factory=...)`

默认 Config、HashingEmbedder、规则抽取、内存文档/向量/图存储。传入 LLM 可启用相关模型路径；可选实现通过各自 extras 安装。参数字段和默认值见[配置参考](configuration.md)及 [Config](../../mnemotrace/config.py)。

直接使用 Memory 不读取服务端的凭据映射，也不自动提供鉴权、服务级锁或逐操作保存。并发与持久化由应用管理，或改用 [MemoryService](../../mnemotrace/service.py)/HTTP。

## 写入与导入

| 方法 | 主要参数 | 返回与语义 |
| --- | --- | --- |
| `add` | content；user_id、session_id、speaker、event_time；consolidate=False | Episode；保存事件，可选择立即整合 |
| `remember` | content；user_id、session_id、scope="auto" | dict；路由 long/working，长期内容仍需显式整合 |
| `consolidate` | episodes=None | 计数字典；None 处理 pending 事件 |
| `consolidate_full` | fact_episodes=None、summary_episodes=None | 计数字典；整合、摘要与反映更新，可分别指定两组事件 |
| `import_messages` | sessions、user_id；consolidate=None、summarize=True、dedupe=True 等 | 计数字典；导入已解析会话 |
| `import_data` | data、format="auto"、user_id、session_id；其余导入选项 | 计数字典；先解析再导入 |
| `import_snapshot` | snapshot、user_id、dedupe=True | dict；导入结构化导出，不等同于打开存储目录 |

结构化快照按版本 1 的必要字段整批校验，错误抛出 `ValueError`，不部分导入。迁移保留目标用户的运行设置；字段、来源编号和替代链语义见[结构化迁移导入](../architecture/memory.md#结构化迁移导入)。

`scope="auto"` 按规则识别短时状态，`long` 强制走长期内容路径，`working` 跳过长期画像事实提取。Python 记忆实例中的写入不会自动保存到磁盘。

## 检索与上下文

| 方法 | 主要参数 | 返回 |
| --- | --- | --- |
| `search` | query、user_id、as_of=None、top_k=None | SearchResult |
| `as_of` | query、when、user_id、top_k=None | SearchResult；时间查询便捷入口 |
| `lean_context` | query、user_id；as_of、session_id、redact_sensitive；n_summaries、n_facts、n_chunks、char_budget 等 | 按预算组合的上下文字符串 |
| `context_for` | query、user_id；as_of、top_k、k_chunks 及按需增强选项 | 上下文字符串 |
| `retrieve_episodes` | query、user_id、k=5、pool=None、as_of=None | 检索到的原始 Episode |
| `history` | subject、predicate、user_id | 同一槽位的 Fact 列表 |
| `profile` | user_id | 属性字典 |
| `structured_profile` | user_id | 结构化画像字典 |
| `build_persona` | user_id | 派生画像文本 |

<a id="profile-evidence"></a>结构化画像条目的 `evidence` 包含 `kind`、`source_count` 和 `reinforcement_count`。`source_count` 是事实 provenance 中不同来源 Episode ID 的数量；手动添加且没有来源、或仅从身份消解补全的姓名，来源数为 0。`reinforcement_count` 是事实的 `access_count`，记录检索使用时的强化次数；手动设定和多来源事实同样返回该次数，身份消解补全的姓名为 0。`kind` 优先用 `user` 表示手动设定；非手动事实在来源不足两条且曾被检索强化时为 `reinforced`，其余为 `mentions`。三者分别表达状态、来源数和强化次数，读取画像不会增加强化次数。

SearchResult 包含 `query`、`facts`、`scores`、`via` 和 `abstained`。`answer()` 返回已生成答案或最高事实的 object/text；拒答时返回固定提示。`top()` 返回首条 Fact 或 None。它不是对任何问题都调用外部模型的通用聊天接口。

`as_of`、时间字段使用 epoch 秒。查询需要沿用正确的 user_id；携带 session_id 才能在相应上下文中包含该 session 的工作记忆。Python `graph_data()` 默认包含敏感事实支持的关系，与 HTTP 的安全视图默认值不同。

## 事实、工作状态与策略

| 方法 | 用途 |
| --- | --- |
| `add_fact(subject, predicate, object, user_id=..., valid_at=None, sensitive=None, category=None)` | 返回用户来源 Fact |
| `update_fact(fact_id, ...)` | 修改内容、分类或敏感标记，返回 Fact 或 None；statement 不与三元组字段混用 |
| `delete_fact(fact_id)` | 删除指定事实，返回 bool |
| `remember_working(content, user_id=..., session_id=..., kind="state", ttl_seconds=None)` | 返回 WorkingMemory |
| `working_memory(user_id=..., session_id=None, as_of=None, kind=None)` | 活跃工作条目 |
| `clear_session(user_id=..., session_id=...)` | 清理该 session 工作记忆，返回数量；不是服务层 close_session |
| `consume_working(wm_id)`、`sweep_working(as_of=None)` | 标记消费或清理过期/已消费工作条目 |
| `get_focus()`、`set_focus(track=None, mute=None)` | 查询或更新关注点，None 保持不变，[] 清空对应列表 |
| `get_policy()`、`set_policy(**fields)` | 查询或覆盖抽取、摘要与画像策略 |
| `graph_data(user_id=..., as_of=None, include_sensitive=True, q="", live_only=False, limit=None)` | 实体与关系字典 |
| `pending_conflicts(user_id=...)`、`resolve_conflict(conflict_id, keep="newer")`、`dismiss_conflict(conflict_id)` | 查询和裁决待确认冲突 |

## 保存与模型切换

```python
from mnemotrace import Memory

memory = Memory.open("/path/to/owned-memory-store")
memory.add("I prefer Python.", user_id="user123")
memory.consolidate()
memory.save()
```

`save(path=None)` 使用指定目录或上次打开/保存的路径；没有路径时抛出 ValueError。`open(path, allow_mismatch=False, **kwargs)` 是类方法，支持与构造函数一致的运行组件参数。

异常类型从 `mnemotrace.store.persist` 导入：PersistenceError、StoreFormatError、IncompatibleStoreError、DimensionMismatchError、EmbedderMismatchError。当前恢复条件和重嵌入流程见[持久化契约](../architecture/memory.md)。

当前格式缺少必要信息、类型非法或缺少提交编号的历史存档会被明确拒绝，不自动升级。自定义 embedder 必须提供正整数 `dim`；`allow_mismatch` 只放宽模型匹配，不跳过格式校验。

## 扩展与研究入口

摘要和派生层操作还包括 `summarize_episodes`、`retrieve_summaries`、`reflect`、`procedural`；热层管理包括 `evict_cold`、`apply_focus`。这些方法改变派生状态或工作集，调用方负责后续保存及必要验证。

精确签名、类型和行为以 [Memory](../../mnemotrace/memory.py)、[记录类型](../../mnemotrace/types.py) 和对应[测试](../../tests/)为核对来源。HTTP/MCP 的方法、默认值及能力不与嵌入式 API 一一对应。
