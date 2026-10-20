# 契约：磁盘格式（v1，`schema_version = 1`）

## 目录布局（每个命名空间一个）

```text
<data_path>/<namespace>/
├── manifest.json      # 最后以原子方式写入（tmp + os.replace），是提交点
├── episodes.jsonl
├── facts.jsonl
├── entities.jsonl
├── relations.jsonl
├── working.jsonl
├── conflicts.jsonl
├── .lock              # 建议式单写者锁（fcntl.flock）
└── lancedb/           # 仅 backend = "lancedb" 时存在
```

## 写入协议（崩溃安全 — D2）

1. 获取 `.lock`；若已被持有则立即失败（单写者 — D5）。
2. 追加 / 重写各 `*.jsonl`；增量保存采用追加，v1 压缩整理允许全量重写。
3. 对每个文件执行 `flush()` + `os.fsync()`。
4. 写入 `manifest.json.tmp` 并 fsync，再通过 `os.replace()` 替换 `manifest.json`，此处为**原子提交点**。
5. 释放锁。

## 读取协议（安全且可恢复 — D2/D3）

1. 没有 `manifest.json` → 视为**空存储**，不报错（US1 验收场景 3）。
2. 解析 manifest；`schema_version` 大于支持版本时抛出 `IncompatibleStoreError`（FR-004）。
3. `embedder_id` 或 `embedding_dim` 与配置的 embedder 不同，抛出 `EmbedderMismatchError` 或 `DimensionMismatchError`，而非返回错误向量空间的近邻（US2 验收场景 3）。
4. 对每个 `*.jsonl`，严格按 manifest 声明的已提交前缀流式读取；前缀**内部**缺失记录或 JSON 格式错误，抛出 `StoreFormatError`；manifest 数量**之后**的异常字节 / 记录视为未提交的撕裂尾部并忽略（FR-005）。
5. 通过显式字段映射重建 dataclass，**不使用 `eval` 或 `pickle`**（FR-003）。

## 保证

- **安全**：打开任何文件都不会执行嵌入代码（SC-002）。
- **持久**：已提交前缀可在写入中崩溃后保留（SC-003）。
- **向量空间安全**：使用不同嵌入模型 id 或维度写入的存储会明确报错。
- **不静默丢失**：manifest 已提交前缀内出现损坏时明确失败，不加载部分记忆冒充完整结果。
- **版本化**：不兼容存储明确失败，绝不静默处理（FR-004）。
- **无损**：所有 dataclass 字段往返保留（宪章 IV；data-model.md 中的 INV-1..4）。
