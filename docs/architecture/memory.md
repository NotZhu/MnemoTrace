# 记忆生命周期与存储

原始 `Episode` 保留完整事件，`Fact` 提供可查询的结构，图谱和摘要是基于来源的派生层。主要实现位于 [Memory](../../mnemotrace/memory.py)、[types](../../mnemotrace/types.py) 和 [System-2](../../mnemotrace/consolidate/engine.py)。

## 对象与时间

| 对象 | 含义与存放位置 |
| --- | --- |
| `Episode` | 原始事件；`episodes_doc`、`episodes_vec`；`event_time` 为事件时间，`ingested_at` 为摄入时间 |
| `Fact` | 原子 `subject`/`predicate`/`object`；`fact_store`、`cold_store`；保存文本、向量、来源、`salience` 与替代链 |
| `Entity` / `Relation` | 同一 `user_id` 内的实体、昵称与关系投影；关系保存 `fact_id` 及双时间 |
| `WorkingMemory` | 会话/TTL 状态，保存在 `working_mem`，不自动晋升为画像 |
| `Conflict` | 等待显式处理的矛盾候选，保存在 conflicts |
| `Summary` / `Profile` / `Procedure` | 原始会话或事实的派生摘要、画像及流程证据 |

`Fact` 的 `valid_at/invalid_at` 表示现实有效时间，`created_at/expired_at` 表示系统获知/撤回时间。`provenance` 指向来源 `Episode`，`supersedes` 指向被替代的事实。

当前 `Memory.as_of(query, when, ...)` 只把单个 when 传入 `search`；没有独立的现实时间 T 与事务时间 K 参数。具体过滤见 [Fact.is_live](../../mnemotrace/types.py) 和 [temporal.py](../../mnemotrace/retrieve/temporal.py)。

## 写入与整合

```text
add / remember / import
  → 保存 Episode、轻量嵌入、排队
  → 按事件时间整合
  → 抽取 Fact → 冲突判断 → 非破坏性失效 → 图谱投影
  → 分类、敏感位、显著度、衰减及派生摘要
```

`add` 默认先保存证据，可选择立即整合；`remember` 总保留 `Episode`，并按 `scope` 路由。`long` 进入长期整理，`working` 保留会话状态，`auto` 根据瞬态表达判断。归属始终精确匹配调用方传入的原始 `user_id`，默认 `default`；姓名与昵称共指只在同一用户范围内发生。

普通来源默认进入事实整理。`metadata.source="agent_session"` 的工作转录默认保存原文、摘要与结论；仅显式要求 `consolidate` 且配置 LLM 时逐轮抽取。响应通过 `facts_deferred`/`deferred_reason` 说明降级，避免把无 LLM 的传记规则当成真实工作会话理解。

批量导入 CLI 经 `MemoryService.import_()` 共用去重、摘要目录和服务写锁。快照导入保留双时间并重映射 `supersedes`/`provenance`，原文标记已消化；格式错误明确失败。参见 [导入回归](../../tests/test_import_snapshot.py) 与 [Agent 指南](../guides/agents.md)。

### 冲突与图谱

冲突按同槽去重、用户权威、多值保护、偏好反转、内容包含及语义槽处理；不同 `object` 只是候选，不能无条件失效。语义判断需要适合的 embedder；歧义 LLM 升级默认关闭。

明确替代时设置 `old.invalid_at` = `new.valid_at`、`old.expired_at` = `now`、`new.supersedes` = `old.id`，并失效旧图边，两条事实的来源保留。家人、语言和多个项目等多值关系受到保护。

`GraphBuilder` 投影适合关系表达的事实。`procedure/how_to/routine` 及 `decision/finding/lesson/open_question` 不投影成实体边，避免把长句和 session id 变成孤立节点。图谱用于多跳、邻近度、历史关系和展示。

### 会话收尾与派生记忆

