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

`Memory.remember` 在抽取后生成向量前、读取候选时以及再次请求模型前复核对话登记和擦除标记。在空间写锁内用原条件重查候选。仅候选移除、区间确定错开或证据组与敏感标记更新时，直接用锁内结果计算写入。需要重新请求模型时，释放锁并只重判新增或变化的比较对，随后重新复核一次。提交使用写会话读到的最新完整证据组。画像引用事实的有效期、标签或敏感性发生变化时，在同一事务内清理画像。仅追加证据组时保留画像。原文、缺失实体、事实、证据组、词法及向量索引、变更记录和受影响的画像清理在同一事务内生效。写会话确认提交后才返回结果。

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

`save_facts` 按游标升序返回本次实际写入的变更，未改变事实或证据组时返回空元组。修改已有来源时须提供完整证据组。擦除对话会移除含被删原文的完整组，只有失去全部证据组的受影响事实才会被撤下。事实仍有证据组时，有效区间保持原值。发布事务将原文、事实、两类索引和变更记录一起提交。

## 批量邻接读取

`SpaceReader.adjacent_edges` 在绑定空间的当前会话快照中读取与一批实体相邻的关系事实。主体或实体客体匹配任一输入 ID 即命中，同一事实只返回一次。查询复用现有事实表、双端索引及与 `query_facts` 相同的 `FactFilter` 和 `reference_time` 语义。

筛选和排除 `exclude_fact_ids` 后，按 `FactId` 升序返回最多 `limit` 条边。每条边包含事实 ID、主体 ID 和客体实体 ID。仅当符合条件的边数超过 `limit` 时，`truncated` 为真。空输入返回未截断的空批次。

## 真库验证

本地测试使用独立的 PostgreSQL/pgvector 实例，镜像参见 [Backend CI](../../.github/workflows/backend-ci.yml)。将 `MNEMOTRACE_NEXT_TEST_POSTGRES_DSN` 指向该实例的管理连接。测试会在该实例创建 UTF-8、C locale 的专用数据库及临时运行角色，并在结束时清理。将 `MNEMOTRACE_NEXT_REQUIRE_POSTGRES` 设为 `1` 时，配置缺失或测试跳过都会使真库作业失败。

```sh
uv run --locked pytest -m postgres
uv run --locked pytest -m postgres -k vector_plan -s
```

向量执行计划测试使用两个空间的合成数据。它通过 `EXPLAIN (ANALYZE, BUFFERS)` 对比全空间 1200 条与标签筛选后 12 条的评分输入，并校验 top-k。执行耗时用来观察当前实例的表现。
