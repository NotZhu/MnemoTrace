# Python API

面向嵌入式应用的公共入口为 `from mnemotrace import Memory, Config`；还导出 Episode、Fact、Entity 和 Relation。核心不需要第三方运行时依赖。入门步骤见[快速开始](../quickstart.md)。

## 构造与运行配置

`Memory(config=None, embedder=None, llm=None, reranker=None, vector_store_factory=..., graph_store_factory=...)`

默认 Config、HashingEmbedder 和内存文档/向量/图存储。传入 LLM 才能抽取自然语言事实；可选实现通过各自 extras 安装。参数字段和默认值见[配置参考](configuration.md)及 [Config](../../mnemotrace/config.py)。

直接使用 Memory 不读取服务端的凭据映射，也不自动提供鉴权、服务级锁或逐操作保存。并发与持久化由应用管理，或改用 [MemoryService](../../mnemotrace/service.py)/HTTP。

## 写入与导入

| 方法 | 主要参数 | 返回与语义 |
| --- | --- | --- |
| `add` | content；user_id、session_id、role、event_time；consolidate=False | Episode；保存事件，可选择立即整合 |
| `remember` | content；user_id、session_id、scope="auto" | dict；路由 long/working，长期内容仍需显式整合 |
| `consolidate` | episodes=None | 计数字典；None 处理 pending 事件 |
| `consolidate_full` | fact_episodes=None、summary_episodes=None | 计数字典；整合与条件摘要，可分别指定两组事件；reflected 固定为 0 |
| `import_messages` | sessions、user_id；consolidate=None、summarize=True、dedupe=True 等 | 计数字典；导入已解析会话 |
| `import_data` | data、format="auto"、user_id、session_id；其余导入选项 | 计数字典；先解析再导入 |
| `import_snapshot` | snapshot、user_id、dedupe=True | dict；导入结构化导出，不等同于打开存储目录 |