`close_session` 排空该会话的待整理消息、补齐摘要、清理工作记忆并保存。配置 LLM 且允许 `outcomes` 时，用一次会话蒸馏调用生成 `decision/finding/lesson/open_question` `Fact`；`subject` 为 session id，`object` 为陈述，`text` 保留依据。`MNEMOTRACE_SESSION_OUTCOMES=0` 强制关闭，调用参数不能绕过。

结论复用 `Fact` 的历史、来源与检索能力，参与敏感分类。精确文本去重不保证同义改写幂等；标准 benchmark 不调用该收尾入口。独立验证见 [outcomes 测试](../../tests/test_outcomes.py)。

事实字段和 `predicate` 使用稳定英文标识，`subject`/`object` 和展示文本跟随来源语言；摘要跟随对话主要语言，离线或失败时回退来源截断。叙事画像使用简体中文，结构化画像从事实派生。

用户 policy 覆盖默认抽取、摘要和画像策略；修改策略清除画像缓存，应用于后续整理，不批量重写旧记录。`evidence.source_count` 只统计不同来源 `Episode` ID；访问强化 `count` 不是来源数或置信度。

## 编辑与擦除

| 操作 | 语义 |
| --- | --- |
| 修改 `sensitive`/`category` 或原样保存正文 | 保留原来源、时间、替代链、向量和图边 |
| 实际修改三元组 | 重建正文、向量与图边，标为用户断言，保留原 `provenance`，重新生效 |
| `statement` 整句编辑 | 仅适用于四类会话结论；属性事实或混用字段在修改前拒绝 |
| 单条 `delete_fact` | 显式擦除指定当前或历史事实 |
| `clear_slot` | 仅 `live` 非手写槽位事实；`expect_count` 防止集合变化；保护历史和用户断言 |

内容编辑修正既有记录，不自动创建历史版本；保留来源不代表修正后的句子逐字来自来源。实现及回归见 [Memory.update_fact](../../mnemotrace/memory.py)、[元数据测试](../../tests/test_fact_metadata_updates.py)。

## 存储与恢复

`VectorStore`、`DocStore`、`GraphStore` 的接口见 [store/base.py](../../mnemotrace/store/base.py)。默认运行时为内存；`durable` 用 [persist.py](../../mnemotrace/store/persist.py) 保存 JSONL 快照，LanceDB 是可选向量后端。

```text
manifest.json
episodes.jsonl   facts.jsonl   entities.jsonl   relations.jsonl
working.jsonl    conflicts.jsonl
.lock           lancedb/（仅可选向量后端）
```

`save` 保存状态，不序列化模型对象；`open` 用传入的模型和配置恢复。manifest 校验 `schema_version`、`embedder_id`、`embedding_dim` 与已提交记录范围；只读取 manifest 声明的已提交前缀；该前缀内部缺失记录或 JSON 格式错误时抛出 `StoreFormatError`。manifest 数量之后的撕裂字节作为未提交尾部丢弃，不解析为有效记忆。读取不使用 `pickle/eval`。

提交通过 manifest 原子替换发布。`snapshot_id` 标识提交，旧 manifest 用原文 SHA-256 识别，下一次正常保存写入新标识；`schema_version` 仍为 1。Windows 允许读者继续持有旧 manifest，并通过 `ReplaceFileW` 发布新版本；服务事务边界见 [服务架构](service.md#并发与提交)。

新快照不保存用户编号关联。旧 `resolver_parent` 只允许缺失、空字典或字符串到自身的映射；真实关联和非法结构在修改 store 前拒绝。姓名/昵称的 `identity`/`aliases` 状态继续保存。pickle 转换与不同编号关联已退役。

嵌入维度不符抛出 `DimensionMismatchError`；即使维度相同，模型 id 不同也抛出 `EmbedderMismatchError`，避免返回错误向量空间的近邻。改变模型先备份并显式重嵌入，参见[配置参考](../reference/configuration.md#存储与嵌入)。冷热分页、衰减和强化在现有 `Memory` 生命周期中执行，不代表已经验证任意规模的能力。

未完成的存储校验、画像和结论问题见[问题台账](../issues.md)。
