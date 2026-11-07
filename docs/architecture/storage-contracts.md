# Postgres 改造契约与行为对照

本文维护 Postgres 产品路径的数据契约、调用方对照和验证输入。
[契约代码](../../mnemotrace/store/contracts.py)约束授权空间、记录变更、读取版本、任务产物和提交回执。
产品接入端使用授权空间内的 `PostgresMemory`，共享三阶段任务流水线和数据库混合检索；
数据库连接、角色和运行方式见[配置参考](../reference/configuration.md#postgres-数据库基础)。

## 身份与公共接口

user_id 表示账号及数据所有者，space_id 为系统生成的稳定空间 ID，agent_id 仅记录输入来源，
session_id 在空间内定位线程，operation_id 定位处理执行。人物 subject 与账号 ID 无关。
首版一个空间只有一个所有者；同用户所有有效 key 权限相同，不做跨用户 ACL、agent 注册或额外 run ID。

HTTP/远程 MCP 根据 key 摘要经 api_keys → users 得到身份，再验证空间归属及状态。
请求、metadata 和 agent 名称不能改写身份。SpaceContext 的结构校验不等于授权，只有受信任入口才能建立它。
Python/CLI/本地 MCP 属于受信任数据库访问，显式携带身份和空间，仍检查归属。
未知/已删除空间报错，不自动创建；创建、改名、列表是用户级入口，空空间也能列出。

| 入口 | 目标参数与作用域 | 返回与错误 |
| --- | --- | --- |
| HTTP 单空间 | 请求体/查询参数 space_id 必填；不接受所有者 user_id；摄入可带 agent_id | 保留成功对象/detail 错误，增加 operation 或 operations |
| HTTP 多空间 recall | 非空、无重复的 space_ids；全部先鉴权，不使用旧 spaces/home 回退 | 任一未授权整次失败；结果/证据带 space_id |
| 远程 MCP | 配置必填 space_id；可显式选择同用户空间；身份只来自 key | 转发 HTTP 进度，保留工具错误形式 |
| 本地 MCP | 启动配置 user_id、space_id、数据库连接，替换 namespace | 共用执行器，工具参数不充当认证 |
| Memory / MemoryService | 绑定可信 SpaceContext；服务的空间操作显式携带上下文 | Python 异常带操作 ID 和实际持久化进度 |
| TypeScript SDK | 单次选项必填 space_id，多空间使用 space_ids | 保留异常封装，类型同步替换 |
| CLI / 导入 / watcher | 直连或 HTTP 各按既有认证方式；必选空间 | 接受、查询、重试使用同一操作 ID；失败不返回空结果 |
| 前端 | 列出/创建并选择空间后才能操作 | 切换 key/空间/会话取消旧请求并清理展示和查询缓存 |

agent_id 可为 null；非空为 1—128 个 ASCII 字符 `[A-Za-z0-9_.:@/-]`，空串不表示空值。
message_id/session_id 为 1—512 字符非空标识，原样保存，不 trim 或折叠大小写。
消息仅允许 user/assistant，正文至多 1000000 codepoint 且不能全空白，event_time 为有限数值或 null。
role、正文、规范化为浮点的事件时间、agent_id、mode、scope 共同形成消息指纹；摄入时间不参与。
唯一键为 `(user_id,space_id,session_id,message_id)`，space_id 本身全局唯一。
同键同指纹复用来源和任务，同键异指纹返回 message_conflict；原文修正通过新消息提交。

messages 请求示例：

```json
{"space_id":"space-A","session_id":"thread-A","agent_id":"writer-A","mode":"live","scope":"auto","processing":"buffered","messages":[{"message_id":"m1","role":"user","content":"Synthetic input","event_time":1700000000}]}
```

操作对象采用 OperationResult.to_dict 的字段。每个摄入响应包含相关 operation 或 operations，
messages 项携带 episode_id、operation_ids，不假定一批消息永远只对应一个抽取窗口。
首次接受生成稳定操作 ID，窗口合批通过来源关联表达，执行时不更换对外操作身份。
查询为 `GET /v1/operations/{operation_id}?space_id=...`，重试为
`POST /v1/operations/{operation_id}/retry`，体内必填 space_id，不重新提交原文。
processing/flush 继续提供聚合和等待能力，内部引用这些操作 ID。

| 场景 | 可观察结果 |
| --- | --- |
| buffered 接受 | HTTP 202；state=pending、phase=queued、stored_raw=true、extraction_saved=false、core_committed=false |
| 产物已保存 | 查询 200；phase=embed，stored_raw/extraction_saved=true、core_committed=false |
| immediate 完成 | 200；state=completed、phase=done，三个持久化标志均 true；辅助操作独立 |
| 等待超时 | 沿用 processing_incomplete / timed_out 形式，带 operation_ids 和实际进度；任务不取消 |
| embedding 失败 | failed/retry_wait、phase=embed、extraction_saved=true、core_committed=false、error_code 可见 |
| 核心完成、摘要失败 | 主操作仍 completed；auxiliary_operation_ids 指向失败 summary，不要求重发原文 |

原文/人工事实不强制有抽取任务；自动抽取缺少 LLM 在本次输入写入前拒绝。
Python 底层保存原文、consolidate 检查模型的分工保留。
阶段六以「整理会话」替换 `close_session`，复用 `flush_processing` 推进并等待本批核心处理。
摘要沿用现有流程并单独报告进度。移除自动工作记忆清理、专门的会话结论生成逻辑及旧入口。
现有各入口的辅助等待差异由接口回归锁定。
错误映射：无效 key=401；未知/他人空间统一 space_not_found=404；参数错误=422；消息/业务版本冲突=409；
数据库不可用=503；损坏=500；模型错误沿用 ExtractionError 映射。不输出凭据、连接串、原文或内部路径。
已保存请求的错误必须带操作标识与 stored_raw，不能让调用方猜测是否需要重发。

### 接入示例

用管理 CLI 创建账号与 key 后，使用 `POST /v1/spaces` 和请求体 `{"name":"研究"}` 创建空间。
`GET /v1/spaces` 返回 `spaces` 和 `next_after_id`，包含空空间。
`PATCH /v1/spaces/{space_id}` 用同样的请求体改名。
`DELETE /v1/spaces/{space_id}?confirm=true` 删除空间。
`POST /v1/forget` 用 `{"space_id":"…","confirm":true}` 清空空间并保留其 ID 与配置。

直连 Python 从 `mnemotrace` 导入 `PostgresMemory`。直连 CLI、导入和 MCP stdio 使用
`MNEMOTRACE_DATABASE_URL`，并显式传入 `--user-id`、`--space-id`。
MCP stdio 代理使用 `--api-url`、`--api-key-file`、`--space-id`。
Streamable HTTP MCP 每次调用携带 Bearer 凭据和 `X-MnemoTrace-Space-Id`。
OpenAI 兼容请求通过 `memory.space_id` 选择空间，受理的操作 ID 通过 `mnemotrace.operation_ids` 返回，
流式响应在结束帧返回同一字段。

```text
mnemotrace --user-id <USER_ID> --space-id <SPACE_ID> operation <OPERATION_ID>
mnemotrace --user-id <USER_ID> --space-id <SPACE_ID> retry <OPERATION_ID>
mnemotrace-import --api-url http://localhost:8000 --key-file <KEY_FILE> --space-id <SPACE_ID> --file history.json
```

TypeScript SDK 的单空间方法接受 `{ spaceId }`，多空间 recall 接受 `{ spaceIds }`。
buffered 请求由独立 Postgres worker 推进，也可以通过 `/v1/processing/flush` 等待指定会话的核心处理。

## 工作集、版本与变更集合

工作集绑定 SpaceContext、ReadSnapshot；禁止任意 where 回调、普通请求完整 values() 或完整图恢复。
ReadView 在同一连接的 REPEATABLE READ 只读事务内读取版本与多表数据，返回脱离连接的记录。
分页默认 100、最多 1000、稳定 ID 游标；必要主体/槽位历史走专用 history 读取，不受候选 top_k 截断。
StoredRecord 的 JSON 是不可变的单条 domain 交接表示，不是任意 SQL 列补丁或空间快照。
复用现有六种 domain 记录的字段校验不代表旧文件格式兼容；阶段一不新增旧格式读写入口。
agent_id、归属在 envelope 明确给出。内联向量不作为权威列；存在内联向量时必须有版本/角色/坐标一致的 EmbeddingWrite。
CandidateBatch 返回读取快照、候选信号和覆盖限制，空结果成功与索引未就绪/降级分别表达。
SpaceDAO 的 claim/renew/save_product/product 是空间内任务路径；跨空间发现与账号管理不进入普通 DAO。

| 版本/标记 | 推进条件 |
| --- | --- |
| core_revision | 空间正式业务内容变化时 +1；任务状态、心跳、错误不推进 |
| record_version | 单条业务记录变化时 +1，插入为 1；摘要/显著度等元数据也推进 |
| content_version | 用于抽取/嵌入的正文变化时推进；原文不可变，摘要更新不改变原文版本 |
| summary_version | 来源摘要自己的文本版本；summary 向量绑定此版本，不绑定原文 content_version |
| space_generation | 清空/删除等空间级作废推进；普通业务更新使用 core_revision |
| session_generation | 只清理/作废目标会话时推进，其他会话不受影响 |
| lease token | 每次领取生成新值；续租/保存/提交校验 token 和数据库时间下的有效期 |

FactQuery 两条时间轴独立。valid_at=null 取快照时间；known_at=null 沿用有效时间检查 expired_at，
不额外加 created_at 截止，以保持当前 Fact.is_live(as_of) 对晚摄入历史来源的可观察结果。
显式 known_at 才要求 created_at ≤ known_at、expired_at > known_at。
CURRENT 要求有效时间和适用的认知时间均未结束；HISTORY 返回已失效/撤回版本，ALL 合并二者。
三者均排除所选视图之后才生效的事实，显式 known_at 的创建时间截止也适用于 HISTORY/ALL；未知事件时间不补造。
旧 as_of 的适配必须通过现有历史回归，不能在存储切换中暗改为另一套时间模型。

ChangeSet 显式包含记录写入、事实失效、擦除、来源关联、向量、实体合并、空间设置、后续任务及回执。
一个记录只有一次主修改；失效仅用于 Fact，擦除遵守用户范围。settings 替换整个小型空间配置，主体名不提供账号身份。
每次完整事实写入携带的 source_links 必须等于其 provenance；新向量版本必须匹配对应文本，不能为本次擦除目标新增派生数据。
源、supersedes、冲突双方、关系端点和任务引用的实际存在性/归属由 DAO 与复合外键共同验证。
Python 结构校验不能证明数据库记录存在。显著度强化、访问计数、衰减、focus/policy 都需显式业务提交，
不能把可持久化副作用留在只读事务。失败可识别，不吞掉后返回成功。
提交检查并推进预期 core_revision，原子应用变更、必要图/词法更新、已生成向量、后续任务和回执。
任务完成时若没有正式业务变更，完成回执记录当前 core_revision。
冲突后丢弃旧归并结果，重读必要历史并计算，不重载整个空间，不盲目重放非幂等操作。
operation_id 回执在空间内唯一；连接中断先查询回执，避免重复应用。

## Schema 与数据库基础

[迁移代码](../../mnemotrace/store/postgres/migrate.py)及其 [SQL 资源](../../mnemotrace/store/postgres/sql/)
是数据库结构、约束、索引与角色策略的权威定义，不在本文复制 DDL。
数据库基础提供账号/key/空间、连接池和授权事务。显式 Python 入口使用 Postgres 业务 DAO 和恢复执行器。
显式 Python Postgres 入口使用下述数据库混合检索链路。
HTTP/MCP 等产品入口通过 PostgreSQL 版 MemoryService 使用同一条数据库查询链路。
没有新旧存储双写或自动回退。

### 显式 Python 业务入口

`PostgresMemory(database, context)` 接收已打开的运行角色连接池与受信任的 `SpaceContext`。
调用方管理连接池，DAO 每次在事务中验证账号和空间。读取返回脱离连接的领域记录，
`memory.dao.read_snapshot()` 提供单次事务内的多表查询及包含版本和 agent 来源的记录封套。

```python
from mnemotrace.postgres_memory import PostgresMemory
from mnemotrace.store.contracts import MessageInput
from mnemotrace.store.postgres import Database, DatabaseConfig, Spaces

with Database(DatabaseConfig.from_env()) as database:
    context = Spaces(database).authorize(user_id, space_id)
    memory = PostgresMemory(database, context)
    receipt = memory.store_messages("thread-A", (
        MessageInput("m1", "user", "Synthetic input", scope="raw", agent_id="writer-A"),
    ))
    episode = memory.get_episode(receipt.source_ids[0])
    memory.add_fact("Synthetic person", "likes", "tea")
    memory.remember_working("Synthetic task", "thread-A")
    result = memory.search("What does Synthetic person like?")
    context_result = memory.lean_context("What does Synthetic person like?", session_id="thread-A")
    print(result.answer(), context_result.context, context_result.limitations)
```

`store_messages` 只接受 `scope="raw"`，返回按输入顺序排列的来源 ID，重复项复用原来源。
直接提交回执与数据同事务提交；连接中断且无法查证时抛出带操作 ID 的
`CommitOutcomeUnknown`，可在同空间用 `memory.dao.receipt(operation_id)` 查证。
`erase` 和 `ChangeSet.erasures` 接受 `FACT` 和 `WORKING`。
批次包含其他类型时，在开始写事务前整批拒绝。单条来源、实体、关系及冲突记录没有显式擦除入口。

`clear_working(session_id)` 清除指定会话的工作记忆并作废该会话的任务，保留原文和长期事实。
空间清空、空间删除和账号删除各自按授权范围清理数据与任务。
默认 `export_records()` 仅流式导出非敏感事实。显式指定 `include_sensitive=True` 可读取全部领域记录，备份恢复另见阶段八。
裁决冲突时按同一快照检查双方是否仍有效。事实文本超出图实体名或谓词的长度限制时，
事实照常保存，图中省略该边。

自动处理通过 `PostgresMemory(database, context, llm=llm)` 启用。使用 `remember_messages(session_id, messages)`
受理对话，或使用 `remember(content, session_id, source_kind="raw")` 受理原文。已解码文档可传
`source_kind="document"`。返回值包含 `source_ids`、`operation_ids` 和 `OperationResult` 对象。
`buffered` 在 T1 后返回，`immediate` 等待核心提交，超时仍返回已接受的操作标识。
`operation(operation_id)` 查询当前进度，`retry_operation(operation_id)` 重新排队，
`flush_processing(session_id, include_summaries=True)` 同时等待本批摘要。长原文和文档的分段进度
保存在来源覆盖记录中，所有连续分段提交后才标记核心完成。公开操作 ID 的最终回执汇总各段仍保留的
事实、工作记忆和来源。每个摘要任务处理一个来源，摘要片段仅在生成配置指纹匹配时复用。

#### 数据库检索与补建

`search` 返回带排序事实和覆盖限制的 `SearchResult`。`retrieve_episodes` 返回带覆盖限制的来源证据，
优先呈现已有摘要，完整原文由 `get_episode` 读取。`context_for` 和 `lean_context` 返回 `ContextResult`，
其中 `context` 是组装文本，`coverage_by_space` 给出各空间的限制。`recall_multi` 先校验所有空间的归属，
再分别读取并标记空间。`as_of` 同时设置有效时间和已知时间。`search(facts=FactQuery(...))` 可独立指定两条时间轴。
历史检索仅在当前摘要版本的写入时间不晚于已知时间时使用该摘要。摘要生成时间未知或当前版本尚未写入时，
使用符合时间条件的原文。被替换的摘要版本不保留。
`FactQuery.limit` 在候选阶段限制各信号的返回数，融合后的结果数由入口参数决定。
向量、词法、新近度与显著度共用筛选后的事实范围，再应用各自的候选上限。
搜索结果基于读取快照。若并发写入使访问强化的前置版本失效，本次跳过强化，仍返回快照结果。

向量查询默认精确，并根据本次空间和过滤值规划。词法使用相同作用域的 BM25 语料统计。
`complete` 表示没有已知的索引或候选覆盖限制。
`limitations` 区分 `lexical_backfill_pending`、`fact_vectors_incomplete`、`graph_backfill_pending`、
`graph_candidate_budget` 和原文向量的 `source_vectors_pending_backfill`、`source_vectors_processing`、
`source_vectors_failed`。数据库连接与查询错误继续通过数据库异常传递。查找实体可用 `graph_entities(name)`，
邻接和事实边可用 `graph_neighbors`、`graph_relations`，两者接受 `FactQuery` 时间条件。
检索内部优先解析实体的确切名称，别名仅在合并后指向唯一实体时解析。

从阶段四 schema 升级后，对每个授权空间分别以 `RecordKind.FACT`、`RecordKind.SOURCE` 和 `RecordKind.ENTITY`
调用 `backfill_retrieval(kind, after_id=cursor, limit=100)`。每种类型从 `None` 开始，传回上一页返回的游标，
直到返回 `None`。重跑会跳过已投影记录并复用原文向量任务。`source_vector_operation(source_id)` 返回当前模型与维度
的辅助任务，可通过已有的 `retry_operation` 重试。需要重建边时，`rebuild_graph(after_id=cursor, limit=100)`
按相同方式分页，根据事实和现有权威实体决定重建。

| 数据组 | 结构与所有权 |
| --- | --- |
| 账号与空间 | users、api_keys、memory_spaces、sessions；账号不依附某把 key，空间显式创建 |
| 权威业务数据 | sources/source_summaries、facts/fact_sources、working_memories/working_sources、conflicts、import_receipts |
| 实体与检索派生数据 | entities/entity_merges、graph_relations、embeddings、lexical_documents/lexical_terms |
| 操作与恢复 | operations、operation_sources/targets/products/receipts；唯一任务账本，不新增 outbox |

每张空间业务表有 user_id、space_id，关联使用包含归属的复合外键。
多态目标使用互斥的可空复合外键；向量绑定文本版本、模型及维度，不作为领域记录内联权威字段。
时间列为 timestamptz，边界转换有限 epoch 值；必要业务字段使用显式列，JSONB 不代替领域建模。
任务及产物的 stats 必需键、类型和非负整数由数据库约束验证；执行器在完整产物写入和恢复时校验结构与引用。
实体合并的环检查、图构建规则和候选查询语义由业务层和数据库集成回归验证。

迁移使用顺序版本、脚本校验和和事务级 advisory lock；已应用结构的目录指纹覆盖列、约束、索引、策略、函数及授权。
普通连接只验证结构；缺失结构、未知版本或漂移明确报错，不自动修复。
角色/扩展首次配置需要 DBA，普通迁移凭据不具备 SUPERUSER/CREATEROLE。
初始化、配置及受信任 Python 空间接口见[Postgres 基础配置](../reference/configuration.md#postgres-数据库基础)。

保留的检索契约：BM25 使用 stems、k1=1.5、b=0.75，从同授权/时间作用域的文档与词项聚合 N、总长度和 df；
空文档计入 N，query terms 为集合，不以 Postgres 全文排序替代。
先做槽位头/图排除，再计算对应语料统计；旧 ANN 候选池统计差异不作为质量等价证据。
参与语义召回的原文在受理事务提交后异步生成向量，已保存的合格来源通过分页任务补建。
原文向量绑定 `content_version`、模型及维度，提交时复核来源和作废状态。
待就绪或失败的向量不参与该信号，查询通过候选覆盖限制报告缺失。原文读取和已就绪的检索信号继续可用。
图边认知时间回查支撑事实，沿用实体归一化与谓词建边规则；权威别名/合并不能只靠事实重建。


## 三阶段提交、恢复与权限

| 提交点 | 同事务内容 | 事务外动作/恢复 |
| --- | --- | --- |
| T1 接受 | 校验身份/空间/模型配置后，保存原文、分配序号、登记任务 | 提交后确认接受；原文持久化不依赖先生成向量 |
| T2 产物 | 校验租约/来源/配置/上下文与作废标记，保存校验后的模型产物 | 生成必要向量；失败复用有效产物，不重复调用模型；T2 本身不推进正式业务版本 |
| T3 完成 | 校验前置条件、当前必要历史；提交事实/失效/链路/工作记忆/必要投影/向量/回执 | 主任务完成，辅助操作独立；未完成语义归并的候选不进入默认事实读取 |

状态沿用 pending → running → completed，暂时故障进入 retry_wait，确定性错误进入 failed，擦除/作废进入 cancelled。
阶段沿用 queued/generate/embed/commit/compensate/done，持久化标志独立表达。T2 不是正式事实发布。
领取原子增加执行尝试数，重领同样消耗预算；续租不增加次数、不推进业务版本。
尝试上限和退避定义维护在[任务实现](../../mnemotrace/store/postgres/tasks.py)，进程中断后的重新领取同样消耗预算。
模型与 embedding 在事务外执行，API 与 worker 共用执行器；同会话依赖有序，跨会话可并行。

| 变化 | 抽取产物有效性 | 归并/向量 |
| --- | --- | --- |
| 租约转移 | 新执行者重新校验后可复用，旧执行者不能提交 | 新执行者重新检查当前必要历史 |
| 来源缺失或版本变化、空间或当前会话的 generation 变化 | 作废旧产物，已删来源不重新抽取 | 拒绝提交失效结果 |
| 抽取配置/策略变化 | 重新抽取 | 按当前配置归并 |
| 实际用于模型的业务上下文版本改变 | 保守重新抽取，不建细粒度依赖图 | 旧计算结果作废 |
| 未用于抽取的业务事实改变 | 可复用；context_revision=null 必须由输入准备逻辑证明未读取相关上下文 | 总是重读/重算归并，不直接提交旧结果 |
| 仅 embedder 变化/embedding 失败 | 可复用 | 按 model/dimension/文本版本生成向量并重新归并 |
| 仅心跳/错误/重试计数变化 | 可复用 | 不触发业务版本冲突或重新调用模型 |

输入包含已存摘要、摘要片段组合或冲突事实候选时，context_revision 必填；源正文版本不能代替这些上下文版本。
摘要上下文带 representation_source 指纹；只有有明确 parts 引用的摘要组合允许该字段为空，恢复时另验证片段引用/版本。

统一锁顺序为 users → memory_spaces → sessions → operations → sources/其他业务记录，同类按 ID 排序。
普通提交在用户行取共享锁、空间行取排他锁，同账号不同空间仍可并行；账号删除取用户排他锁阻止新提交再按空间 ID 清理。
空间删除争用同一空间行；会话清理锁对应会话并推进 session_generation，提交在同事务检查，不能先查后在另一事务写入。
领取/续租也遵守用户/空间先行顺序；后台发现只返回候选 ID/归属，不持任务锁反向锁用户。
领取使用短事务的 FOR UPDATE SKIP LOCKED；保存/提交再次校验 token 与 lease_until。模型期间释放全部数据库锁。
清空保留空间 ID/名称/配置，删除 ID 不复用；直接事务清理相关原文、事实、派生数据、产物及正文副本，失败不提前确认完成。
删除账号先阻止新请求/提交，再清理全部 key/空间；备份按部署侧策略到期清除，不迁入或覆盖旧目录。

| 角色/受限路径 | 权限 |
| --- | --- |
| runtime | 普通业务和用户空间能力；无 SUPERUSER/BYPASSRLS、账号/key 管理或 DDL 权限 |
| migration | 扩展/schema/约束/RLS/授权初始化升级，不用于运行请求 |
| account_admin | 受限 CLI 创建/删除账号、签发/撤销 key，不向普通用户 key 开放 |
| auth lookup | 受限函数/权限，仅由摘要查询有效身份/账号状态，不返回业务内容 |
| worker discovery | 受限函数/权限，仅返回候选操作 ID 与归属，处理时回到 runtime 作用域 |

部署 worker 时，先由 DBA 执行数据库角色初始化，再运行迁移。阶段三数据库升级时也要先执行这一步，
以建立 worker 的两个 NOLOGIN 角色。向独立登录账号授予 `<prefix>_worker_discovery`，
通过 `MNEMOTRACE_DISCOVERY_DATABASE_URL` 配置它。函数所有者 `<prefix>_worker_discovery_owner`
由初始化和迁移维护。使用现有的 `MNEMOTRACE_DATABASE_URL`、`MNEMOTRACE_LLM` 和
`MNEMOTRACE_EMBEDDER` 运行 `python -m mnemotrace.worker`。请求内执行与 worker 使用相同的模型和空间策略配置。
未配置 `MNEMOTRACE_LLM` 时，worker 只执行与其 embedding 模型和维度匹配的原文向量任务。

业务表默认用户级 RLS，USING/WITH CHECK 同时约束；DAO 额外约束空间。
runtime 不作为表所有者，业务表强制 FORCE RLS。身份与查询在同一事务/连接，事务局部设置，缺失拒绝访问。
数据库基础目前不提供关闭 RLS 的配置。真实运行角色下验证池复用、回滚、撤销和后台上下文，不能用管理员或契约测试代替。
首版不缓存认证：撤销后下一次认证失效，已受理任务按用户状态/空间/租约处理；撤销凭据不等于删除账号或记忆。

## 调用方清单

按实际函数职责核对完整集合读取和字段写入；同组共享目标操作，不以搜索命中计数代替检查。
下表保留改造前的调用位置，供核对消费者和清理旧路径；阶段号表示实施归属，验证列列出对应回归。

| 改造前调用入口 | 改造前行为 | 产品契约及清理要求 | 阶段 | 验证 |
| --- | --- | --- | --- | --- |
| types: 六类记录、Fact.slot；spaces: Principal/load_spaces/principal_for_token；server: auth/auth_principal | user_id 同时分区/身份，Relation 无归属，home/共享 ACL/开放 key 命名空间 | 账号与必填空间分开，subject 独立，复合关联；移除旧默认/共享兼容 | 2—3、6 | user_scope、spaces、api_key_validation、storage_contracts |
| service: __init__/_safe_user/_contained_child/_secure_path/_path | 数据目录/文件名/后端选择 | 连接生命周期；归属不拼接文件路径 | 2、8 | service_paths、storage_configuration |
| service: write_lock/_read_scope/get/_get_locked/_load_locked/_cached/_publish/_trim_cache/_save；memory: save/open/_prepare_vector_restore | 全空间恢复、缓存、manifest 身份、文件锁、后端恢复迁移 | 必要工作集、快照读取与显式记录提交；去掉产品文件恢复 | 3、8 | service_transactions、persist_roundtrip/crash、restore_validation |
| _namespace_lock/_flock；persist: discover_processing_namespaces；_pipeline_worker | 本地协调、目录扫描与空间恢复 | DB 发现、租约领取/续租/重领 | 2、4、8 | flock、message_recovery、forget_concurrency |
| memory: _all_facts/_upsert_fact/_sync_episode/_rewire；store.memory_store | 可变对象及完整内存索引 | 记录工作集、显式提交；内存实现留作算法对照 | 3、5 | core_default、retrieval_candidates |
| ingest.pipeline: add；memory: add/remember/remember_messages；service: remember/remember_messages | embedding 后写原文，默认用户、路由/任务随快照保存 | T1 原文/任务、消息去重、来源 agent；原文不依赖向量 | 3—4、6 | extraction_contract、message_pipeline/interfaces、pipeline_sources |
| memory: import_messages/_document_inputs/_import_documents/import_data/import_snapshot；service: import_/import_document | 来源扫描、内存 import_seen、快照导入、全量统计 | 正常导入走新协议/import_receipts；不增加旧快照迁移 | 3、6、8 | import_dedupe/snapshot/cli、connectors、documents |
| consolidate.pipeline: inspect/accept/register/rows/trigger/retry/snapshot/response/invalidate/validate_state | 空间 ledger、序号、窗口/覆盖、单 generation，stats 恢复校验不足 | 操作/来源/产物/回执；会话精确 generation，必需字段严格校验 | 4、7 | message_pipeline/windows/recovery、storage_contracts |
| _pipeline: new_window/trigger_ready/enqueue_operation/_commit/_advance/_fail/run_one/execute_sources/execute_memory/flush | 修改事实、身份/aliases、working、来源摘要/覆盖、窗口统计 | T2/T3、租约/版本守卫、统一执行器，摘要片段可靠保存 | 4 | pipeline_sources、async_consolidation、message_recovery |
| consolidate.planning: plan/summary_plan/supplied；fact_rules 主体归一化 | 来源上下文、身份/别名、窗口预算和主体推断 | 必要会话/主体上下文，记录实际业务版本；用户/assistant 自称分开 | 3—4 | message_windows、pipeline_sources、profile_evidence |
| consolidate.facts: _accepted/_write_fact/_ensure_projection/_decay_sweep | 全事实归并、规范化对象/文本、去重 provenance、失效链路、显著度 | 完整必要主体/槽位/语义历史；显式修改，衰减为维护操作 | 3—4 | fact_statement_updates、conflict、message_recovery |
| consolidate.conflict: _supersede/insert_dialogue_history/reconcile；conflict_candidates | 双时间轴/链路重连、重复来源、语义候选 | 必要历史读取，同事务更新；归并向量先就绪 | 3—4 | conflict、search_answering |
| consolidate.decay: reinforce；memory: _reinforce_fact/apply_focus/_classify | 查询中改 access_count/last_access/salience，分类/focus 改字段 | 显式业务提交或维护事务，不隐藏持久化副作用 | 3、5 | service_transactions 的 read_side_effects、classification |
| memory: reembed/evict_cold/_enforce_hot_limit/_page_hot/_retrieve_cold | 全量重嵌入/冷热搬运和内存索引 | 版本化批处理；正常读取不依赖完整冷热内存 | 3、5、8 | reembed、retrieval_candidates |
| memory: add_fact/update_fact/delete_fact；service: 对应方法/clear_slot | 修改 SPO/text/display、人工权威、分类/topic/时间，删除来源/关系 | 人工记录提交、精确擦除，保留历史/来源/修正 | 3、6 | fact_metadata_updates、fact_statement_updates、forget_links |
| memory: remember_working/working_memory/clear_session/consume_working/sweep_working | 全量筛会话、TTL、消费与删除 | 会话/到期查询、显式更新与精确作废 | 3、7 | smoke/outcomes、message_recovery |
| memory: pending_conflicts/resolve_conflict/dismiss_conflict；service: conflicts/resolve_conflict | 状态修改、双方失效和 supersedes | 冲突、双方事实、必要图同事务 | 3 | conflict、forget_links |
| memory: set_focus/set_policy/_apply_policy/build_persona/structured_profile/profile | 修改配置、扫描事实、缓存 persona | settings 权威、条件读取；persona 可重建 | 3、5 | profile_display/evidence、configuration_isolation |
| memory: summarize_episodes/retrieve_summaries；consolidate.summarizer | 来源/摘要扫描及内存向量 | 版本化摘要、summary 向量、辅助任务 | 4—5 | summary_interfaces、pipeline_sources |
| retrieve.hybrid: retrieve/_ann_candidates/_graph_scores/_graph_neighbor_facts/graph_exclusion_zone | 全 live 或 ANN+图池、内存 BM25/五信号 | DB 候选/作用域统计/局部图，Python 槽位头/排除/融合 | 5 | retrieval_candidates、storage_baseline、search_answering |
| retrieve.planner；memory: _graph_related_facts/_entity_notes/_graph_paths_block/graph_data | 实体、全关系、邻接与事实回查 | 授权空间/时间下结构化遍历和事实查询 | 5 | graph_query、planner |
| memory: _persona_at/_preference_block/_history_block/_temporal_history_result/_chain_facts_for_seeds/history | 扫事实形成当前槽位、历史、偏好 | 主体/槽位/历史链查询，必要历史不受候选预算截断 | 3、5 | smoke temporal/history、fact_statement_updates |
| memory: _provenance_raw_block/_provenance_detail_chunks/_aggregation_block/_duration_block/_procedural_candidates/procedural | 扫来源/事实补证据、计数/时长/规则 | 来源 ID 回查、聚合和条件读取，保留证据/排除规则 | 5 | lean、profile_evidence、event_representation |
| service: _working_sources；pipeline: cancel_missing/identity_saved；_pipeline: _pending/_select/_current | 来源/窗口集合扫描，检查取消、身份完成和会话队头 | 按来源/会话查询操作关联和回执；只遍历已读取候选/冲突对 | 3—4 | pipeline_sources、message_recovery |
| hybrid: query_entity_ids/graph_excluded_entity_ids；planner: _anchor_entity/_user_predicates/_user_entities/_bridge_paths | 实体/谓词/桥接路径完整扫描 | 名称/别名/主体查询、局部邻接与谓词聚合 | 5 | planner、graph_query、lean |
| memory.reembed 内部 _clear；classify: classify_fact；memory_store: upsert_entity/invalidate_relations_for_fact/delete_relations_for_fact/prune_orphan_entities | 清索引、修改分类/别名和关系有效时间、删除邻接/孤立实体 | 版本化重嵌入、显式分类/图事务；人工别名/合并锚点不随派生图清理丢失 | 3—5 | reembed、classification、forget_links |
| memory: _current_state_block/_should_abstain；structured: build_structured_profile；agentic: gather_episodes；aggregate: dedupe_aggregation_candidates；conflict_candidates: candidate_pairs；lexical: bm25_scores | 遍历局部 latest/diag/basic/prefs/seen/best/by_subj/doc_tokens 字典 | 保留局部算法；输入来自授权必要工作集，BM25 统计从同作用域数据库语料提供 | 5 | smoke、profile_display、lean、storage_baseline |
| memory: lean_context/search/context_for/retrieve_episodes/get_episode；service: recall/recall_multi/get_episode | 内存候选/working_set、完整 token 估算与可选 answerer | DB 候选/必要工作集，逐空间鉴权融合；保留入口差异 | 5—6 | lean、search_answering、spaces |
| service: processing_status/_processing_status/flush/close | 扫空间来源/任务、按 namespace 等待 | 操作/会话查询、DB 租约恢复 | 4、6 | async_consolidation、message_recovery |
| service: close_session/session_report/agent_status/sessions | 扫原文/working/facts 汇总，close 捕获边界清理 | 会话查询和聚合。整理会话复用 flush_processing，移除旧关闭入口、自动清理和会话结论 | 3、6 | agent_sessions、message_recovery、outcomes |
| service: graph/memories/audit/_embedder_blindness/stats | 全量分页、实体/关系统计、敏感过滤与审计 | DB 分页/聚合，审计走显式维护接口 | 3、5—6 | memory_pagination、graph_query、metrics |
| service: forget/export；persist: save_memory/load_memory/_memory_state | 全空间擦除/导出、manifest 携带策略/身份/去重/任务，缺失可当空库 | 删除事务/作废；新格式流式导出恢复；不迁旧目录 | 3、7—8 | forget_concurrency/lock_cleanup、snapshot_validation、persist_safety |
| server.app 全业务路由/openai_compat | 部分支持 space/home，部分直接使用认证命名空间 | 全部显式 space_id，代理携带操作标识 | 6 | server、openai_compat、message/topic_interfaces |
| mcp.server/backends/__main__ | 本地固定 namespace，远程旧参数 | 配置身份/空间，共享新进度 | 6 | mcp、mcp_credentials、message/summary_interfaces |
| TypeScript client/types；frontend api/auth/queries/ScopeSelect | 可选 space/home、旧任务类型和凭据缓存 | 同步参数/结果，空间列表/创建，隔离切换状态 | 6 | SDK typecheck/test，前端 query-isolation/test/build |
| connectors.__main__/watch/session_hook；agent_config/setup/doctor | namespace/文件参数、导入去重/采集游标、配置与诊断 | 显式空间/新进度；watcher 游标保留但不是权威记忆 | 6、8 | import_cli、watch_install、session_hook、agent_setup/doctor |
| eval harness/bench/facility/ingest/seed_console；examples/quickstart | 内存/文件 Memory、用 user 分数据 | 算法离线对照保留；产品显式数据库空间；真实模型评测另授权 | 5、8 | eval_harness、facility、seed_console、core/release |

局部图、候选列表和事务内小字典的 values() 可保留；它们不等于完整空间读取。
底层内存/第三方向量后端迭代在阶段八按消费者清理，纯算法替身保留。
临时新对象赋值随新增记录提交；已加载对象字段修改必须进入所属变更集合。

## 行为对照与验证输入

| 分类 | 保留的语义或改造前问题 | 验证入口 |
| --- | --- | --- |
| 保留 | 人工权威、来源/双时间轴/历史、同会话顺序、角色区分、会话范围 | 固定模型回归和离线基线 |
| 改变 | default/home、跨用户 ACL、文件快照、全空间内存检索 | 新契约和真实数据库测试 |
| 记录级原子提交 | save_memory 逐个替换集合，最后替换 manifest | [记录事务](../../tests/postgres/test_record_business.py)的回滚/一致读取，以及[进程恢复](../../tests/postgres/test_process_recovery.py)的三个提交点前后中断 |
| 未知空间与损坏拒绝 | _load_memory 缺失 manifest 时返回空库 | [空间身份](../../tests/postgres/test_identity.py)及[数据库事务](../../tests/postgres/test_transactions.py)的未知空间、损坏与连接失败区分 |
| 任务与产物校验 | validate_state 未逐键验证 stats，提交直接访问统计 | [数据库约束](../../tests/postgres/test_constraints.py)及[恢复校验](../../tests/postgres/test_process_recovery.py)的必需字段、检查点缺失、非法引用和向量维度 |
| 原文先落库 | service.remember/messages、ingest.add 先 embed | [流水线恢复](../../tests/postgres/test_processing.py)验证向量失败仍保留原文/产物，重试复用有效产物 |
| 会话精确作废 | invalidate(session_id) 改空间 generation、取消全部未终态窗口 | [会话并发清理](../../tests/postgres/test_process_recovery.py)验证目标会话任务取消，其他会话继续处理，原文和长期事实保留 |

[跨进程回归](../../tests/postgres/test_process_recovery.py)使用 `spawn` 和独立数据库连接，
在模型调用、向量生成和数据库提交边界同步进程，覆盖租约转移、迟到提交、重试预算、重复消息及版本冲突后的重新归并。
删除竞争分别验证清理先提交时拒绝旧结果，以及业务提交或图重建持锁时删除等待、随后完整清除结果。
清理范围同时核对原文、事实、工作记忆、向量、词法、图、任务产物和完成回执。
[凭据传输回归](../../tests/postgres/test_mcp_credential_transport.py)验证撤销 key 后下一次 HTTP 与远程 MCP 请求失败，同用户另一把有效 key 仍可访问。
测试数据库、实际角色和禁止跳过的配置见[数据库回归说明](../reference/configuration.md#postgres-数据库基础)。

[离线基线](../../eval/storage_baseline.py)复用 eval.harness、完整十题合成集及固定响应，固定时钟，
随机 ID 映射为按事实内容排序的 fact-N 和输入顺序的 source-N。
[稳定观察](../../eval/fixtures/storage_baseline.json)保留答案/拒答、上下文 token、排名/分数、事实/来源/历史和词法分数。
每次运行另存检索 p50/p95、环境/配置/commit、任务相关 tracked diff 和未提交文件原文。
diff 包含运行实现、评测代码和固定模型响应的相关已跟踪改动；未知的新评测源文件须先暂存或提交，否则拒绝生成不完整的复现记录。
快照仅是算法对照，不是生产保存/全空间加载机制。

```powershell
python -m eval.storage_baseline --out .artifacts/storage-phase1/reference
python eval/report.py .artifacts/storage-phase1/reference/synthetic.jsonl
python -m pytest tests/test_storage_contracts.py tests/test_storage_baseline.py
```

输出目录必须全新，不覆盖旧 run，保留至整体验收；之后只清理本任务产物。长期 fixture 随代码维护。
这是 hashing+固定响应的离线结果，没有真实 answerer/judge，naive recall 不是正式 full-context 模型基线，
不用于公开端到端质量/总模型 token 或数据库性能结论；上下文 token 不称为提供方总 token。

[负载清单](../../eval/fixtures/storage_workloads.json)固定种子、三种空间分布、64 维向量、25% 历史、
1%/10%/100% 过滤桶、查询、top_k、候选预算、并发、预热和重复次数。workload_records 流式生成，不提前建大库。
查询向量使用独立 query_vector_seed，按清单查询次序逐个生成 dimension 个 uniform(-1,1) 坐标，距离 cosine。
每个负载仅选择实际存在的 query_space_indices，条件为 filter_bucket < 桶数量。
current 在 BASE+86401，before-invalidation 在 BASE+43200，all-history 忽略失效终点；事件时间固定。
用 `python -m eval.storage_postgres --out .artifacts/storage-phase5/comparison` 运行合成质量和向量负载对照。
设置 `MNEMOTRACE_TEST_POSTGRES_DSN` 指向隔离的测试 DBA 实例，评测为每类负载创建并清理专用数据库。
使用 Docker 复现八并发负载时，为测试容器配置 `--shm-size=1g`，供 Postgres 并行查询分配共享内存。
可用 `--case` 和 `--concurrency` 单独补跑负载组。测量样本数至少等于所选最高并发数。
精确与近似应用相同授权和时间过滤，精确结果作 recall@k 对照，同分按稳定 ID 排序，不足 k 以实际数量为分母。
HNSW 索引只在评测库创建。原始记录保存执行计划、样本覆盖、返回数和延迟。
记录精确 ID/近似召回/返回数/检索 p50/p95，建库、索引构建及查询耗时分别记录。验收未预设绝对延迟门槛。

复用事实/冲突、消息流水线/恢复/接口、候选/图、用户/空间、服务事务/损坏/擦除及 eval harness 回归。
契约测试验证结构、版本和归属检查；数据库外键、原子性、连接池、RLS 和跨进程竞争使用真实数据库回归验证。
实际命令、结果、失败和跳过范围记录在对应任务、提交或 CI；评测产物按各入口要求保存，本页不维护进度表。