结构化快照不使用格式版本字段，按当前必要字段整批校验，错误抛出 `ValueError`，不部分导入。导入保留目标用户的运行设置；字段、来源编号和替代链语义见[结构化快照导入](../architecture/memory.md#结构化快照导入)。

`scope="auto"` 在整合时由同一次模型调用识别长期事实与临时状态；`long` 仅请求长期事实，`working` 显式写入工作记忆。时间词不决定生命周期。底层 remember 仅保存原文和请求的 scope，auto/long 仍需 consolidate。Python 记忆实例中的写入不会自动保存到磁盘。

## 检索与上下文

| 方法 | 主要参数 | 返回 |
| --- | --- | --- |
| `search` | query、user_id、as_of=None、top_k=None | SearchResult |
| `as_of` | query、when、user_id、top_k=None | SearchResult；时间查询便捷入口 |
| `lean_context` | query、user_id；as_of、session_id、redact_sensitive；n_summaries、n_facts、n_chunks、char_budget 等 | 按预算组合的上下文字符串 |
| `context_for` | query、user_id；as_of、top_k、k_chunks 及按需增强选项 | 上下文字符串 |
| `retrieve_episodes` | query、user_id、k=5、pool=None、as_of=None | 不可变 EventEvidence；text 为该事件当前的原文或摘要，不含原文副本 |
| `get_episode` | episode_id、user_id=default | 原文记录字典或 None；仅含身份、来源时间和完整 content |
| `history` | subject、predicate、user_id | 同一槽位的 Fact 列表 |
| `profile` | user_id | 属性字典 |
| `structured_profile` | user_id | 结构化画像字典 |
| `build_persona` | user_id | 派生画像文本 |

<a id="profile-evidence"></a>结构化画像条目的 `evidence` 包含 `kind`、`source_count` 和 `reinforcement_count`。`source_count` 是事实 provenance 中不同来源 Episode ID 的数量；手动添加且没有来源、或仅从身份消解补全的姓名，来源数为 0。`reinforcement_count` 是事实的 `access_count`，记录检索使用时的强化次数；手动设定和多来源事实同样返回该次数，身份消解补全的姓名为 0。`kind` 优先用 `user` 表示手动设定；非手动事实在来源不足两条且曾被检索强化时为 `reinforced`，其余为 `mentions`。三者分别表达状态、来源数和强化次数，读取画像不会增加强化次数。

偏好条目提供 topic/topic_source，按已存主题分组；unknown 均进入“其他”，明确禁忌关系是独立特例。条目的 category 为画像分组标签，完整规则见[主题与来源](../architecture/memory.md#主题与来源)。Python 服务入口 `MemoryService.memories(..., topic=None)` 按已存主题精确筛选；unknown 包含人工及未赋值的未知，非法枚举抛出 ValueError。与其他事实筛选取交集，在分页前执行，不筛选原始 episode。

SearchResult 包含 `query`、`facts`、`scores`、`via` 和 `abstained`。`answer()` 返回已生成答案或最高事实的 object/text；拒答时返回固定提示。`top()` 返回首条 Fact 或 None。它不是对任何问题都调用外部模型的通用聊天接口。

`as_of`、时间字段使用 epoch 秒。查询需要沿用正确的 user_id；携带 session_id 才能在相应上下文中包含该 session 的工作记忆。Python `graph_data()` 默认包含敏感事实支持的关系，与 HTTP 的安全视图默认值不同。

## 事实、工作状态与策略

| 方法 | 用途 |
| --- | --- |
| `add_fact(subject, predicate, object, user_id=..., valid_at=None, sensitive=None, category=None, topic=None)` | 返回用户来源 Fact |
| `update_fact(fact_id, ..., topic=None, reset_topic=False)` | 修改内容或元数据，返回 Fact 或 None；statement 不与三元组字段混用 |
| `delete_fact(fact_id)` | 删除指定事实，返回 bool |
| `remember_working(content, user_id=..., session_id=..., kind="state", ttl_seconds=None)` | 返回 WorkingMemory |
| `working_memory(user_id=..., session_id=None, as_of=None, kind=None)` | 活跃工作条目 |
| `clear_session(user_id=..., session_id=...)` | 清理该 session 工作记忆，返回数量；不是服务层 close_session |
| `consume_working(wm_id)`、`sweep_working(as_of=None)` | 标记消费或清理过期/已消费工作条目 |
| `get_focus()`、`set_focus(track=None, mute=None)` | 查询或更新关注点，None 保持不变，[] 清空对应列表 |
| `get_policy()`、`set_policy(**fields)` | 查询或覆盖抽取、摘要与画像策略 |
| `graph_data(user_id=..., as_of=None, include_sensitive=True, q="", live_only=False, limit=None)` | 返回 nodes、edges、has_more；见下述图谱完整性契约 |
| `pending_conflicts(user_id=...)`、`resolve_conflict(conflict_id, keep="newer")`、`dismiss_conflict(conflict_id)` | 查询和裁决待确认冲突 |

图谱的状态、搜索、敏感内容与时间筛选均在 `limit` 前执行；正数 `limit` 限制返回关系数，节点仅包含这些关系的端点。`has_more=true` 表示当前查询条件下仍有未返回的匹配关系；恰好达到上限不表示截断。不限量查询返回全部匹配关系，`has_more=false`。该字段不提供命名空间总数，展示数量仅描述已返回的图。

主题可使用导出的 `Topic`、`TopicSource` 类型。`topic=None` 表示未指定，`topic="unknown"` 是人工选择未知。`reset_topic=True` 按当前内容重新执行离线规则，不调用模型，结果为明确主题/rule 或 unknown/unset，不能与明确 topic 同传。仅修改主题不改变事实来源或历史；新增未指定主题、内容变化且未指定新主题时使用规则。完整契约见[主题与来源](../architecture/memory.md#主题与来源)。

```python
fact = memory.add_fact("user", "prefers", "茶", topic="food")
memory.update_fact(fact.id, topic="unknown")  # unknown/user
memory.update_fact(fact.id, reset_topic=True)  # 按当前内容重新判断，来源为 rule 或 unset
```

## 保存与模型切换

```python
from mnemotrace import Memory

memory = Memory.open("/path/to/owned-memory-store")
memory.add("I prefer Python.", user_id="user123")
memory.consolidate()
memory.save()
```

`save(path=None)` 使用指定目录或上次打开/保存的路径；没有路径时抛出 ValueError。`open(path, allow_mismatch=False, **kwargs)` 是类方法，支持与构造函数一致的运行组件参数。

异常类型从 `mnemotrace.store.persist` 导入：PersistenceError、StoreFormatError、DimensionMismatchError、EmbedderMismatchError。当前恢复条件和重嵌入流程见[持久化契约](../architecture/memory.md)。

当前格式缺少必要信息、类型非法或缺少提交编号的历史存档会被明确拒绝，不自动升级。自定义 embedder 必须提供正整数 `dim`；`allow_mismatch` 只放宽模型匹配，不跳过格式校验。

## 扩展与研究入口

服务端 `MemoryService.remember(user, content, session_id="default", scope="auto", processing="immediate")`
可显式选择 buffered，需开启 `MNEMOTRACE_BUFFERED_EXTRACTION`。这不改变底层 `Memory.remember()` 仅保存原文与路由的职责。
`processing_status(user, session_id=None)` 查看任务；`flush(user, session_id=None, timeout=30, retry_failed=False, include_summaries=False)`
等待本次捕获范围并返回 completed、failed 或 timed_out，只有 completed 对应 ok=true。
取消、失败和超时不会删除原始事件。模型或存储失败不能通过 queued 响应解释为抽取成功。

摘要和派生层操作还包括 `summarize_episodes`、`retrieve_summaries`、`procedural`；热层管理包括 `evict_cold`、`apply_focus`。这些方法改变派生状态或工作集，调用方负责后续保存及必要验证。

精确签名、类型和行为以 [Memory](../../mnemotrace/memory.py)、[记录类型](../../mnemotrace/types.py) 和对应[测试](../../tests/)为核对来源。HTTP/MCP 的方法、默认值及能力不与嵌入式 API 一一对应。

## 模型抽取与错误契约

模型的 `complete(prompt, **kwargs)` 继续返回字符串。需要结束状态与用量时使用
`complete_result(prompt, **kwargs) -> Completion`；`Completion` 从 `mnemotrace.llm`
导入，包含 `text`、`finish_reason` 和 `usage`。结束状态或 token 数未提供时为 `None`，
不能将其解释为零用量或已确认正常结束。LiteLLM 适配器和 `CachedLLM` 保留这些信息；
仅实现字符串接口的自定义适配器仍可使用，但只能通过内容校验检测不完整响应。

抽取模型输出 `{"facts": [...], "working": [...]}`；facts 条目包含非空字符串 subject、predicate、object，可附 text 和 topic；working 条目包含非空 content 和 kind（state/intent/task），不接受模型设置 TTL。两个数组均为必需；混合内容分别生成两类记录，无记忆价值时两个数组为空。自定义 extract_system 必须遵循该协议，旧数组响应不兼容。会话结论仍使用对象数组协议。

`consolidate()` 空任务返回零计数；非空任务要求 LLM。整批模型响应全部通过校验后才写入事实和工作条目。模型调用或校验失败不改变身份或完成标记；存储写入失败可能留下部分事实，已完成的窗口范围保留，未完成范围继续待处理；重试跳过完成范围并消解重复事实和工作条目。完整空结果与失败分别处理，截断 JSON 不做部分抢救。

导入重试先去重，再检查实际抽取需求：全部内容已完成且没有相关 pending 时，无需 LLM 即可返回 skipped，即使 consolidate=True。存在待抽取的新内容或相关 pending 时，在本次新输入写入前检查模型。已存在内容不再创建 episode。summarize=True 会补齐本次涉及会话中满足长度条件且缺失的摘要，包括抽取重试和部分新增的情况；已有摘要不重复生成，返回 summaries 为本次实际补齐数量。

公开异常为 LLMNotConfiguredError、ExtractionError、ExtractionResponseError、ExtractionTruncatedError；未成功范围保留原文，可以重新 consolidate。截断错误 code 为 extraction_truncated。窗口输入、来源与范围记录契约见[记忆架构](../architecture/memory.md#窗口抽取与处理范围)。异常携带 code、stored_raw、episode_ids，不包含模型原始响应。原文保存不等同于事实抽取完成。模型输出的 working.kind 限定为 state/intent/task，显式工作记忆接口仍可使用其他 kind。会话结论 kind 在校验前忽略大小写和首尾空白，未知类型仍报响应无效。

替代裁决仅接受 REPLACES/COEXISTS，冲突检测仅接受 CONFLICT/COEXIST（忽略首尾空白与大小写）；其他响应记录安全警告并不执行对应替代或冲突标记。


服务层 `MemoryService.close_session()` 遇到缓冲刷新失败或超时会抛出 `ProcessingIncompleteError`（code=`processing_incomplete`），不会报告关闭成功或执行后续清理。先查询处理状态，必要时显式重试失败任务，再关闭会话。缓冲关闭配置错误另用 `BufferedProcessingDisabledError` 表达。

## 数据库资源生命周期

`Memory.close()` 幂等释放 Memory 内部创建的数据库客户端，不隐式保存。`with Memory.open(...) as memory:` 可保证退出时释放；需要持久化改动时在退出前调用 `save()`。直接注入 `QdrantVectorStore` 的客户端由调用方管理。可选后端的选择、路径和远程配置见[配置参考](configuration.md#可选-qdrant)。


### 摘要与显式原文读取

`remember` 在完成原文保存及抽取后才执行条件摘要；摘要失败仍返回 ok=true，并通过 summarization.status/error_code 报告。
buffered 在整个事件抽取完成后自动注册摘要任务，读取不触发模型。processing_status 返回独立的 summarization 任务区，原 counts 仍只统计抽取。
默认 flush 只等待抽取；include_summaries=True 等待调用开始捕获范围内的摘要。未启用 worker 时不隐式启动后台处理，未完成任务返回 failed，可通过 close_session(summarize=True) 显式重试。
close_session 的摘要未完成时返回 processing_incomplete，保留已完成阶段及工作记忆；summarize=False 不取消既有后台任务。

摘要成功后，所有 agent 事件检索、来源证据、聚合和上下文只使用摘要。检索文本带事件 ID；通过 get_episode 显式读取原文。
Fact 仍从原文抽取且独立检索；历史事件摘要不追加后续事实。owner 管理浏览、全文搜索及导出保留。
