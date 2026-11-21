# PostgreSQL 存储适配器

新后端使用 PostgreSQL 18 和 pgvector。迁移把业务表建在 `mnemotrace_next`，扩展位于 `public`。在包含 pgvector 的空数据库上，用具备建 schema 和安装扩展权限的迁移账号执行命令。托管 PostgreSQL 可由 DBA 预装扩展。

在新后端目录设置 `MNEMOTRACE_NEXT_MIGRATION_DSN` 后运行以下命令。

```sh
uv run --locked python -m mnemotrace_next.adapters.postgres.migrate status
uv run --locked python -m mnemotrace_next.adapters.postgres.migrate up
```

迁移锁保护待执行版本的检查和应用。迁移 SQL 按文件执行，使用无绑定参数的文本并禁用预编译。每次 `up` 把待执行的 SQL 和账本写入一起提交，失败时按迁移文件和 SQLSTATE 诊断。后续变更追加新编号的 SQL，已应用文件的校验和用于识别改写。新列、新表等扩展沿用现行查询的显式列清单。

`003` 从原文和摘要回填会话登记，保留既有 ID。旧事实的来源按一个共同证据组保留，旧审计的截止触发标识为空。迁移会删除替代链表及审计快照里的旧替代链字段。

`004` 将单个截止触发事实迁入有序触发表，保留原变更游标及完整擦除依赖。已有单项使用变更后的截止时间，逐项原文无法还原，读出时为 `None`。无触发事实的记录读出空列表。升级已有库前备份并停止旧适配器，在重新启动前应用迁移、更新运行角色授权并切换新版适配器。

`005` 为实体增加身份类型，将既有实体归为 `named`，保留 ID 和事实引用。每个空间最多一个 `space_user`，其确定性 ID 与同名普通实体分开。迁移随新版实体适配器切换。

## 运行账号与会话

数据库管理员为服务配置独立登录角色。以下授权在迁移后执行，其中 `mnemotrace_next_runtime` 是已创建的运行角色。

```sql
GRANT USAGE ON SCHEMA mnemotrace_next TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, UPDATE (purge_generation) ON mnemotrace_next.spaces
    TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, DELETE ON
    mnemotrace_next.messages, mnemotrace_next.fact_provenance,
    mnemotrace_next.sessions, mnemotrace_next.fact_changes TO mnemotrace_next_runtime;
GRANT SELECT, INSERT ON
    mnemotrace_next.embedding_spaces, mnemotrace_next.fact_change_triggers
    TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    mnemotrace_next.entities, mnemotrace_next.facts, mnemotrace_next.digests,
    mnemotrace_next.profiles, mnemotrace_next.fact_salience,
    mnemotrace_next.fact_vectors TO mnemotrace_next_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mnemotrace_next TO mnemotrace_next_runtime;
```

服务进程共享并管理 `psycopg_pool.AsyncConnectionPool`。服务端按已认证账号构造 `PostgresStorage(pool, owner_id=owner_id)`。在 Windows 上，运行异步池的进程可用 `asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)`。会话入口用可信账号和空间 ID 联合检查授权，会话里的 SQL 始终使用绑定空间。受限读取的可见性由服务端设置。

只读事务从授权查询取得一致快照。写事务持有空间行锁，成功退出时提交，块内异常或取消时回滚。若调用方捕获了 SQL 错误但写事务仍处于失败状态，退出时抛出 `RuntimeError` 并回滚。提交前的连接错误和数据库明确拒绝提交的错误原样传播。进入提交阶段后因断连或调用方取消而丢失确认时，适配器抛出 `CommitOutcomeUnknown`。收到提交确认后，即使调用方在连接归还期间取消，适配器也会完成归还并保留写入成功结果。数据操作的部署权限授予运行角色，租户间的访问由适配器显式授权和空间复合键限定。

`application.sessions.create_session` 通过服务端标识生成器创建对话，在空间写事务提交后返回标识。调用方用 `has_session` 检查 ID 是否在绑定空间内有效，`append_messages` 在写入时拒绝未登记或已擦除的对话。

应用在事务外生成向量和派生内容，发布前在写会话中复核对话登记、擦除标记及读入依赖。擦除对话即撤销登记，即使该对话尚无消息。擦除标记使用 `spaces.purge_generation`，在空间清空或实际删除对话原文的同一事务内推进，使同空间依赖旧快照的在途派生结果失效。空间清空撤销全部会话登记，保留空间及模型维度登记。已开始的只读事务仍按其快照返回数据。

