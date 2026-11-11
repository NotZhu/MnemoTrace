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
| `remember_raw(content, session_id)` | 保存原文并返回 Episode，无需抽取模型 |
| `remember(content, session_id, *, extract=True, processing="immediate")` | 接收笔记；`extract=False` 只保存原文，也可直接调用 `remember_raw` 获取 Episode |
| `remember_messages(session_id, messages, *, processing="buffered")` | 接收 `MessageInput` 序列，按各条消息的 `extract` 值创建任务 |
| `operation(operation_id)`、`retry_operation(operation_id)` | 查询或重试原操作，返回 `OperationResult` 或 `None` |
| `list_operations(*, session_id=None, after_id=None, limit=100)` | 分页返回操作对象；下一页用最后一个操作的 `operation_id` 作游标 |
| `flush_processing(session_id=None, timeout=30, *, retry_failed=False, include_digests=False)` | 推进并等待本次捕获范围内的任务 |
| `search(query, *, as_of=None, top_k=None, facts=None)`、`get_episode(episode_id)` | 检索候选事实及来源，或显式读取原文 |
| `add_fact(subject, predicate, object, text=..., ...)` | 写入人工事实 |
| `profile()` | 读取七栏画像缓存、待更新数量和刷新状态，不调用模型 |
| `refresh_profile()` | 创建或复用空间级画像操作，返回 `OperationResult`；通过 worker 或 `flush_processing()` 推进 |

`remember` 和 `remember_messages` 返回 `source_ids`、`operation_ids` 和 `operations`。缓冲接收只确认持久化，任务由独立 worker 或显式 `flush_processing` 推进。刷新返回 `completed`、`failed` 或 `timed_out`，仅完成时 `ok=True`。等待超时后可继续查询原操作。自动抽取缺少模型时在写入前失败。

`SearchResult.status` 为 `answered`、`evidence_only` 或 `no_evidence`。自由文本返回候选事实和来源。使用 `FactQuery` 同时指定 `subject` 和 `predicate`，且完整筛选结果只有一个不同值时，`answer()` 返回该值。

