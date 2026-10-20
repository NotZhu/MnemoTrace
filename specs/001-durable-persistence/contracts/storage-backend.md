# 契约：持久化存储后端

持久化后端不引入新 API，而是实现 [`mnemotrace/store/base.py`](../../../mnemotrace/store/base.py)
中已有的抽象基类。本契约规定持久化实现在内存参照实现之外**必须**履行的义务。

## VectorStore（LanceDBVectorStore）

| 方法 | 持久化义务 |
|---|---|
| `upsert(key, vector, payload)` | 持久保存 `(key, vector, payload)`，在进程重启后保留；对 `key` 幂等 |
| `search(vector, top_k, where)` | 按相似度返回 top_k；`where` 谓词语义必须与内存存储一致，v1 可在检索后过滤 |
| `get(key)` | 返回持久化 payload 或 `None` |
| `delete(key)` | 持久删除，用于衰减 / 遗忘 |
| `values()` | 遍历所有 payload，用于保存与整合；必须流式处理，不得将整个存储一次性物化为一个数据块 |

**一致性门禁（SC-005 / FR-009）：** 对相同数据与查询，重新加载的 LanceDB `search` top-k 必须与内存存储一致，
只允许已记录的容差（仅限同分排序与浮点误差）。

**延迟导入门禁（FR-007）：** `import lancedb` 发生在 `lancedb_store.py` **内部**，
不得在 `mnemotrace/` 核心导入时执行。`tests/test_zero_setup_default.py` 断言默认导入图中不含 lancedb。

## DocStore / GraphStore（v1 持久化形式）

v1 通过**快照序列化器**（`persist.py`）持久保存 episode（`DocStore`）以及 entity + relation（`GraphStore`），
而非使用实时嵌入式数据库；内存字典存储仍驻留 RAM，以 JSONL 保存 / 加载。
原生嵌入式图后端（如 Kuzu）属于后续独立规格。

**契约：** 一次 `save → open` 周期后，`DocStore.values()`、`GraphStore.relations()` 和
`GraphStore.neighbors(entity_id, as_of=…, direction=…)` 的结果完全一致（INV-2）。

## 后端选择（配置）

`mnemotrace/config.py` 提供 `storage = "memory" | "durable" | "lancedb"` 以及 `data_path`：

- `"memory"`：**默认**，行为不变（宪章 II）。
- `"durable"`：所有存储使用 JSONL 快照（US1）。
- `"lancedb"`：episode / 图谱使用 JSONL，向量存储使用 LanceDB（US2）。

公开接口不变：调用者继续使用 `Memory.open(path)` / `mem.save()`，只有磁盘表示与可选向量后端不同。
