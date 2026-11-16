# Python API

面向产品集成的 Python 入口是 `from mnemotrace import Memory`。先打开运行角色的数据库连接，再用 `Spaces(database).authorize(user_id, space_id)` 获取授权上下文并构造 `Memory(database, context)`。连接由调用方负责关闭。详见 [Postgres 存储契约](../architecture/storage-contracts.md)。

`from mnemotrace import Config, MemoryEngine` 用于离线验证。核心不需要第三方运行时依赖，支持 Episode、Fact、Entity 和 Relation。

## Postgres 产品入口

`Memory` 绑定一个授权空间，调用方法时无需再次传 `user_id`。连接由直连调用方持有，用 `Database` 上下文管理器覆盖完整的调用范围。传入 `llm=` 启用抽取；通过 `config=Config(...)` 覆盖应用默认值。模型和连接的完整示例见 [Quickstart](../quickstart.md)。

从存储契约模块导入消息输入、查询条件和任务回执类型。

```python
from mnemotrace.store.contracts import FactQuery, MessageInput, OperationResult
```

| 方法 | 用途 |
| --- | --- |
| `remember(value, session_id, *, wait=False, timeout=30, message_id=None, event_time=None)` | 字符串转为一条用户消息，也可传 `MessageInput` 或消息字典的序列 |
| `operation(operation_id)`、`retry_operation(operation_id)` | 查询或重新排队原任务，返回 `OperationResult` 或 `None` |
| `list_operations(*, session_id=None, after_id=None, limit=100)` | 分页返回操作对象。下一页使用最后的 `operation_id` |
| `search(query, *, as_of=None, top_k=None, facts=None)`、`get_episode(episode_id)` | 检索候选事实及来源，或显式读取原文 |
| `get_fact(fact_id)` | 在授权空间按 ID 读取单条事实，包含历史和未来生效的事实。未找到时返回 `None` |
| `set_fact_sensitive(fact_id, sensitive)` | 修改共享敏感标记 |
| `profile()` | 读取七栏画像缓存、待更新数量和刷新状态，不调用模型 |
| `refresh_profile()` | 创建或复用空间级画像操作，返回 `OperationResult`；通过 worker 执行 |

`remember` 返回 `source_ids`、含会话顺序的 `messages` 和 `task`。
`wait=True` 可等待 worker 发布，超时返回 `timed_out`，终态失败返回 `failed` 和任务错误。
整批重传沿用原顺序与消息 ID，字符串可指定稳定的 `message_id`。

`SearchResult.status` 为 `answered`、`evidence_only` 或 `no_evidence`。自由文本返回候选事实和来源。使用 `FactQuery` 同时指定 `subject` 和 `predicate`，且完整筛选结果只有一个不同值时，`answer()` 返回该值。

