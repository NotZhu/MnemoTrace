# 数据生命周期指南

本指南供接入方和运维人员删除事实、擦除会话、清空空间或删除空间。四类操作的范围和保留内容不同，[核对操作结果](#核对操作结果)需要用操作后的新请求。

[HTTP 端点索引](../reference/http-api.md#端点索引) · [擦除与一致性边界](../architecture/memory.md#擦除与一致性边界) · [部署与备份](../../deploy/README.md#停机备份与升级)

**这些操作不可撤销。** 执行前用同一账号确认空间和资源 ID、选择最小范围；需要保留的数据先导出或备份；敏感事实先用 `include_sensitive=true` 查询列表或召回确认。不要用删除前缓存或已读取的召回结果核对删除效果。

## 选择操作

| 需求 | 操作 | 影响 |
| --- | --- | --- |
| 移除一条抽取结果，保留对话 | `DELETE /v1/spaces/{space_id}/facts/{fact_id}` | 删除事实、关联向量、显著度、证据引用和相关审计；依赖该事实的画像条目退出可见结果。保留会话、原文和原写入任务。 |
| 移除一段对话及其原文 | `DELETE /v1/spaces/{space_id}/sessions/{session_id}` | 删除会话、原文、写入任务和会话摘要；引用被擦除原文的证据组整组撤下，引用这些原文的审计记录清理。仍有独立完整证据组的事实保留。 |
| 清空空间，继续使用原空间 | `POST /v1/spaces/{space_id}/purge` | 清空会话、原文、事实、搜索数据、审计、任务和派生数据；保留空间 ID、名称、配置和嵌入空间登记。 |
| 移除空间及其数据 | `DELETE /v1/spaces/{space_id}` | 删除空间内容、空间登记和嵌入空间登记；账号的 API 密钥仍可用于其他空间。 |

## 共同约定

- 四类操作都是**同步请求**；成功返回 `204 No Content` 即已提交，没有任务 ID，也不需要轮询。
- 使用 Bearer 认证并发送空正文。清空空间还须发送 `Idempotency-Key`。
- 会话擦除、清空空间和删除空间使用维护语句时限，默认 60 秒（`MNEMOTRACE_DB_MAINTENANCE_TIMEOUT_SECONDS`）；删除事实使用普通语句时限。
- `404` 表示目标不存在或对当前账号不可见；删除成功后的重放也可能返回 `404`。
- 删除只作用于 MnemoTrace 当前数据库；调用方保存的响应、备份和外部日志按各自流程处理。

请求示例沿用 [HTTP API 的配置请求](../reference/http-api.md#配置请求) 中的 `api()` 和 `BASE_URL`；`SPACE_ID`、`FACT_ID`、`SESSION_ID` 取自 API 返回的 ID。

## 删除事实

**适用：** 纠正一条抽取错误或移除不再需要的事实，同时保留对话。

```bash
api -X DELETE "$BASE_URL/v1/spaces/$SPACE_ID/facts/$FACT_ID"
```

**注意：** 不删除原文；要移除整段对话用擦除会话。目标带敏感标记时，先用 `include_sensitive=true` 查询列表或召回确认可见范围。

## 擦除会话

**适用：** 移除一段对话及其原文，并撤下依赖这些原文的证据。

```bash
api -X DELETE "$BASE_URL/v1/spaces/$SPACE_ID/sessions/$SESSION_ID"
```

**注意：** 证据组引用被擦除原文时整组撤下；事实失去全部完整证据组才删除，有其他完整证据组时保留。删除原文时推进擦除代际，阻止旧快照写回。擦除后的 `as_of` 查询遵循新数据范围，依赖已撤下事实的审计和画像条目退出可见结果。 [证据组和审计回归](../../tests/postgres/test_erasure.py)

## 清空空间

**适用：** 重置空间内容，但保留空间本身、配置和嵌入空间登记。

```bash
PURGE_KEY=$(openssl rand -hex 16)  # 保存这个键；重试时复用
api -X POST "$BASE_URL/v1/spaces/$SPACE_ID/purge" -H "Idempotency-Key: $PURGE_KEY"
```

**重放：** 同一空间、同一 `Idempotency-Key` 只执行一次；第一次已提交但响应丢失时，重放返回 `204`，不会清掉第一次清空之后的新写入。独立清空用新键。

## 删除空间

**适用：** 不再使用该空间，移除其内容和登记。

```bash
api -X DELETE "$BASE_URL/v1/spaces/$SPACE_ID"
```

**注意：** 不可恢复；需要保留的数据先备份。

## 超时、重试与并发

- **清空空间的结果不明时，用原键重试。** 同键只执行一次；重放语义见[清空空间](#清空空间)。 [同键重放测试](../../tests/postgres/test_operations.py)
- **删除请求的结果不明时，先核对原目标。** 使用同一账号、空间 ID 和资源 ID 发起详情查询；目标已消失时，重放删除可能返回 `404`。
- **后台任务与已开始的读取有不同的完成边界。** 擦除会移除目标会话的写入任务，清空会移除目标空间中的任务。写入和派生数据在发布前会校验会话、租约或擦除代际；核对结果时应在操作完成后发起新请求。 [进行中写入测试](../../tests/postgres/test_operations.py) · [召回快照测试](../../tests/postgres/test_recall.py)

`400`、`404`、`413`、`503` 等错误码见 [HTTP 错误响应](../reference/http-api.md#错误响应)。

## 核对操作结果

| 操作 | 完成后发起的新读取 |
| --- | --- |
| 删除事实 | `GET /v1/spaces/{space_id}/facts/{fact_id}` 返回 `404 fact_not_found`。目标原先带敏感标记时，用 `include_sensitive=true` 查询列表或召回，按相同的可见性范围核对。原文仍可通过 `GET /v1/spaces/{space_id}/messages/{message_id}` 访问。 |
| 擦除会话 | 查询原会话、已知原文和写入任务 ID，分别返回 `404`。若事实由其他会话独立支持，读取事实检查保留下来的 `source_message_ids`。 |
| 清空空间 | 空间详情仍可读。如果清空后还没有新写入，会话、事实和任务列表的 `items` 为空，空间画像的 `profile` 为 `null`。用 `state=all&include_sensitive=true` 查询事实列表可覆盖历史及敏感事实。 |
| 删除空间 | `GET /v1/spaces/{space_id}` 返回 `404 space_not_found`。在同一账号下查询其他空间以核对操作范围。 |

`404` 表示资源不存在或对此账号不可见。用操作前确认过的账号和资源 ID 核对结果，保留 `X-Request-ID` 供排查。召回结果会受时点、筛选条件和预算影响，按资源 ID 查询适合验证删除的具体目标。

## 相关文档

- [HTTP 端点索引](../reference/http-api.md#端点索引)
- [HTTP 错误响应](../reference/http-api.md#错误响应)
- [擦除与一致性边界](../architecture/memory.md#擦除与一致性边界)
- [部署与备份](../../deploy/README.md#停机备份与升级)