Memory 在授权空间内按记录提交。现有记录与向量按文本版本、模型标识及维度关联，查询按当前配置筛选匹配的向量。备份和恢复见[部署与恢复流程](../../deploy/README.md#备份与恢复)。更多方法见 [Memory 实现](../../mnemotrace/memory.py)。

## 离线算法入口与运行配置

`MemoryEngine(config=None, embedder=None, llm=None, reranker=None, vector_store_factory=..., graph_store_factory=...)`

算法工作集使用 Config 和内存文档及图存储，默认使用无向量检索。可显式传入 FastEmbedEmbedder 等嵌入器。传入 LLM 才能抽取自然语言事实；可选实现通过各自 extras 安装。参数字段和默认值见[配置参考](configuration.md)及 [Config](../../mnemotrace/config.py)。

直接使用 MemoryEngine 只在进程内维护算法状态。产品调用使用 Memory 或 HTTP。

## 离线写入与导入

对话使用 `MemoryEngine.remember_messages(messages, session_id, user_id="default", mode="live", extract=True)`。
每条消息包含 message_id、role、content 和可选 event_time，角色限定为 user/assistant。返回每条事件 ID、持久化顺序和处理状态；相同 ID 的冲突输入报 MessageConflictError。
内存算法入口可记录原文，并通过 `MemoryEngine.flush_messages` 驱动固定模型或配置的 LLM。持久写入及恢复由 Postgres 入口提供。

通用 transcript 仅在明确标出 `user` 或 `assistant` 时作为对话导入，其他文本保留原文。时间接受 epoch 秒、明确标注单位的数值或带时区的时间。

历史导入适配后逐条提交 historical 消息，超过入口批量上限时顺序分批。来源没有消息 ID 时，适配器采用稳定源位置 ID；修订原位置应使用明确的新消息 ID，不能以正文哈希删除合法重复。来源时间缺失保持未知，不再生成今天的合成时间。
文档、手工事实和下述原文 API 保留独立契约；`consolidate()` 不会绕过联合任务处理对话消息。

| 方法 | 主要参数 | 返回与语义 |
| --- | --- | --- |
| `add` | content；user_id、session_id、role、event_time；consolidate=False | Episode；保存事件，可选择立即整合 |
| `remember` | content；user_id、session_id、extract=True | dict；保存原文并记录抽取意图，事实仍需显式整合 |
| `consolidate` | episodes=None | 计数字典；None 处理 pending 事件 |
| `consolidate_full` | fact_episodes=None、digest_episodes=None | 计数字典；整合与条件摘要，可分别指定两组事件；reflected 固定为 0 |
| `import_messages` | sessions、user_id；consolidate=None、generate_digests=True、dedupe=True 等 | 计数字典；导入已解析会话 |
| `import_data` | data、format="auto"、user_id、session_id；其余导入选项 | 计数字典；先解析再导入 |
| `import_snapshot` | snapshot、user_id、dedupe=True | dict；导入结构化导出，不等同于打开存储目录 |

结构化快照要求 `format_version=3`。导入先整批校验，遇到无效数据时抛出 `ValueError`。导入保留目标用户的运行设置；字段、来源编号和替代链语义见[结构化快照导入](../architecture/memory.md#结构化快照导入)。

`extract=True` 请求事实抽取，`extract=False` 只保存原文。仅执行请求不生成任务记忆；明确陈述的决定、计划和时间化事实保留其条件与时间。底层 remember 保存原文，随后通过 consolidate 推进抽取。

## 离线检索与上下文

| 方法 | 主要参数 | 返回 |
| --- | --- | --- |
| `search` | query、user_id、as_of=None、top_k=None | SearchResult |
| `as_of` | query、when、user_id、top_k=None | SearchResult；时间查询便捷入口 |
| `lean_context` | query、user_id；as_of、redact_sensitive；n_digests、n_facts、n_chunks、char_budget 等 | 按预算组合的上下文字符串 |
| `context_for` | query、user_id；as_of、top_k、k_chunks 及按需增强选项 | 上下文字符串 |
| `retrieve_episodes` | query、user_id、k=5、pool=None、as_of=None | 不可变 EventEvidence；text 为该事件当前的原文或摘要，不含原文副本 |
| `get_episode` | episode_id、user_id=default | 原文记录字典或 None；仅含身份、来源时间和完整 content |
| `history` | subject、predicate、user_id | 同一槽位的 Fact 列表 |
| `profile` | user_id | 结构化画像缓存与刷新状态 |
| `refresh_profile` | user_id | 创建或复用显式刷新任务，返回 OperationResult |

<a id="profile-evidence"></a>画像条目包含 text、fact_ids 和由服务端计算的 fact_count。读取只返回缓存。首次需要显式调用 refresh_profile，再由离线 flush_messages 或 Postgres flush_processing 推进操作。离线全空间刷新共用一个超时预算，返回的 `jobs`、`counts` 和 `status` 同时反映消息处理与画像任务。画像模型步骤在工作线程执行，等待到期返回 `timed_out` 和当前任务状态，已启动的步骤可继续保存产物，后续调用可续跑。指定 `session_id` 时，返回该会话的处理状态。引用事实发生变化时，对应条目退出展示。模型输入、分批预算与恢复行为见[画像缓存](../architecture/memory.md#画像缓存与显式刷新)。

SearchResult 包含 `query`、`facts`、`scores`、`via` 和 `abstained`。`answer()` 返回已生成答案或最高事实的 object/text；拒答时返回固定提示。`top()` 返回首条 Fact 或 None。它不是对任何问题都调用外部模型的通用聊天接口。

`as_of`、时间字段使用 epoch 秒。查询需要沿用正确的 user_id；recall 和上下文组装不接收 session_id；会话标识用于写入、浏览和处理刷新。Python `graph_data()` 默认包含敏感事实支持的关系，与 HTTP 的安全视图默认值不同。图谱由已通过审核且 `object_kind="entity"` 的事实生成。主体、谓词和对象须为长度符合图标识约束的单行文本。手工新增默认 `unknown`，编辑类型即可补入或移出图谱。名称按大小写无关的规范化文本查找。

## 离线事实、工作状态与策略

| 方法 | 用途 |
| --- | --- |
| `add_fact(subject, predicate, object, user_id=..., text=..., ...)` | 返回用户来源 Fact，`text` 为必填关键字参数 |
| `update_fact(fact_id, ..., categories=None, object_kind=None)` | 修改内容或元数据，返回 Fact 或 None；内容修改必须同时提交 subject、predicate、object、text，元数据可单独修改 |
| `delete_fact(fact_id)` | 删除指定事实，返回 bool |
| `get_focus()`、`set_focus(track=None, mute=None)` | 查询或更新关注点，None 保持不变，[] 清空对应列表 |
| `get_policy()`、`set_policy(**fields)` | 查询或覆盖抽取、摘要与画像策略 |
| `graph_data(user_id=..., as_of=None, include_sensitive=True, q="", live_only=False, limit=None)` | 返回 nodes、edges、has_more；见下述图谱完整性契约 |
| `pending_conflicts(user_id=...)`、`resolve_conflict(conflict_id, keep="newer")`、`dismiss_conflict(conflict_id)` | 查询和裁决待确认冲突 |

图谱的状态、搜索、敏感内容与时间筛选均在 `limit` 前执行；正数 `limit` 限制返回关系数，节点仅包含这些关系的端点。`has_more=true` 表示当前查询条件下仍有未返回的匹配关系；恰好达到上限不表示截断。不限量查询返回全部匹配关系，`has_more=false`。该字段不提供命名空间总数，展示数量仅描述已返回的图。

字段及编辑规则见[分类与对象类型](../architecture/memory.md#分类与对象类型)。

```python
fact = memory.add_fact("user", "prefers", "茶", text="用户偏好茶。", categories=["food"], object_kind="entity")
memory.update_fact(fact.id, categories=["misc"])
memory.update_fact(fact.id, categories=[])  # 清除标签，来源为 unset
```

## 离线扩展与研究入口

摘要和派生层操作还包括 `generate_digests`、`retrieve_digests`；热层管理包括 `evict_cold`、`apply_focus`。这些方法改变派生状态或工作集，调用方负责后续保存及必要验证。

精确签名、类型和行为以 [MemoryEngine](../../mnemotrace/memory_engine.py)、[记录类型](../../mnemotrace/types.py) 和对应[测试](../../tests/)为核对来源。HTTP/MCP 的方法、默认值及能力不与嵌入式 API 一一对应。

## 模型抽取与错误契约

模型的 `complete(prompt, **kwargs)` 继续返回字符串。需要结束状态与用量时使用
`complete_result(prompt, **kwargs) -> Completion`；`Completion` 从 `mnemotrace.llm`
导入，包含 `text`、`finish_reason` 和 `usage`。结束状态或 token 数未提供时为 `None`，
不能将其解释为零用量或已确认正常结束。LiteLLM 适配器和 `CachedLLM` 保留这些信息；
仅实现字符串接口的自定义适配器仍可使用，但只能通过内容校验检测不完整响应。

抽取模型输出 `{"facts": [...], "digests": [...]}`。facts 包含主体、关系、取值、正文、来源及时间/类别/敏感性/生命周期标签；digests 每项包含 source_id 和 text，按请求中的 digest_required 为来源生成摘要。两个数组均为必需。具体字段和严格校验见 [planning.py](../../mnemotrace/consolidate/planning.py)。自定义 extract_system 必须遵循该协议。

离线 `MemoryEngine.consolidate()` 空任务返回零计数；非空任务要求 LLM。整批模型响应全部通过校验后才写入事实和工作条目。模型调用或校验失败不改变身份或完成标记；存储写入失败可能留下部分事实，已完成的窗口范围保留，未完成范围继续待处理；重试跳过完成范围并消解重复事实和工作条目。完整空结果与失败分别处理，截断 JSON 不做部分抢救。

对话历史导入使用消息身份和联合任务；同 ID 的重传不重新生成。文档和可移植快照保留各自导入契约。显式摘要与 include_digests 仍通过同一任务内核，并保留既有摘要。

离线抽取异常包括 LLMNotConfiguredError、ExtractionError、ExtractionResponseError、ExtractionTruncatedError、ExtractionConflictError。未成功范围保留原文，可以重新 consolidate。截断错误 code 为 extraction_truncated；同一命名空间的并发写作废在途抽取时抛 ExtractionConflictError（code 为 extraction_conflict），内核会先基于新快照自动重试一次。窗口输入、来源与范围记录契约见[记忆架构](../architecture/memory.md#窗口抽取与处理范围)。异常携带 code、stored_raw、episode_ids，不包含模型原始响应。Postgres 已接收任务的进度和错误从操作回执读取。

替代裁决仅接受 REPLACES/COEXISTS，冲突检测仅接受 CONFLICT/COEXIST（忽略首尾空白与大小写）；其他响应记录安全警告并不执行对应替代或冲突标记。


## 摘要与显式原文读取

Postgres 核心操作通过 `auxiliary_operation_ids` 关联独立的摘要任务。调用 `flush_processing(include_digests=True)` 可把符合条件的摘要纳入本次处理范围，`retry_failed=True` 重试范围内的失败操作。摘要失败时保留已提交的事实和原文。

摘要成功后，所有 agent 事件检索、来源证据、聚合和上下文只使用摘要。检索文本带事件 ID；通过 get_episode 显式读取原文。
Fact 仍从原文抽取且独立检索；历史事件摘要不追加后续事实。owner 管理浏览、全文搜索及导出保留。

使用 `memory.aggregate_facts("visited", operation="count_distinct")` 统计不同对象，使用 `memory.follow_path("user", ["sister", "works_at"])` 获取路径和支持事实。两者可通过 `FactQuery` 设置有效时间、认知时间、事实状态和脱敏条件。
