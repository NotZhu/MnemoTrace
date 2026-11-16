# Postgres 改造契约与行为对照

本文维护 Postgres 产品路径的数据契约、调用方对照和验证输入。
[契约代码](../../mnemotrace/store/contracts.py)约束授权空间、记录变更、读取版本、任务产物和提交回执。
产品接入端使用授权空间内的 `Memory`，共享三阶段任务流水线和数据库混合检索；
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
| CLI | 直连或 HTTP 各按既有认证方式；必选空间 | 接受、查询、重试使用同一操作 ID；失败不返回空结果 |
| 前端 | 列出/创建并选择空间后才能操作 | 切换 key/空间/会话取消旧请求并清理展示和查询缓存 |

agent_id 可为 null；非空为 1—128 个 ASCII 字符 `[A-Za-z0-9_.:@/-]`，空串不表示空值。
message_id/session_id 为 1—512 字符非空标识，原样保存，不 trim 或折叠大小写。
消息仅允许 user/assistant，正文至多 1000000 codepoint 且不能全空白，event_time 为有限数值或 null。
role、正文、规范化为浮点的事件时间及 agent_id 共同形成消息指纹。摄入时间独立记录。
唯一键为 `(user_id,space_id,session_id,message_id)`，space_id 本身全局唯一。
同键同指纹复用来源和任务，同键异指纹返回 message_conflict；原文修正通过新消息提交。