`Memory.remember` 在抽取后生成向量前、读取候选时以及再次请求模型前复核对话登记和擦除标记。在锁外一次计算本批新事实之间的配对。在空间写锁内复用这些配对，并以原请求时刻和同一套[有界候选规则](inference.md#写入抽取)重新选取同槽与跨槽旧事实。出榜候选的判定不参与写入。仅候选移除、区间确定错开或证据组与敏感标记更新时，直接用锁内结果计算写入。候选入榜或比较依据发生实质变化时，释放锁并只重判受影响的比较对，随后重新复核一次。提交时从写会话批量读取重复或矛盾裁决所涉旧事实的最新完整证据组。画像引用事实的有效期、标签或敏感性发生变化时，在同一事务内清理画像。仅追加证据组时保留画像。原文、缺失实体、事实、证据组、词法及向量索引、变更记录和受影响的画像清理在同一事务内生效。写会话确认提交后才返回结果。

## 表与索引

| 数据 | 设计理由 |
| --- | --- |
| 对话、原文、实体 | 会话登记通过空间复合外键约束原文和摘要。全局追加序支持稳定尾部读取，原文重放沿用首次位置。实体的唯一键包括空间、身份类型和规范化名称。 |
| 事实与证据组 | 事实的有效区间、敏感性和槽位由关系列承载。每个完整证据组可独立支持事实，组内成员须一起保留。普通读取返回有序去重的来源；写会话可读取完整分组。 |
| 修改记录与清理 | JSONB 保存事实前后快照，有序子表保存实际设置或提前截止的触发事实、时点和原文。擦除任一快照或触发来源、删除任一触发事实时，整条受影响的审计一起清理。事实硬删同时清理索引、显著度和依赖画像。 |
| 摘要、画像、显著度 | 摘要写入时核对来源并记录最后追加位置，所有读取都限定来源齐全。画像按空间保存整份内容。访问强化单独写表，避免更新事实行和索引。高于基线的显著度必须有访问时刻，迁移时校验已有记录。 |
| 词法、向量 | 词法列由事实正文生成，用 GIN 缩小词项命中的范围。向量表按空间与模型登记维度，同一事实可保存不同维度的模型向量。适配器在读写入口校验 pgvector 的维度上限。先取满足事实条件的同模型候选，再计算精确余弦排序。输入等比缩放以保持方向和有限分数。 |

词法索引基于 NFKC、大小写折叠和中文单字及重叠双字。查询按词项任一命中，用 `ts_rank` 排序。更换分词策略时，需统一重建存量词法列并更新查询侧分词。

`FactFilter.overlap_start` 和 `overlap_end` 可按左闭右开的有效区间筛选。两端可独立指定，只排除已知边界能确定错开的事实。`exclude_slot` 排除同时匹配指定主体和谓词的事实。词法和向量检索均在评分和限额前应用筛选。

`save_facts` 按游标升序返回本次实际写入的变更，合法的幂等重放返回空元组。只有设置或提前截止才能附带触发项。新建手工事实的 `provenance` 须为空，后续可补入会话证据。修改已有来源时须提供完整证据组。纯追加接续现存的组号和位置，只写新证据关系及相应审计，有事实字段变动时一并更新事实行。擦除对话会移除含被删原文的完整证据组，以及依赖这些原文的审计记录。受影响的抽取事实失去全部证据组时撤下。手工事实及仍有完整证据组的事实保留原有效区间。发布事务将原文、事实、两类索引和变更记录一起提交。

`Memory.remember` 的审计时间 `changed_at` 取写入时钟，且不早于本次请求接收时间。每条事实的首次获知截止时间 `expired_at` 还须不早于该事实的录入时间。提前既有截止时保留原 `expired_at`。变更记录按游标分页。

显式事实筛选时点、用于筛选的默认时点、检索分数阈值及 `save_facts` 的审计时间须为有限数值。显式时点和完整时间线查询无需使用默认时点。

## 批量邻接读取

`SpaceReader.adjacent_edges` 在绑定空间的当前会话快照中读取与一批实体相邻的关系事实。主体或实体客体匹配任一输入 ID 即命中，同一事实只返回一次。查询复用现有事实表、双端索引及与 `query_facts` 相同的 `FactFilter` 和 `reference_time` 语义。

筛选和排除 `exclude_fact_ids` 后，按 `FactId` 升序返回最多 `limit` 条边。每条边包含事实 ID、主体 ID 和客体实体 ID。仅当符合条件的边数超过 `limit` 时，`truncated` 为真。空输入返回未截断的空批次。

`SpaceReader.get_relation_edges` 按候选事实 ID 投影关系端点，省略字面值事实和不可见记录。候选的有效时点由同一读快照中的候选查询处理。

`SpaceReader.outgoing_edges` 按主体批量读取一层出边，先应用 `FactFilter` 和可选的精确谓词集合，再按主体首次出现的位置、`FactId` 升序排列。`limit` 约束整批结果。两项读取均限定在会话绑定空间内。

## 真库验证

本地测试使用独立的 PostgreSQL/pgvector 实例，镜像参见 [Backend CI](../../.github/workflows/backend-ci.yml)。将 `MNEMOTRACE_NEXT_TEST_POSTGRES_DSN` 指向该实例的管理连接。测试会在该实例创建 UTF-8、C locale 的专用数据库及临时运行角色，并在结束时清理。将 `MNEMOTRACE_NEXT_REQUIRE_POSTGRES` 设为 `1` 时，配置缺失或测试跳过都会使真库作业失败。

### 普通召回质量验收

从 `backend` 使用独立 PostgreSQL 实例和[嵌入端点配置](inference.md#环境变量)运行下列命令。脚本会在该实例上创建并清理自己的数据库，只用固定合成事实和查询调用配置的 Embedder。输出文件须是新路径。

```sh
mkdir -p .artifacts/recall-quality
uv run --locked --extra inference python scripts/recall_quality.py --out .artifacts/recall-quality/run.json
```

调参集和留出集各含 235 条事实及 20 条查询，覆盖相似编号、无向量的专名、语义改写、历史区间及有干扰项的无答案查询。各策略共用同一份查询向量和实际 PostgreSQL 排名。脚本将默认策略与 `Memory.recall` 的结果逐题核对，报告记录模型与数据库版本、正确事实是否入围、Top‑1/Top‑5 命中数、无答案返回证据的次数和延迟。查看两组结果时同时检查漏在候选池外和入围后被挤出 Top‑5 的查询。脚本中的 `evaluate` 也可使用实现 `Embedder` 端口的本地模型。

本地 CPU 对照使用仓库固定的多语 MiniLM ONNX 修订 `faf4aa4`。`top_k=5` 时，默认双路 RRF 在两组的 Top‑5 命中分别为 `10/12` 和 `12/12`，无答案误召回均为 `6/8`。候选深度增至 64 或 200 未改善命中，200 在调参集还把一条正确编号挤出 Top‑5。继续使用普通召回的[候选预算](../src/mnemotrace_next/application/recall.py)。 [普通召回原始报告（gzip JSON）](recall-quality/ordinary-minilm-faf4aa4.json.gz)

MiniLM 的余弦下限降至 `0.45` 可补回余弦约为 `0.481` 的英文改写，留出集命中和两组无答案误召回持平。另一个中文改写的正确事实在原始向量排名第 203，先看分词与模型区分度，再考虑候选扩张。词法分数门槛在调参集的收益伴随留出集掉召回。当前通用向量下限为 `0.5`，词法路按词项命中入围。换嵌入模型时按本模型重跑；同题的弱命中由评测记录，普通召回允许返回相关性不足的候选供上层判断。

### 图召回质量验收

复用上述测试实例和嵌入端点配置，从 `backend` 运行以下命令。也可将本地模型作为 `Embedder` 传给 `evaluate(..., suite="graph")`。脚本按查询共享同一嵌入向量，逐题核对公开 `Memory.recall` 的开图和关图排序，并把各路候选、起点来源、每跳读边及排名写入新报告。

```sh
uv run --locked --extra inference python scripts/recall_quality.py --suite graph --out .artifacts/recall-quality/graph.json
```

中英文调参集和留出集各含 4 道一跳直答、6 道二跳补全和 2 道无答案查询。二跳题覆盖已索引与未索引的目标、历史时点及显式多起点。另用含 13 和 32 个自动起点的两个压力样例比较边预算。出边计划使用生产 SQL，先预热一次，再记录 12 次 `EXPLAIN (ANALYZE, BUFFERS)`，对照完整前沿与前两个起点。

本地 CPU 对照使用固定 MiniLM 修订 `faf4aa4`、FastEmbed `0.7.4`、ONNX Runtime `1.32.0`、tokenizers `0.23.3`、PostgreSQL `18.6` 和 pgvector `0.8.7`。同次复跑普通召回的 40 题，关闭图时的返回 ID 及目标排名逐题与原基线一致。 [MiniLM 固定修订](https://huggingface.co/Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q/tree/faf4aa4225822f3bc6376869cb1164e8e3feedd0) · [普通召回复跑原始报告（gzip JSON）](recall-quality/ordinary-rerun-minilm-faf4aa4.json.gz)

| 观察 | 调参集 | 留出集 |
| --- | --- | --- |
| 二跳 Top‑5 命中，关图 → 开图 | 0/6 → 6/6 | 0/6 → 6/6 |
| 一跳 Top‑1 命中，保留 → 跳过种子边图路分 | 4/4 → 0/4 | 4/4 → 0/4 |
| `top_k=1` 时二跳目标进入候选，默认 → 4 倍边预算 | 6/6 → 4/6 | 6/6 → 4/6 |
| `top_k=5` 时无答案返回证据，关图 → 开图 | 1/2 → 1/2 | 1/2 → 1/2 |

**保留种子边的图路 RRF 分及当前[全局边预算](../src/mnemotrace_next/application/graph_recall.py)**。只去掉种子边的图路分、保留遍历和后续边的原名次时，二跳无收益。一跳直答因竞争事实得分更高而退到第二。预算提高到 16 倍未改善两组结果。两组二跳目标在 `top_k=1` 均能进入图候选，最终都排在第一名之后。

12 条干扰出边的样例在 `top_k=5` 以默认预算读取 `[25, 1]` 条边，4 倍预算停在第一跳。2,048 条干扰出边的样例在 `top_k=1` 和 `5` 都会耗尽首跳预算，16 倍预算也到不了第二跳。`top_k=1` 时真实计划使用现有 `facts_edges_out`，向排序节点送入约 2,050 条边后返回 8 条。保留全部 32 个起点的首跳 SQL P95 为 `0.97 ms`，仅传前两个起点为 `0.73 ms`。这组本地合成样例支持沿用现有索引，也表明候选上限只约束返回量。高出度下能否到达第二跳仍取决于首跳消耗，延迟数字用于比较这次本机运行。

随文档保存的报告已按固定用例逐题核对，并从原始候选回放排序。图召回报告含 52 组查询与 `top_k` 组合，以及 8 组 PostgreSQL 执行计划和延迟样本。 [图召回原始报告（gzip JSON）](recall-quality/graph-minilm-faf4aa4.json.gz)

### 写入到召回闭环验收

从 `backend` 配置专用 PostgreSQL 测试实例及[实际补全和嵌入端点](inference.md#环境变量)，运行以下命令。脚本创建并清理自己的临时数据库，仅向配置端点发送合成消息和查询。每次执行调用实际模型，可能产生费用。

```sh
mkdir -p .artifacts/memory-closure
uv run --locked --extra inference python scripts/memory_closure.py --out .artifacts/memory-closure/run.json
```

每轮在两个空空间中分别验证跨会话状态变更和二跳关系召回。状态变更检查生效前、恰好生效和当前三次召回。二跳用例固定查询与 `top_k=5`，要求输出中的目标边只出现在二跳结果。三轮中至少两轮的两个用例都通过，且全程没有事实区间、来源或可见性契约违规，验收才通过。使用新的输出路径保留全部轮次，报告包含显式模型配置、代码版本、合成原文、提交的事实变更、查询条件和排序后的证据。

真库回归使用固定模型输出覆盖同一闭环，以及擦除、写入竞争和提交确认丢失。实际模型验收由上述命令单独执行。

2026-11-21 的闭环验收使用 DeepSeek `deepseek-chat`、SiliconFlow `BAAI/bge-m3`、PostgreSQL 18.6 和 pgvector 0.8.7。三个完整轮次均通过，报告未发现契约违规。报告中的嵌入修订是本次部署的标识，服务端权重修订未确认。

### 检查命令

```sh
uv run --locked pytest -m postgres
uv run --locked pytest -m postgres -k vector_plan -s
```

向量执行计划测试使用两个空间的合成数据。它通过 `EXPLAIN (ANALYZE, BUFFERS)` 对比全空间 1200 条与标签筛选后 12 条的评分输入，并校验 top-k。执行耗时用来观察当前实例的表现。
