# 记忆生命周期与存储

本页解释记忆的时间、来源、持久化与擦除边界。操作步骤见[使用指南](../guides/usage.md)，公共方法见 [Python API](../reference/python-api.md)。

## 数据模型

| 类型 | 用途与关键字段 |
| --- | --- |
| Episode | 原始内容，关联 user、session、speaker、event_time 与 ingested_at；摘要和整合标记是派生状态 |
| Fact | subject/predicate/object 断言，保留 provenance、source、有效时间、事务时间和 supersedes |
| Entity / Relation | 实体及由 fact 支撑的关系，用于图检索与可视化 |
| WorkingMemory | 带 session、TTL 和 consumed 状态的短时内容 |
| Conflict | 对疑似冲突的待裁决记录，关联新旧事实及处理状态 |

画像、摘要和图谱都是派生视图，不能代替原始来源。Fact 的 `provenance` 指向 Episode id，`source="user"` 标识用户明确添加或内容编辑的事实。

## 时间与知识更新

- Episode 的 `event_time` 表示事件发生时间，`ingested_at` 表示进入系统的时间。
- Fact 的 `valid_at/invalid_at` 记录有效区间，`created_at/expired_at` 记录认知侧时间。
- `supersedes` 连接新事实与被替代事实。更新与自动冲突处理通常采用失效化，保留被替代事实。
- `as_of` 接受 epoch 秒，过滤当前可用记录。它不是任意历史磁盘快照，也无法恢复已经物理删除的数据；具体有效性判定以 [Fact.is_live](../../mnemotrace/types.py) 和对应读取实现为准。

相同槽位中的不同值只是候选冲突。明确更新、补充和歧义冲突需要区分；可选 LLM 检测出的待确认冲突由用户裁决。用户撰写事实受到自动整合的保护，但用户主动编辑或删除仍可改变它。

## 工作记忆与会话收尾

`Memory.remember(scope="working")` 保存带日期的 Episode，并把内容放入 working 层，避免作为持久画像事实提取；直接调用 `remember_working()` 则只添加 working 条目。

working 的“临时”指可见性与清理生命周期。当前 JSONL 快照包含 `working.jsonl`，活跃条目可能随服务重启恢复；它不等于只在 RAM 中存在。`working_set` 是查询时的工作集合，与上述条目不同。

过期、消费或明确清空后，working 条目不再作为活跃状态返回。`close_session` 可整合未完成事件、补摘要、按模型与配置提取结论，再清理指定 session 的 working。原始 Episode 保留。没有配置 LLM 时，不应期待 session outcomes 生成语义结论。

## 保存与提交边界

`Memory.save(path)` 将纯记录保存到 JSONL 集合，并提交 `manifest.json`。集合包括 episodes、facts、entities、relations、working 和 conflicts；manifest 保存计数、嵌入身份、提交标识以及 focus、policy、身份和导入去重等状态。

加载以 **manifest 声明的已提交前缀** 为边界：

- 前缀内部缺失记录或 JSON 格式错误时抛出 `StoreFormatError`。
- manifest 数量之后的撕裂字节作为未提交尾部丢弃；这里指加载时不接纳尾部，不表示自动改写磁盘文件。
- 路径存在且不是目录时拒绝加载；manifest 不存在时，当前实现按空库处理。
- 当前写入 schema 为 1，较新的不支持 schema 会被拒绝。

保存先刷新并替换集合文件，再替换 manifest。文件锁和提交标识提供并发协调，但多文件快照不等于完整的跨文件事务回滚，也不承诺崩溃发生在任意阶段都能自动恢复旧代次。备份应在暂停写入后复制完整数据目录，见[部署说明](../../deploy/README.md#备份与恢复)。

当前加载器仍存在缺失字段默认补齐、部分元数据兼容与旧事实回填路径。相关收敛事项见 [记录字段校验 #15](https://github.com/NotZhu/MnemoTrace/issues/15)、[元数据校验 #16](https://github.com/NotZhu/MnemoTrace/issues/16)、[状态恢复校验 #17](https://github.com/NotZhu/MnemoTrace/issues/17) 和 [加载回填退出 #18](https://github.com/NotZhu/MnemoTrace/issues/18)；这些目标尚不能作为现有严格校验能力对外描述。

## 嵌入模型一致性

- 嵌入维度不符抛出 `DimensionMismatchError`。
- 即使维度相同，模型 id 不同也抛出 `EmbedderMismatchError`。
- 相同维度不代表向量可比较；跳过身份校验后直接检索会得到错误向量空间的近邻。

更换模型时先备份，使用 `Memory.open(path, allow_mismatch=True, embedder=...)`，立即执行 `reembed()`，再 `save()`。该例外用于显式重嵌入，不是忽略损坏的通用选项。服务端对应开关 `MNEMOTRACE_REEMBED_ON_MISMATCH=1` 也可能触发模型调用、下载或重算，完成后应恢复通常的守卫。

运行时 embedder、LLM 和可调 Config 不作为完整 Python 对象序列化。重开时重新提供运行配置，不能假定快照会恢复所有算法选项。

## 隔离与擦除

直接 Memory 实例通过 user_id 分区，调用方仍能访问实例本身；面向不可信客户端的隔离由 HTTP 鉴权和服务命名空间执行。

单事实删除、槽位清理和命名空间清空的范围不同。删除 Fact 不保证抹掉来源 Episode；静音、失效和敏感标记也不等于物理擦除。槽位批量清理仅接受 live、非手写、处于已确认范围内且不破坏替代链的候选，并校对预期数量。完整擦除还需考虑外部导出、备份和客户端副本。

实现入口：[types.py](../../mnemotrace/types.py)、[persist.py](../../mnemotrace/store/persist.py)、[MemoryService](../../mnemotrace/service.py)。
