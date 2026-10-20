# Phase 0 — 研究与决策：持久化存储

所有决策遵循宪章：默认仅用标准库（II）、位于现有接口之后（III）、保留每个字段（IV），
且**不声称任何**性能数字（V；这些数字由评测任务测量）。

## D1. 磁盘格式 → JSON Lines（每种实体一个文件）+ `manifest.json`

- **决策：** 每个集合序列化至各自便于追加的 `.jsonl`，每行一个 JSON 对象；`manifest.json` 头部元数据记录 schema 版本、embedder 身份和已提交记录数量。
- **理由：** 纯标准库（`json`）让持久化路径保持零依赖（II）；便于追加，有助于实现崩溃安全（FR-005）；加载安全，`json.loads` 不会执行代码（FR-003）；人可直接检查，便于追溯来源和调试；可逐条流式处理，无需像 pickle 那样一次加载整个数据块。现有存储已经持有普通 dataclass，因此映射直接明确。
- **备选：** *pickle*（现状）不安全、脆弱，每次保存都重写整个存储；*SQLite* 可靠，但增加 schema / 迁移维护面，嵌套字段（嵌入、来源）仍需 JSON，等需要二级索引时再考虑，不用于本次快照；*LMDB* 有 C 依赖，破坏标准库默认方案；*Parquet* 需要 pyarrow，对文档 / 图谱记录使用列式存储过重。

## D2. 崩溃安全 → 追加 + fsync，最后原子写入 manifest，恢复撕裂尾部

- **决策：** 每条记录追加为一行，执行 `flush()` + `os.fsync()`；**最后**通过 `manifest.json.tmp` + `os.replace()` 原子写入 `manifest.json`。加载时按 manifest 声明的已提交前缀解析各 `.jsonl`；前缀内部缺失记录或格式错误必须明确失败，manifest 数量之后的截断 / 损坏记录作为未提交的撕裂尾部忽略。这样崩溃后可恢复**最后已提交前缀**，不会静默丢弃已提交记忆（FR-005、SC-003）。
- **理由：** 原子重命名 + fsync 是不依赖数据库实现持久性的常见做法；`os.replace` 在 POSIX 和 Windows 上均为原子操作。
- **备选：** *每次保存先写完整文件再重命名*会失去 O(1) 追加，每次重写全部内容，与 pickle 相同；对追加日志采用 *WAL / journal* 过重。

## D3. 安全加载与版本门禁 → manifest `schema_version`，显式重建

- **决策：** `manifest.json` 携带 `schema_version`（int）和 `mnemotrace_version`。若 `schema_version` 超过读取器支持的版本，明确抛出 `IncompatibleStoreError`，绝不部分加载或错误加载（FR-004）。记录通过**显式字段映射**重建为 dataclass，绝不使用 `eval` 或向 `__dict__` 注入。
- **理由：** 满足 FR-003 + FR-004，明确拒绝不兼容的较新格式存储。
- **备选：** 无版本格式会静默误加载，拒绝；pickle 协议版本仍无法解决安全问题。

## D4. 首个持久向量后端 → LanceDB

- **决策：** 使用 `LanceDBVectorStore`（嵌入式、磁盘存储、原生 ANN）作为首个真实 `VectorStore`，由配置选择，`lancedb` 在模块内部延迟导入。
- **理由：** 符合章程中的既定路径；嵌入式、无需服务器，适合自托管；内存映射可扩展到 RAM 之外（SC-006）；采用 Apache-2.0 许可。
- **备选：** *Chroma* 依赖树更重、更偏向服务式运行（它是评测中 Mem0 的默认后端，不是我们的默认）；*sqlite-vec* 有潜力，但较新、验证较少；*Qdrant/pgvector* 需要服务器，留作同一抽象基类之后的后续 extras。

## D5. 并发 → 单写者建议锁（v1）

- **决策：** 每个命名空间目录使用建议锁文件（通过 `fcntl.flock` 操作 `.lock`）；第二个写者立即明确报错。**v1 不支持多写者**，需在文档中说明。
- **理由：** 与 `MemoryService` 按命名空间管理 LRU 归属的方式一致（`mnemotrace/service.py`），无需数据库即可避免交错写入损坏。
- **备选：** portalocker 增加依赖；MVCC 过重。

## D6. 格式与向量后端保持正交

- **决策：** 事实嵌入保存在 `facts.jsonl`，因此持久快照独立于后端。使用内存后端加载时，嵌入重新填入字典；使用 LanceDB 时，通过 upsert 写入 LanceDB 表。因此，按规格要求，US1（格式）可在没有 US2（LanceDB）的情况下测试。
- **理由：** 两个用户故事可独立交付和测试；快照可跨向量后端移植。

**所有 NEEDS CLARIFICATION 均已解决，没有未决问题阻止 Phase 1。**