`POST /v1/remember` 在同一事务保存本批原文、消息序号和操作 ID。
本批按原顺序完整重传时复用原任务，已有 ID 与新 ID 混合或改序会导致冲突。
默认返回 HTTP 202 和任务，`wait=true` 等待该任务发布，超时仍返回 202 并继续后台执行。
查询为 `GET /v1/operations/{operation_id}?space_id=…`，重试原任务为
`POST /v1/operations/{operation_id}/retry`，请求体含 `space_id`。
错误映射与响应字段见 [HTTP API](../reference/http-api.md#post-v1remember)。

### 接入示例

用管理 CLI 创建账号与 key 后，使用 `POST /v1/spaces` 和请求体 `{"name":"研究"}` 创建空间。
`GET /v1/spaces` 返回 `spaces` 和 `next_after_id`，包含空空间。
`PATCH /v1/spaces/{space_id}` 用同样的请求体改名。
`DELETE /v1/spaces/{space_id}?confirm=true` 删除空间。
`POST /v1/forget` 用 `{"space_id":"…","confirm":true}` 清空空间并保留其 ID 与配置。

直连 Python 从 `mnemotrace` 导入 `Memory`。直连 CLI 和 MCP stdio 使用
`MNEMOTRACE_DATABASE_URL`，并显式传入 `--user-id`、`--space-id`。
MCP stdio 代理使用 `--api-url`、`--api-key-file`、`--space-id`。
Streamable HTTP MCP 每次调用携带 Bearer 凭据和 `X-MnemoTrace-Space-Id`。
OpenAI 兼容请求通过 `memory.space_id` 选择空间，受理的任务和来源通过 `mnemotrace.task` 和 `mnemotrace.source_ids` 返回，
流式响应在结束帧返回同一字段。

```text
mnemotrace --user-id <USER_ID> --space-id <SPACE_ID> operation <OPERATION_ID>
mnemotrace --user-id <USER_ID> --space-id <SPACE_ID> retry <OPERATION_ID>
```

TypeScript SDK 的单空间方法接受 `{ spaceId }`，多空间 recall 接受 `{ spaceIds }`。
独立 Postgres worker 执行抽取。调用方可以等待本次任务或按操作 ID 查询。

## 工作集、版本与变更集合

工作集绑定 SpaceContext、ReadSnapshot；禁止任意 where 回调、普通请求完整 values() 或完整图恢复。
ReadView 在同一连接的 REPEATABLE READ 只读事务内读取版本与多表数据，返回脱离连接的记录。
分页默认 100、最多 1000、稳定 ID 游标；必要主体/槽位历史走专用 history 读取，不受候选 top_k 截断。
StoredRecord 的 JSON 是不可变的单条 domain 交接表示，不是任意 SQL 列补丁或空间快照。
初始化接受空库或完整的新基线。旧基线及旧检查点不会自动转换。
agent_id、归属在 envelope 明确给出。内联向量不作为权威列；存在内联向量时必须有版本/角色/坐标一致的 EmbeddingWrite。
CandidateBatch 返回读取快照、候选信号和覆盖限制，空结果成功与索引未就绪/降级分别表达。
`PostgresDAO` 提供空间内快照和业务提交，`Tasks` 负责领取、续租和检查点。跨空间发现与账号管理使用独立连接。

| 版本/标记 | 推进条件 |
| --- | --- |
| core_revision | 空间正式业务内容变化时 +1；任务状态、心跳、错误不推进 |
| record_version | 单条业务记录变化时 +1，插入为 1；摘要/显著度等元数据也推进 |
| content_version | 用于抽取/嵌入的正文变化时推进；原文不可变，摘要更新不改变原文版本 |
| digest_version | 来源摘要自己的文本版本；digest 向量绑定此版本，不绑定原文 content_version |
| space_generation | 清空/删除等空间级作废推进；普通业务更新使用 core_revision |
| lease token | 每次领取生成新值；续租/保存/提交校验 token 和数据库时间下的有效期 |

FactQuery 按有效时间筛选。未指定日期时取快照时间并允许起点未知的事实；指定日期时要求已知的有效起点。`CURRENT` 返回有效区间 `[valid_at, invalid_at)` 覆盖查询时点的事实。`HISTORY` 返回截至查询时点已经结束的事实。`ALL` 合并当前与已结束的事实，完整槽位时间线也列出未来生效的事实。

产品使用数据库时钟记录收录及首次获知失效的时间。以后补录的事实可以改变过去有效日期的查询结果；查询不按收录时刻回放旧认知。每条事实保留一行，替代关系和来源各存于空间内关联表。已失效事实仍可查询历史。

ChangeSet 显式包含记录写入、事实失效、擦除、来源关联、向量、实体合并、空间设置、后续任务及回执。
一个记录只有一次主修改；失效仅用于 Fact，擦除遵守用户范围。settings 替换整个小型空间配置，主体名不提供账号身份。
完整事实写入通过有序的 provenance 提供来源 ID。提交校验 ID 格式，数据库外键校验来源存在且属于同一空间。新向量版本必须匹配对应文本，不能为本次擦除目标新增派生数据。
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

`Memory(database, context)` 接收已打开的运行角色连接池与受信任的 `SpaceContext`。
调用方管理连接池，DAO 每次在事务中验证账号和空间。读取返回脱离连接的领域记录，
`memory.dao.read_snapshot()` 提供单次事务内的多表查询及包含版本和 agent 来源的记录封套。

```python
from mnemotrace import Memory
from mnemotrace.store.contracts import MessageInput
from mnemotrace.store.postgres import Database, DatabaseConfig, Spaces

with Database(DatabaseConfig.from_env()) as database:
    context = Spaces(database).authorize(user_id, space_id)
    memory = Memory(database, context, llm=llm)
    saved = memory.remember([MessageInput("m1", "user", "Synthetic input")], "thread-A", wait=True)
    episode = memory.get_episode(saved["source_ids"][0])
    result = memory.search("What does Synthetic person like?")
    context_result = memory.lean_context("What does Synthetic person like?")
    print(result.status, [fact.text for fact in result.facts], context_result.limitations)
```

`source_ids` 按输入顺序排列，完全重传复用原来源和任务。
直接提交回执与数据同事务提交；连接中断且无法查证时抛出带操作 ID 的
`CommitOutcomeUnknown`，可在同空间用 `memory.dao.receipt(operation_id)` 查证。
`erase` 和 `ChangeSet.erasures` 接受 `FACT`。
批次包含其他类型时，在开始写事务前整批拒绝。单条来源、实体和关系通过上级范围擦除或关联清理。

空间清空、空间删除和账号删除各自按授权范围清理数据与任务。
默认 `export_records()` 仅流式导出非敏感事实。显式指定 `include_sensitive=True` 可读取全部领域记录。整库恢复使用[数据库归档](../../deploy/README.md#备份与恢复)。
事实归并基于同一空间快照。事实文本超出图实体名或谓词的长度限制时，
事实照常保存，图中省略该边。

`Memory(database, context, llm=llm)` 启用抽取。字符串和消息序列通过 `remember` 进入相同的处理链路。
一个请求对应一个任务，前文来自同会话本批之前最多十条原文。失败的较早消息仍可作为前文，内部只存档的消息会排除。
首次生成前保存实际请求。有效产物只有在配置、来源或实际前文失效时重新生成。事实、摘要、索引及回执同时提交。

#### 数据库检索与补建

`search` 返回带排序事实和覆盖限制的 `SearchResult`。`retrieve_digests` 只返回摘要证据和覆盖限制，完整原文由 `get_episode` 读取。`context_for` 和 `lean_context` 返回 `ContextResult`，
其中 `context` 是组装文本，`coverage_by_space` 给出各空间的限制。`recall_multi` 先校验所有空间的归属，
再分别读取并标记空间。`as_of` 同时设置有效时间和已知时间。`search(facts=FactQuery(...))` 可独立指定两条时间轴。
历史检索仅在当前摘要版本的写入时间不晚于已知时间时使用该摘要。摘要生成时间未知或当前版本尚未写入时，
该来源不进入候选。被替换的摘要版本不保留。
`FactQuery.limit` 在候选阶段限制各信号的返回数，融合后的结果数由入口参数决定。
向量、词法、新近度与显著度共用筛选后的事实范围，再应用各自的候选上限。
搜索结果基于读取快照。若并发写入使访问强化的前置版本失效，本次跳过强化，仍返回快照结果。

向量查询默认精确，并根据本次空间和过滤值规划。词法使用相同作用域的 BM25 语料统计。
`complete` 表示没有已知的索引或候选覆盖限制。
`limitations` 区分词法补建、事实或摘要向量不完整、图候选覆盖等限制。数据库连接与查询错误通过数据库异常传递。查找实体可用 `graph_entities(name)`，
邻接和事实边可用 `graph_neighbors`、`graph_relations`，两者接受 `FactQuery` 时间条件。
实体查询使用去除首尾空白并转小写后的名称。共享图的名称来自可共享的事实。

从 `mnemotrace.store.postgres.maintenance` 导入 `rebuild_lexical_index` 和 `rebuild_graph`。
用授权空间的 `memory.dao` 调用 `rebuild_lexical_index(memory.dao, kind, after_id=cursor)`，分别处理
`RecordKind.FACT` 和 `RecordKind.SOURCE`。来源补建只投影已有摘要。
`rebuild_graph(memory.dao, after_id=cursor)` 修复图边，并在末页清理孤立实体。
两者均从 `after_id=None` 开始，用返回的游标继续，直到返回 `None`。

| 数据组 | 结构与所有权 |
| --- | --- |
| 账号与空间 | users、api_keys、memory_spaces、sessions；账号不依附某把 key，空间显式创建 |
| 权威业务数据 | sources/source_digests、facts/fact_sources、entity_types/entities/relation_types |
| 检索派生数据 | graph_relations、embeddings、lexical_documents/lexical_terms |
| 操作与恢复 | operations、operation_sources、operation_products、commit_receipts |

`operation_sources` 每个任务和来源只保留一条空间内关联，记录来源版本、输入顺序及是否为上下文。实际模型输入范围和表示指纹见 `operation_products.input`。

每张空间业务表有 user_id、space_id，关联使用包含归属的复合外键。
多态目标使用互斥的可空复合外键；向量绑定文本版本、模型及维度，不作为领域记录内联权威字段。
时间列为 timestamptz，边界转换有限 epoch 值；必要业务字段使用显式列，JSONB 不代替领域建模。
执行器在产物写入和恢复时校验结构与来源引用。模型调用数由运行指标记录。
实体名称唯一性、图构建规则和候选查询语义由业务层和数据库集成回归验证。

空库初始化使用单份基线 SQL 和事务级 advisory lock。基线记录包含标识、校验和、角色前缀及目录指纹。指纹覆盖列、约束、索引、策略、函数及授权。
普通连接只验证结构；缺失结构、未知版本或漂移明确报错，不自动修复。
首次配置角色和扩展需要 DBA。专用初始化角色没有 SUPERUSER 或 CREATEROLE 权限。
初始化、配置及受信任 Python 空间接口见[Postgres 基础配置](../reference/configuration.md#postgres-数据库基础)。

保留的检索契约：BM25 使用 stems、k1=1.5、b=0.75，从同授权/时间作用域的文档与词项聚合 N、总长度和 df；
空文档计入 N，query terms 为集合，不以 Postgres 全文排序替代。
先做槽位头/图排除，再计算对应语料统计；旧 ANN 候选池统计差异不作为质量等价证据。
事实和来源摘要参与语义召回。事实向量绑定正文版本，摘要向量绑定摘要版本，均区分模型及维度。
待就绪或失败的向量不参与该信号，查询通过候选覆盖限制报告缺失。原文读取和已就绪的检索信号继续可用。
图边的有效时间从支撑事实读取，建边时使用已绑定的实体与关系。


## 三阶段提交、恢复与权限

| 提交点 | 同事务内容 | 事务外动作/恢复 |
| --- | --- | --- |
| T1 接受 | 校验身份/空间/模型配置后，保存原文、分配序号、登记任务 | 提交后确认接受；原文持久化不依赖先生成向量 |
| T2 产物 | 校验租约/来源/配置/上下文与作废标记，保存校验后的抽取产物 | 按当前空间版本解析实体和关系、生成向量及判定候选。失败可复用有效抽取产物 |
| T3 完成 | 校验前置条件和空间业务版本，原子提交采纳事实支持的实体类型、实体、关系定义和端点扩展，以及事实、来源、替代链、投影、向量和回执 | 主任务完成，辅助操作独立 |

状态沿用 pending → running → completed，暂时故障进入 retry_wait，确定性错误进入 failed，擦除/作废进入 cancelled。
阶段沿用 queued/generate/embed/commit/done，持久化标志独立表达。T2 不是正式事实发布。
领取原子增加执行尝试数，重领同样消耗预算；续租不增加次数、不推进业务版本。
尝试上限和退避定义维护在[任务实现](../../mnemotrace/store/postgres/tasks.py)，进程中断后的重新领取同样消耗预算。
模型与 embedding 在事务外执行。待抽取任务按空间序号领取，同空间由一个可续期租约保护，跨空间可并行。30 秒和 120 秒的退避期间，后续就绪任务可以执行。

本体解析和事实归并使用同一空间业务版本。归并读取最新已提交的目录和历史。

| 变化 | 抽取产物有效性 | 归并/向量 |
| --- | --- | --- |
| 租约转移 | 新执行者重新校验后可复用，旧执行者不能提交 | 新执行者重新检查当前必要历史 |
| 来源缺失或版本变化、空间代次变化 | 作废旧产物，已删来源不重新抽取 | 拒绝提交失效结果 |
| 抽取配置/策略变化 | 重新抽取 | 按当前配置归并 |
| 实际用于模型的业务上下文版本改变 | 保守重新抽取，不建细粒度依赖图 | 旧计算结果作废 |
| 未用于抽取的业务事实改变 | 可复用；context_revision=null 必须由输入准备逻辑证明未读取相关上下文 | 总是重读/重算归并，不直接提交旧结果 |
| 仅 embedder 变化/embedding 失败 | 可复用 | 按 model/dimension/文本版本生成向量并重新归并 |
| 仅心跳/错误/重试计数变化 | 可复用 | 不触发业务版本冲突或重新调用模型 |

联合抽取使用完整原文，保存的版本覆盖本批和实际采用的前文。归并单独校验业务快照。

统一锁顺序为 users → memory_spaces → sessions → operations → sources/其他业务记录，同类按 ID 排序。
普通提交在用户行取共享锁、空间行取排他锁，同账号不同空间仍可并行；账号删除取用户排他锁阻止新提交再按空间 ID 清理。
空间删除争用同一空间行，提交在同事务校验空间代次、业务版本、来源版本及租约，不能先查后在另一事务写入。
领取/续租也遵守用户/空间先行顺序；后台发现只返回候选 ID/归属，不持任务锁反向锁用户。
领取使用短事务的 FOR UPDATE SKIP LOCKED；保存/提交再次校验 token 与 lease_until。模型期间释放全部数据库锁。
清空保留空间 ID/名称/配置，删除 ID 不复用；直接事务清理相关原文、事实、派生数据、产物及正文副本，失败不提前确认完成。
删除账号先阻止新请求/提交，再清理全部 key/空间；备份按部署侧策略到期清除，不迁入或覆盖旧目录。

| 角色/受限路径 | 权限 |
| --- | --- |
| runtime | 普通业务和用户空间能力；无 SUPERUSER/BYPASSRLS、账号/key 管理或 DDL 权限 |
| migration | 初始化数据库结构、RLS 和授权，并校验现有基线 |
| account_admin | 受限 CLI 创建/删除账号、签发/撤销 key，不向普通用户 key 开放 |
| auth lookup | 受限函数/权限，仅由摘要查询有效身份/账号状态，不返回业务内容 |
| worker discovery | 受限函数/权限，仅返回候选操作 ID 与归属，处理时回到 runtime 作用域 |

在空库部署 worker 时，先由 DBA 配置扩展和角色，再初始化数据库结构。角色初始化会建立
worker 的两个 NOLOGIN 角色。向独立登录账号授予 `<prefix>_worker_discovery`，
通过 `MNEMOTRACE_DISCOVERY_DATABASE_URL` 配置它。函数所有者 `<prefix>_worker_discovery_owner`
由初始化维护。使用现有的 `MNEMOTRACE_DATABASE_URL`、`MNEMOTRACE_LLM` 和
`MNEMOTRACE_EMBEDDER` 运行 `python -m mnemotrace.worker`。请求内执行与 worker 使用相同的模型和空间策略配置。
联合抽取与摘要、画像任务均使用配置的 LLM。原文归档可独立写入。

业务表默认用户级 RLS，USING/WITH CHECK 同时约束；DAO 额外约束空间。
runtime 不作为表所有者，业务表强制 FORCE RLS。身份与查询在同一事务/连接，事务局部设置，缺失拒绝访问。
数据库基础目前不提供关闭 RLS 的配置。真实运行角色下验证池复用、回滚、撤销和后台上下文，不能用管理员或契约测试代替。
认证逐请求查验：撤销后下一次认证失效，已受理任务按用户状态/空间/租约处理；撤销凭据不等于删除账号或记忆。

## 行为对照与验证输入

| 分类 | 现行契约 | 验证入口 |
| --- | --- | --- |
| 记录级原子提交 | 写入、投影与回执在同一事务提交 | [记录事务](../../tests/postgres/test_record_business.py)的回滚/一致读取，以及[进程恢复](../../tests/postgres/test_process_recovery.py)的三个提交点前后中断 |
| 未知空间与损坏拒绝 | 查询前验证空间归属和当前基线 | [空间身份](../../tests/postgres/test_identity.py)及[数据库事务](../../tests/postgres/test_transactions.py)的未知空间、损坏与连接失败区分 |
| 任务与产物校验 | 严格验证来源引用、阶段和结果结构 | [数据库约束](../../tests/postgres/test_constraints.py)及[恢复校验](../../tests/postgres/test_process_recovery.py)的必需字段、检查点缺失、非法引用和向量维度 |
| 原文先落库 | 接受原文与处理事实分开提交 | [流水线恢复](../../tests/postgres/test_processing.py)验证向量失败仍保留原文/产物，重试复用有效产物 |
| 空间作废 | 清空、删除或账号删除使旧任务失效 | [并发擦除回归](../../tests/postgres/test_process_recovery.py)验证旧租约不能提交，其他空间继续处理 |

[跨进程回归](../../tests/postgres/test_process_recovery.py)使用 `spawn` 和独立数据库连接，
在模型调用、向量生成和数据库提交边界同步进程，覆盖租约转移、迟到提交、重试预算、重复消息及版本冲突后的重新归并。
删除竞争分别验证清理先提交时拒绝旧结果，以及业务提交或图重建持锁时删除等待、随后完整清除结果。
清理范围同时核对原文、事实、向量、词法、图、任务产物和完成回执。
[凭据传输回归](../../tests/postgres/test_mcp_credential_transport.py)验证撤销 key 后下一次 HTTP 与远程 MCP 请求失败，同用户另一把有效 key 仍可访问。
测试数据库、实际角色和禁止跳过的配置见[数据库回归说明](../reference/configuration.md#postgres-数据库基础)。

[离线基线](../../eval/storage_baseline.py)复用 eval.harness、完整十题合成集及固定响应，固定时钟，
随机 ID 映射为按事实内容排序的 fact-N 和输入顺序的 source-N。
[稳定观察](../../eval/fixtures/storage_baseline.json)保留答案/拒答、上下文 token、排名/分数、事实/来源/历史和词法分数。
多跳用例在离线参考中使用显式实体身份，Postgres 对照则通过固定的本体响应建立端点。稳定观察中的 `supersedes` 记录直接前驱 ID 列表。
每次运行另存检索 p50/p95、环境/配置/commit、任务相关 tracked diff 和未提交文件原文。
diff 包含运行实现、评测代码和固定模型响应的相关已跟踪改动；未知的新评测源文件须先暂存或提交，否则拒绝生成不完整的复现记录。
算法对照使用独立内存工作集，产品部署与恢复使用 Postgres。

```powershell
python -m eval.storage_baseline --out .artifacts/storage-check/reference
python eval/report.py .artifacts/storage-check/reference/synthetic.jsonl
python -m pytest tests/test_storage_contracts.py tests/test_storage_baseline.py
```

输出目录必须全新，不覆盖旧 run，保留至整体验收；之后只清理本任务产物。长期 fixture 随代码维护。
这是词法检索加固定响应的离线结果，没有真实 answerer/judge，naive recall 不是正式 full-context 模型基线，
不用于公开端到端质量/总模型 token 或数据库性能结论；上下文 token 不称为提供方总 token。

[负载清单](../../eval/fixtures/storage_workloads.json)固定种子、三种空间分布、64 维向量、25% 历史、
1%/10%/100% 过滤桶、查询、top_k、候选预算、并发、预热和重复次数。workload_records 流式生成，不提前建大库。
查询向量使用独立 query_vector_seed，按清单查询次序逐个生成 dimension 个 uniform(-1,1) 坐标，距离 cosine。
每个负载仅选择实际存在的 query_space_indices，条件为 filter_bucket < 桶数量。
current 在 BASE+86401，before-invalidation 在 BASE+43200，all-history 忽略失效终点；事件时间固定。
用 `python -m eval.storage_postgres --out .artifacts/storage-check/comparison` 运行合成质量和向量负载对照。
设置 `MNEMOTRACE_TEST_POSTGRES_DSN` 指向隔离的测试 DBA 实例，评测为每类负载创建并清理专用数据库。
使用 Docker 复现八并发负载时，为测试容器配置 `--shm-size=1g`，供 Postgres 并行查询分配共享内存。
可用 `--case` 和 `--concurrency` 单独补跑负载组。测量样本数至少等于所选最高并发数。
精确与近似应用相同授权和时间过滤，精确结果作 recall@k 对照，同分按稳定 ID 排序，不足 k 以实际数量为分母。
HNSW 索引只在评测库创建。原始记录保存执行计划、样本覆盖、返回数和延迟。
记录精确 ID/近似召回/返回数/检索 p50/p95，建库、索引构建及查询耗时分别记录。验收未预设绝对延迟门槛。

复用事实/冲突、消息流水线/恢复/接口、候选/图、用户/空间、服务事务/损坏/擦除及 eval harness 回归。
契约测试验证结构、版本和归属检查；数据库外键、原子性、连接池、RLS 和跨进程竞争使用真实数据库回归验证。
实际命令、结果、失败和跳过范围记录在对应任务、提交或 CI；评测产物按各入口要求保存，本页不维护进度表。
