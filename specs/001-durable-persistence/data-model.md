# Phase 1 — 数据模型：持久化记录与 Manifest

实体的权威定义位于 [`mnemotrace/types.py`](../../mnemotrace/types.py)；本文规定它们的**持久化方式**，
以及序列化器必须保持的**不变量**。源 dataclass 是字段的唯一事实来源；序列化器保存**完整**字段集，
确保重新加载无损（宪章 IV）。

## 持久化集合（每个集合一个 `.jsonl`）

| 文件 | Dataclass | 往返过程中必须保留的字段 |
|---|---|---|
| `episodes.jsonl` | `Episode` | id, content, user_id, session_id, speaker, **event_time**, **ingested_at**, embedding, consolidated, summary, summary_embedding, metadata |
| `facts.jsonl` | `Fact` | id, subject, predicate, object, text, display, user_id, **embedding**, salience, confidence, source, category, sensitive, **valid_at, invalid_at, created_at, expired_at, supersedes, provenance**, last_access, access_count |
| `entities.jsonl` | `Entity` | id, name, type, user_id, embedding, aliases |
| `relations.jsonl` | `Relation` | id, subject_id, predicate, object_id, fact_id, **valid_at, invalid_at** |
| `working.jsonl` | `WorkingMemory` | id, content, user_id, session_id, kind, event_time, created_at, expires_at, consumed, embedding, metadata |
| `conflicts.jsonl` | `Conflict` | id, older, newer, text_older, text_newer, user_id, reason, status, detected_at |

> 持久化范围是 **`Memory` 当前持有的每个集合**，即现有 pickle 字典中的全部集合。
> `WorkingMemory` 和 `Conflict` 也必须保存，让会话状态与冲突**审计轨迹**在重启后保留；丢弃它们就是静默丢失（宪章 IV）。

## 记录序列化规则

- 每行一个紧凑 JSON 对象，UTF-8 编码，以 `\n` 结束。
- `float` 时间字段写为 JSON 数字，单位为 epoch 秒；在本项目使用的数量级下不损失精度。
- `Optional[...] = None` 字段显式写为 `null`，避免字段缺失与默认值之间产生歧义。
- `embedding` / `summary_embedding` 写为浮点数 JSON 数组或 `null`。
- `metadata` 为任意 JSON 对象，原本已声明为 dict 类型。
- 重建时将字段**显式**映射到 dataclass 构造器：忽略较新次版本中的未知字段；缺失的可选字段使用 dataclass 默认值，在同一 `schema_version` 内容忍较新版本字段。

## 双时间与来源不变量（发布门禁）

- **INV-1**：每个持久化 `Fact` 重新加载后的 `valid_at / invalid_at / created_at / expired_at / supersedes / provenance` **完全一致**；失效事实仍失效，supersedes 链完整，加载时不硬删除。
- **INV-2**：每个 `Relation` 重新加载后的 `valid_at / invalid_at` 完全一致（图谱的双时间边），使 `neighbors(as_of=…)` 在重启后返回相同的有效集合。
- **INV-3**：来源 episode-id 列表重新加载后完全一致，仍能回答“来自哪里”。
- **INV-4**：成功加载后，各文件已提交行数与 manifest 的 `counts` 相等。

## `manifest.json` schema（新增）

```json
{
  "schema_version": 1,
  "mnemotrace_version": "0.x.y",
  "embedder_id": "bge-small-en-v1.5 | hashing | ...",
  "embedding_dim": 384,
  "created_at": 1750000000.0,
  "updated_at": 1750000000.0,
  "backend": "durable | lancedb",
  "counts": {"episodes": 0, "facts": 0, "entities": 0, "relations": 0, "working": 0, "conflicts": 0}
}
```

- `embedder_id` + `embedding_dim` 在打开时检查模型 id 与维度是否匹配（US2-AC3），避免维度相同但模型不同的嵌入静默复用旧近邻。
- `schema_version` 控制兼容性（D3）；`backend` 记录写入快照的向量存储后端。
