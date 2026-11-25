# HTTP API v1 接入指南

MnemoTrace 以空间组织跨会话记忆。应用把消息写入某个会话，等待后台任务完成，再从同一空间召回事实和原文证据。部署和密钥签发见[快速开始](../../README.zh-CN.md#快速开始)，TypeScript 示例见 [SDK 指南](../../clients/typescript/README.md)。

## 地址、认证与契约

- 本地默认地址为 `http://127.0.0.1:8100`。业务接口使用 `/v1`，当前部署的路径、字段、类型与取值范围以 `GET /openapi.json` 为准。
- 业务请求带上 `Authorization: Bearer <API 密钥>`。服务端从密钥识别账号并校验空间归属。路径中使用 API 返回的空间、会话、任务、事实和消息 ID。
- 带 JSON 正文的接口使用 `Content-Type: application/json`。`GET /livez`、`GET /readyz` 和 `GET /openapi.json` 可用于检查服务。 [部署验收](../../deploy/README.md#部署验收)

## 写入并召回一条记忆

准备 Bash、`curl` 和 Python 3（命令为 `python3`），把已签发的 `token` 设为环境变量 `MNEMOTRACE_API_KEY`。如果服务地址不同，设置 `MNEMOTRACE_BASE_URL`。以下步骤在同一个 Bash 会话中运行。请求失败时，处理 `curl` 报出的错误后再继续。

### 配置请求

```bash
: "${MNEMOTRACE_API_KEY:?请先设置 MNEMOTRACE_API_KEY}"
BASE_URL="${MNEMOTRACE_BASE_URL:-http://127.0.0.1:8100}"
BASE_URL="${BASE_URL%/}"

api() {
  curl -fsS -H "Authorization: Bearer $MNEMOTRACE_API_KEY" "$@"
}

api_field() {
  local field="$1" response
  shift
  response=$(api "$@") || return
  python3 -X utf8 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field" <<< "$response"
}
```

### 选择空间

**接入已有空间。** 将 `SPACE_ID` 设为 `admin bootstrap` 返回的 `space_id`，或可访问的已有空间 ID。确认下方请求成功后，继续创建会话。

```bash
SPACE_ID='替换为已有空间 ID'
api "$BASE_URL/v1/spaces/$SPACE_ID"
```

**运行独立演示。** 创建一个空间来写入本篇的合成消息。创建成功后返回 `201 Created`，`Location` 指向新空间。

```bash
SPACE_ID=$(api_field space_id -X POST "$BASE_URL/v1/spaces" \
  -H 'Content-Type: application/json' \
  -d '{"name":"http-v1-demo"}')
```

### 创建会话

```bash
SESSION_ID=$(api_field session_id -X POST "$BASE_URL/v1/spaces/$SPACE_ID/sessions")
```

创建会话时发送空正文，成功返回 `201 Created`，`Location` 指向该会话。

### 提交写入

每次提交一个按原始发言顺序排列的消息批次。`messages` 包含 1 到 64 条消息，`role` 可为 `user` 或 `assistant`。已知发言时刻时，可传入带时区的 RFC 3339 `event_time`。在已有空间接入时，把示例消息换成实际消息。

```bash
WRITE_KEY=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
WRITE_BODY='{"messages":[{"role":"user","content":"我工作日坐地铁上班。"}]}'

OPERATION_ID=$(api_field operation_id -X POST "$BASE_URL/v1/spaces/$SPACE_ID/sessions/$SESSION_ID/remember" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $WRITE_KEY" \
  -d "$WRITE_BODY")
```

`202 Accepted` 表示服务已持久接收任务。响应正文包含 `operation_id` 和当前 `status`，`Location` 指向任务详情。原文和事实变更在后台任务成功时一并提交。

### 等待任务完成

```bash
api "$BASE_URL/v1/spaces/$SPACE_ID/operations/$OPERATION_ID"
```

收到 `queued` 或 `running` 时，在客户端的等待预算内再次查询同一任务。响应包含 `next_retry_at` 时，可等到该时刻再查。收到 `succeeded` 后继续召回。收到 `failed` 时，根据公开错误排查，再决定是否重新提交。

| `status` | 含义 |
| --- | --- |
| `queued` | 等待处理或后台重试。 |
| `running` | 后台正在处理。 |
| `succeeded` | 原文已提交，响应包含 `message_ids` 和 `fact_change_count`。事实变更数可以为 `0`。 |
| `failed` | 任务已结束。从 `error.code` 和可用的 `error.stage` 定位失败。 |

`error.code` 为 `extraction_input_too_large` 时，本批原文未提交。请把消息拆成更小且语义完整的批次，为每个新批次使用新的幂等键。 [模型上下文配置](model-configuration.md#选择补全的-json-输出模式)

### 召回并查看原文

在同一空间发起召回。接入已有空间时，将示例问题换成与实际消息相关的问题。证据正文在 `evidence[].text`，原文 ID 在 `evidence[].source_message_ids`。以下命令打印召回结果，并读取第一条带来源的证据所引用的首条原文。

```bash
RECALL_RESPONSE=$(api -X POST "$BASE_URL/v1/spaces/$SPACE_ID/recall" \
  -H 'Content-Type: application/json' \
  -d '{"query":"我工作日怎么上班？","top_k":3,"max_evidence_chars":600}') && {
  printf '%s\n' "$RECALL_RESPONSE"

  MESSAGE_ID=$(python3 -X utf8 -c '
import json, sys
evidence = json.load(sys.stdin)["evidence"]
print(next((item["source_message_ids"][0] for item in evidence if item["source_message_ids"]), ""))
' <<< "$RECALL_RESPONSE")

  if [[ -n "$MESSAGE_ID" ]]; then
    api "$BASE_URL/v1/spaces/$SPACE_ID/messages/$MESSAGE_ID"
  fi
}
```

若 `evidence` 为空，检查 `fact_change_count`、召回条件和响应中的 `degraded_reason`。演示结束后，可按[数据生命周期指南](../guides/data-lifecycle.md#选择操作)删除本次创建的专用空间。

## 幂等重试与恢复

`remember` 的 `Idempotency-Key` 使用 1 到 128 个可见 ASCII 字符，按空间生效。客户端应为每批逻辑写入保留原键、空间 ID、会话 ID 和消息。同一空间用相同键重放原会话与原消息，会返回原任务及其当前状态。用同一键换会话或消息，则返回 `409 idempotency_conflict`。

- **提交结果不明**。连接中断、提交超时或收到 `503 commit_outcome_unknown` 时，用原请求和原幂等键重试。
- **已经取得任务 ID**。客户端等待超时后，继续查询同一任务。客户端停止轮询后，任务仍可由后台处理。
- **任务最终失败**。先处理失败原因。只有决定发起新的写入时才用新键提交，请求重放会返回原失败任务。

## 召回选项

| 字段 | 用法 |
| --- | --- |
| `as_of` | 在当前已提交的数据中按业务有效时间筛选，使用带时区的 RFC 3339 时间。省略时查询当前有效的事实。 |
| `filters` | 按 `source` 和 `labels` 筛选。指定多个标签时，事实具有其中任一标签即可匹配。 |
| `top_k`、`max_evidence_chars` | 分别限制证据条数和证据正文的总字符数。超过字符预算时，从第一条放不下的证据开始省略，响应以 `truncated` 标明裁剪。 |
| `graph` | 按需启用实体关系扩展。 |
| `rerank` | 省略时使用已配置的重排器。设为 `false` 可跳过，设为 `true` 要求服务端已配置重排器。 |
| `prefer_recent` | 默认为 `false`。设为 `true` 时为近期生效的事实小幅加分，只对省略 `as_of` 的查询生效。 |
| `track_usage` | 默认为 `true`。对启用了显著度衰减的空间，可设为 `false` 关闭此次召回的使用记录，适用于评测和只读验证。 |
| `include_sensitive` | 默认为 `false`。设为 `true` 可纳入带敏感标记的事实。 |

`rerank_applied` 表示本次是否实际重排。成功响应的 `degraded_reason` 有值时，当前结果来自降级的检索路径。空的 `evidence` 表示此次请求未返回证据。

## 列表、分页和审计

列表接口返回 `items` 和 `next_cursor`。首页可传 `limit`。获取下一页时保留原查询的资源范围和筛选条件，把响应中的 `next_cursor` 原样作为 `cursor` 传入。`next_cursor` 为 `null` 时已到末页。

| 目的 | 接口 |
| --- | --- |
| 列出空间或会话 | `GET /v1/spaces`、`GET /v1/spaces/{space_id}/sessions` |
| 查询任务及其事实变更 | `GET /v1/spaces/{space_id}/operations`、`GET /v1/spaces/{space_id}/operations/{operation_id}/fact-changes` |
| 查询事实及其有效期变更 | `GET /v1/spaces/{space_id}/facts`、`GET /v1/spaces/{space_id}/facts/{fact_id}/changes` |
| 读取一个会话的原文 | `GET /v1/spaces/{space_id}/sessions/{session_id}/messages` |
| 读取或请求刷新空间画像 | `GET /v1/spaces/{space_id}/profile`、`POST /v1/spaces/{space_id}/profile/refresh` |

删除事实、擦除会话、清空空间或删除空间时，按[数据生命周期指南](../guides/data-lifecycle.md#选择操作)选择接口、判断重试方式并核对结果。

## 错误响应

HTTP 错误使用 `application/problem+json`。客户端按 `status` 和 `code` 处理，字段校验失败时可查看 `errors` 中的位置和错误码。排查时记录正文的 `request_id` 或响应头 `X-Request-ID`。调试单个请求时，可直接用 `curl -sS -i` 带上相同的请求头和正文，查看响应头和错误正文。

| HTTP 状态 | 常见错误码 | 处理方式 |
| --- | --- | --- |
| 400 | `invalid_json`、`unexpected_body` | 修正 JSON 或按接口要求发送空正文。 |
| 401 | `invalid_api_key` | 核对 Bearer 密钥是否有效。 |
| 404 | `space_not_found`、`session_not_found`、`operation_not_found` | 核对资源 ID、所属空间和账号权限。 |
| 409 | `idempotency_conflict` | 恢复原批次时复用原参数。新批次使用新键。 |
| 413 / 415 | `request_too_large`、`unsupported_media_type` | 缩小请求体，或为 JSON 正文设置正确媒体类型。 |
| 422 | `invalid_request`、`invalid_cursor` | 按错误字段修正请求，续页沿用原筛选条件。 |
| 502 | `inference_failed` | 检查模型服务和推理响应。 |
| 503 | `remember_queue_full` | 退避后用原请求和幂等键重试。 |
| 503 | `write_models_unavailable`、`embedding_not_configured`、`reranker_not_configured` | 按[模型配置参考](model-configuration.md#应用配置并验收)核对对应角色。 |
| 503 | `commit_outcome_unknown` | `remember` 提交结果不明时，用原请求和幂等键重放。 |
| 503 | `database_unavailable`、`database_not_ready` | 按运维指南检查数据库与迁移账本。 |

部署相关的检查命令及常见故障见[自托管部署与运维](../../deploy/README.md#常见故障)。