Memory 在授权空间内按记录提交。现有记录与向量按文本版本、模型标识及维度关联，查询按当前配置筛选匹配的向量。备份和恢复见[部署与恢复流程](../../deploy/README.md#备份与恢复)。更多方法见 [Memory 实现](../../mnemotrace/memory.py)。

## 离线算法入口与运行配置

`MemoryEngine(config=None, embedder=None, llm=None, reranker=None, vector_store_factory=..., graph_store_factory=...)`

算法工作集使用 Config 和内存文档及图存储，默认使用无向量检索。可显式传入 FastEmbedEmbedder 等嵌入器。传入 LLM 才能抽取自然语言事实；可选实现通过各自 extras 安装。参数字段和默认值见[配置参考](configuration.md)及 [Config](../../mnemotrace/config.py)。

直接使用 MemoryEngine 只在进程内维护算法状态。产品调用使用 Memory 或 HTTP。

## 离线写入

`MemoryEngine.remember(value, session_id, user_id="default", *, message_id=None, event_time=None)`
同步写入和抽取，返回来源 ID、消息状态及事实和摘要计数。字符串按用户消息处理，消息列表保留 user/assistant 角色。
已保存的消息 ID 和原批内容或顺序冲突时，抛出 `MessageConflictError`。

算法测试可用 `add(..., consolidate=False)` 登记原文，再用 `consolidate()` 处理待整合的事件。

## 离线检索与上下文

| 方法 | 主要参数 | 返回 |
| --- | --- | --- |
| `search` | query、user_id、as_of=None、top_k=None | SearchResult |
| `as_of` | query、when、user_id、top_k=None | SearchResult；时间查询便捷入口 |
| `lean_context` | query、user_id；as_of、redact_sensitive；n_digests、n_facts、char_budget 等 | 按预算组合的上下文字符串 |
| `context_for` | query、user_id；as_of、top_k、k_digests 及按需增强选项 | 上下文字符串 |
| `retrieve_digests` | query、user_id、k=8、as_of=None | 不可变的摘要证据列表 |
| `get_episode` | episode_id、user_id=default | 原文记录字典或 None；仅含身份、来源时间和完整 content |
| `history` | subject、predicate、user_id | 同一槽位的 Fact 列表 |
| `profile` | user_id | 结构化画像缓存与刷新状态 |
| `refresh_profile` | user_id | 创建或复用显式刷新任务，返回 OperationResult |

<a id="profile-evidence"></a>画像条目包含 text、fact_ids 和服务端计算的 fact_count。
首次生成需调用 `refresh_profile`。离线可用 `wait_profile` 等待，Postgres 可按操作 ID 查询 worker 进度。
引用事实发生变化时，对应条目退出展示。分批预算和恢复行为见[画像缓存](../architecture/memory.md#画像缓存与显式刷新)。

SearchResult 包含 `query`、`facts`、`scores`、`via` 和 `abstained`。`answer()` 返回已生成答案或最高事实的 object/text；拒答时返回固定提示。`top()` 返回首条 Fact 或 None。它不是对任何问题都调用外部模型的通用聊天接口。

`as_of`、时间字段使用 epoch 秒。查询需要沿用正确的 user_id；recall 和上下文组装不接收 session_id；会话标识用于写入、浏览和处理刷新。Python `graph_data()` 默认包含敏感事实支持的关系，与 HTTP 的安全视图默认值不同。图谱由引用两个实体且符合端点类型约束的事实投影。名称用于查找，实体身份由 ID 区分。

## 离线事实与策略

| 方法 | 用途 |
| --- | --- |
| `add_fact(subject, predicate, object, user_id=..., text=..., ...)` | 构造或复用 Fact，`text` 为必填关键字参数 |
| `Memory.set_fact_sensitive(fact_id, sensitive)` | 修改事实的共享敏感标记，返回 Fact 或 None |
| `delete_fact(fact_id)` | 删除指定事实，返回 bool |
| `get_focus()`、`set_focus(track=None, mute=None)` | 查询或更新关注点，None 保持不变，[] 清空对应列表 |
| `get_policy()`、`set_policy(**fields)` | 查询或覆盖抽取、摘要与画像策略 |
| `graph_data(user_id=..., as_of=None, include_sensitive=True, q="", live_only=False, limit=None)` | 返回 nodes、edges、has_more；见下述图谱完整性契约 |

图谱的状态、搜索、敏感内容与时间筛选均在 `limit` 前执行；正数 `limit` 限制返回关系数，节点仅包含这些关系的端点。`has_more=true` 表示当前查询条件下仍有未返回的匹配关系；恰好达到上限不表示截断。不限量查询返回全部匹配关系，`has_more=false`。该字段不提供命名空间总数，展示数量仅描述已返回的图。

字段及编辑规则见[分类与对象类型](../architecture/memory.md#分类与对象类型)。

产品摄入使用 `Memory.remember`。`add_fact` 用于离线工作集。省略生效时间时，同槽位、同宾语、同正文且当前有效的断言会复用原事实，保留已有分类和留存策略，敏感标记取逻辑或。显式指定生效时间时，复用还要求起点相同。

## 离线扩展与研究入口

`consolidate()` 联合生成事实及符合条件的来源摘要，计数包含新增摘要数。`retrieve_digests()` 读取摘要，`evict_cold()` 和 `apply_focus()` 管理检索工作集。

精确签名、类型和行为以 [MemoryEngine](../../mnemotrace/memory_engine.py)、[记录类型](../../mnemotrace/types.py) 和对应[测试](../../tests/)为核对来源。HTTP/MCP 的方法、默认值及能力不与嵌入式 API 一一对应。

## 模型抽取与错误契约

模型的 `complete(prompt, **kwargs)` 继续返回字符串。需要结束状态与用量时使用
`complete_result(prompt, **kwargs) -> Completion`；`Completion` 从 `mnemotrace.llm`
导入，包含 `text`、`finish_reason` 和 `usage`。结束状态或 token 数未提供时为 `None`，
不能将其解释为零用量或已确认正常结束。LiteLLM 适配器和 `CachedLLM` 保留这些信息；
仅实现字符串接口的自定义适配器仍可使用，但只能通过内容校验检测不完整响应。

抽取模型输出 `{"facts": [...], "digests": [...]}`。facts 包含主体、关系、取值、正文、来源及时间/类别/敏感性/生命周期标签；digests 每项包含 source_id 和 text，按请求中的 digest_required 为来源生成摘要。两个数组均为必需。具体字段和严格校验见 [planning.py](../../mnemotrace/consolidate/planning.py)。自定义 extract_system 必须遵循该协议。

离线 `MemoryEngine.consolidate()` 空任务返回零计数；非空任务要求 LLM。整批模型响应全部通过校验后才写入事实。模型调用或校验失败不改变身份或完成标记；存储写入失败可能留下部分事实，已完成的窗口范围保留，未完成范围继续待处理；重试跳过完成范围并消解重复事实。完整空结果与失败分别处理，截断 JSON 不做部分抢救。

对话写入使用消息身份和联合任务，同 ID 的重传复用处理结果。联合处理完成后可读取事实与符合条件的摘要。

离线抽取异常包括 LLMNotConfiguredError、ExtractionError、ExtractionResponseError、ExtractionTruncatedError、ExtractionConflictError。未成功范围保留原文，可以重新 consolidate。截断错误 code 为 extraction_truncated；同一命名空间的并发写作废在途抽取时抛 ExtractionConflictError（code 为 extraction_conflict），内核会先基于新快照自动重试一次。窗口输入、来源与范围记录契约见[记忆架构](../architecture/memory.md#窗口抽取与处理范围)。异常携带 code、stored_raw、episode_ids，不包含模型原始响应。Postgres 已接收任务的进度和错误从操作回执读取。

模型比较候选断言并返回 `conflict`、`coexists` 或 `unknown`。PostgreSQL 对符合语义去重条件的候选也允许 `duplicate`。程序计算有效区间，遇到不确定、互相矛盾的判定或无法排序的时间重叠时，保留原文并放弃受影响的新候选。


## 摘要与显式原文读取

Postgres 的 `remember(wait=True)` 等待事实和符合条件的摘要一起发布。失败时保留原文。`retry_operation(operation_id)` 将原任务重新排队。

来源检索和聚合回退只使用摘要，自动上下文只包含事实和摘要。通过 get_episode 显式读取来源 ID 对应的原文。
私有 JSON 导出保留摘要的 `digest_written_at` 供追溯。历史摘要检索按原始来源的事件时间筛选，使用当前保存的摘要。
Fact 仍从原文抽取且独立检索；历史事件摘要不追加后续事实。空间所有者可浏览和导出原文。

使用 `memory.aggregate_facts("visited", operation="count_distinct")` 统计不同对象，使用 `memory.follow_path("user", ["sister", "works_at"])` 获取路径和支持事实。两者可通过 `FactQuery` 设置有效时间、认知时间、事实状态和脱敏条件。
