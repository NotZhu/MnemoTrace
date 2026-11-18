# PostgreSQL 存储适配器

新后端使用 PostgreSQL 18 和 pgvector。迁移把业务表建在 `mnemotrace_next`，扩展位于 `public`。在包含 pgvector 的空数据库上，用具备建 schema 和安装扩展权限的迁移账号执行命令。托管 PostgreSQL 可由 DBA 预装扩展。

在新后端目录设置 `MNEMOTRACE_NEXT_MIGRATION_DSN` 后运行以下命令。

```sh
uv run --locked python -m mnemotrace_next.adapters.postgres.migrate status
uv run --locked python -m mnemotrace_next.adapters.postgres.migrate up
```

迁移锁保护待执行版本的检查和应用。迁移 SQL 按文件执行，使用无绑定参数的文本并禁用预编译。每次 `up` 把待执行的 SQL 和账本写入一起提交，失败时按迁移文件和 SQLSTATE 诊断。后续变更追加新编号的 SQL，已应用文件的校验和用于识别改写。新列、新表等扩展沿用现行查询的显式列清单。

## 运行账号与会话

数据库管理员为服务配置独立登录角色。以下授权在迁移后执行，其中 `mnemotrace_next_runtime` 是已创建的运行角色。

```sql
GRANT USAGE ON SCHEMA mnemotrace_next TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, UPDATE (purge_generation) ON mnemotrace_next.spaces
    TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, DELETE ON
    mnemotrace_next.messages, mnemotrace_next.fact_provenance,
    mnemotrace_next.fact_supersedes, mnemotrace_next.fact_changes TO mnemotrace_next_runtime;
GRANT SELECT, INSERT ON mnemotrace_next.embedding_spaces TO mnemotrace_next_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    mnemotrace_next.entities, mnemotrace_next.facts, mnemotrace_next.digests,
    mnemotrace_next.profiles, mnemotrace_next.fact_salience,
    mnemotrace_next.fact_vectors TO mnemotrace_next_runtime;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA mnemotrace_next TO mnemotrace_next_runtime;
```

服务进程共享并管理 `psycopg_pool.AsyncConnectionPool`。服务端按已认证账号构造 `PostgresStorage(pool, owner_id=owner_id)`。在 Windows 上，运行异步池的进程可用 `asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)`。会话入口用可信账号和空间 ID 联合检查授权，会话里的 SQL 始终使用绑定空间。受限读取的可见性由服务端设置。

只读事务从授权查询取得一致快照。写事务持有空间行锁，成功退出时提交，块内异常或取消时回滚。若调用方捕获了 SQL 错误但写事务仍处于失败状态，退出时抛出 `RuntimeError` 并回滚。数据操作的部署权限授予运行角色，租户间的访问由适配器显式授权和空间复合键限定。

应用在会话外生成向量和派生内容，发布前在写会话中复核 `get_erasure_token()` 返回的擦除标记及依赖。擦除标记使用 `spaces.purge_generation`，在空间清空或实际删除会话原文的同一事务内推进。会话删除会使同空间尚未发布的派生结果失效，即使原文随后原样重放。清空空间会删除向量和其他记忆数据，并保留模型维度登记。已开始的只读事务仍按其快照返回数据。

## 表与索引

| 数据 | 设计理由 |
| --- | --- |
| 原文、实体 | 全局追加序和空间内会话索引支持稳定尾部读取，消息重放沿用首次位置。实体的唯一键使用领域的规范化名称。 |
| 事实、来源、前驱 | 关系列承载有效时间、敏感性和槽位。时间保持 epoch 秒精度，ID 按 `C` 排序。列表和槽位按收录时刻分页，边索引直接覆盖两个端点。来源和前驱通过空间复合外键关联，有序关系用于返回完整快照。 |
| 修改记录与清理 | JSONB 保存事实前后快照，按事实和变更游标读取。来源 ID 的并集支持删除会话时清理幸存事实的历史。事实硬删级联索引、状态及当前前驱关系。画像在调用方处理事实依赖的同一事务清理。 |
| 摘要、画像、显著度 | 摘要写入时核对来源并记录最后追加位置，所有读取都限定来源齐全。画像按空间保存整份内容。访问强化单独写表，避免更新事实行和索引。高于基线的显著度必须有访问时刻，迁移时校验已有记录。 |
| 词法、向量 | 词法列由事实正文生成，用 GIN 缩小词项命中的范围。向量表按空间与模型登记维度，同一事实可保存不同维度的模型向量。适配器在读写入口校验 pgvector 的维度上限。先取满足事实条件的同模型候选，再计算精确余弦排序。输入等比缩放以保持方向和有限分数。 |

词法索引基于 NFKC、大小写折叠和中文单字及重叠双字。查询按词项任一命中，用 `ts_rank` 排序。更换分词策略时，需统一重建存量词法列并更新查询侧分词。

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
