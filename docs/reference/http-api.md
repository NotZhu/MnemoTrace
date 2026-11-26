# HTTP API v1 接入指南

MnemoTrace 以空间组织跨会话记忆。应用把消息写入某个会话，等待后台任务完成，再从同一空间召回事实和原文证据。

本文先给出可运行的写入与召回流程，再按资源列出端点、响应字段和通用约定。部署和密钥签发见[快速开始](../../README.zh-CN.md#快速开始)，TypeScript 示例见 [SDK 指南](../../clients/typescript/README.md)。

## 地址、认证与契约

- 本地默认地址为 `http://127.0.0.1:8100`。业务接口使用 `/v1`，`GET /livez`、`GET /readyz` 和 `GET /openapi.json` 可用于检查服务。 [部署验收](../../deploy/README.md#部署验收)
- 业务请求带上 `Authorization: Bearer <API 密钥>`。服务端从密钥识别账号并校验空间归属。
- 路径中的空间、会话、任务、事实和消息 ID 由 API 返回，格式为 32 位小写十六进制。
- 带 JSON 正文的接口使用 `Content-Type: application/json`；创建会话、删除资源、清空空间和请求画像刷新时发送空正文。
- 时间使用带时区的 RFC 3339，最多六位小数。列表响应返回 `items` 和 `next_cursor`。

### 请求限制与默认值


| 项                   | 限制                                                 |
| ---------------------- | ------------------------------------------------------ |
| 空间名称             | 1–128 字符，去首尾空白                              |
| 会话消息批次         | 1–64 条；`role` 为 `user` 或 `assistant`            |
| `Idempotency-Key`    | 1–128 个可见 ASCII 字符                             |
| 列表 `limit`         | 1–100，默认 50                                      |
| `top_k`              | 1–100，默认 10                                      |
| `max_evidence_chars` | ≥1，省略时不限制                                    |
| `query`              | ≤4096 字符                                          |
| 请求体               | 默认最多 64 KiB，由 `MNEMOTRACE_MAX_BODY_BYTES` 配置 |
| ID 与时间            | ID 为 32 位小写十六进制；时间为带时区的 RFC 3339     |

## 写入并召回一条记忆

准备 Bash、`curl` 和 Python 3（命令为 `python3`）。把已签发的 `token` 设为 `MNEMOTRACE_API_KEY`；如果服务地址不同，设置 `MNEMOTRACE_BASE_URL`。以下步骤在同一个 Bash 会话中运行。

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

将 `SPACE_ID` 设为 `admin bootstrap` 返回的 `space_id`，或可访问的已有空间 ID。确认请求成功后，继续创建会话。

```bash
SPACE_ID='替换为已有空间 ID'
api "$BASE_URL/v1/spaces/$SPACE_ID"
```

空间响应包含 `space_id`、名称、记忆策略和创建时间：

```json
{
  "space_id": "…",
  "name": "default",
  "memory_decay": false,
  "profile_auto_refresh": false,
  "session_summary_enabled": false,
  "created_at": "2026-11-26T09:00:00Z"
}
```

### 创建空间

运行独立演示时，创建一个专用空间。成功返回 `201 Created`，`Location` 指向新空间；`name` 为 1–128 字符。

```bash
SPACE_ID=$(api_field space_id -X POST "$BASE_URL/v1/spaces" \
  -H 'Content-Type: application/json' \
  -d '{"name":"http-v1-demo"}')
```

### 创建会话

```bash
SESSION_ID=$(api_field session_id -X POST "$BASE_URL/v1/spaces/$SPACE_ID/sessions")
```

创建会话时发送空正文，成功返回 `201 Created`，`Location` 指向该会话；响应包含 `session_id` 和 `created_at`。

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

`202 Accepted` 表示服务已持久接收任务，`Location` 指向任务详情。响应包含任务 ID、状态和领取次数：

```json
{
  "operation_id": "…",
  "session_id": "…",
  "status": "queued",
  "attempts": 0,
  "created_at": "2026-11-26T09:00:00Z",
  "updated_at": "2026-11-26T09:00:00Z"
}
```

`Idempotency-Key` 按空间生效。客户端应为每批逻辑写入保留原键、空间 ID、会话 ID 和消息；重试时不要生成新键。

### 等待任务完成

```bash
api "$BASE_URL/v1/spaces/$SPACE_ID/operations/$OPERATION_ID"
```

收到 `queued` 或 `running` 时，在客户端的等待预算内继续查询同一任务；有 `next_retry_at` 时等到该时刻再查。收到 `succeeded` 后继续召回。遇到超时或失败时，按下文处理。 [幂等重试与恢复](#幂等重试与恢复)


| `status`    | 含义                                                                              |
| ------------- | ----------------------------------------------------------------------------------- |
| `queued`    | 等待处理或后台重试。                                                              |
| `running`   | 本次尝试已被 worker 领取。worker 中断时，任务可能保持此状态直到租约到期。         |
| `succeeded` | 原文已提交，响应包含 `message_ids` 和 `fact_change_count`。事实变更数可以为 `0`。 |
| `failed`    | 任务已结束。从 `error.code` 和可用的 `error.stage` 定位失败。                     |

任务成功时，详情响应类似：

```json
{
  "operation_id": "…",
  "session_id": "…",
  "status": "succeeded",
  "attempts": 1,
  "created_at": "2026-11-26T09:00:00Z",
  "updated_at": "2026-11-26T09:00:02Z",
  "message_ids": ["…"],
  "fact_change_count": 1
}
```

`attempts` 是 worker 已成功领取任务的次数。新任务从 `0` 开始，首次领取、后台重试和租约到期后的接管均计入，最多为 `3`。提交回执、任务详情和任务列表都返回此字段。`error.stage` 的取值为 `extraction`、`embedding`、`adjudication` 或 `publish`。

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

一次成功的召回响应包含证据、有效期和来源：

```json
{
  "evidence": [
    {
      "fact_id": "…",
      "text": "工作日坐地铁上班。",
      "valid_at": "2026-10-01T00:00:00Z",
      "invalid_at": null,
      "source_message_ids": ["…"],
      "relation": null
    }
  ],
  "degraded_reason": null,
  "rerank_applied": false,
  "truncated": false
}
```

`GET /v1/spaces/{space_id}/messages/{message_id}` 返回 `message_id`、`session_id`、`role`、`content`、`event_time` 和 `recorded_at`。若 `evidence` 为空，检查 `fact_change_count`、召回条件和响应中的 `degraded_reason`。演示结束后，可按[数据生命周期指南](../guides/data-lifecycle.md#选择操作)删除本次创建的专用空间。

## 端点索引

所有业务端点位于 `/v1`。创建空间和会话返回 `201` 和 `Location`；`remember` 和画像刷新返回 `202` 和 `Location`；列表端点返回 `items` 和 `next_cursor`；删除或清空返回 `204`。


| 资源 | 方法与路径                                                         | 用途                           |
| ------ | -------------------------------------------------------------------- | -------------------------------- |
| 空间 | `POST /v1/spaces`                                                  | 创建空间                       |
| 空间 | `GET /v1/spaces`                                                   | 列出空间                       |
| 空间 | `GET /v1/spaces/{space_id}`                                        | 读取空间                       |
| 空间 | `PATCH /v1/spaces/{space_id}`                                      | 更新名称或记忆策略             |
| 空间 | `POST /v1/spaces/{space_id}/purge`                                 | 清空空间；需 `Idempotency-Key` |
| 空间 | `DELETE /v1/spaces/{space_id}`                                     | 删除空间                       |
| 会话 | `POST /v1/spaces/{space_id}/sessions`                              | 创建会话；空正文               |
| 会话 | `GET /v1/spaces/{space_id}/sessions`                               | 列出会话                       |
| 会话 | `GET /v1/spaces/{space_id}/sessions/{session_id}`                  | 读取会话                       |
| 会话 | `DELETE /v1/spaces/{space_id}/sessions/{session_id}`               | 擦除会话                       |
| 会话 | `GET /v1/spaces/{space_id}/sessions/{session_id}/messages`         | 列出会话原文                   |
| 写入 | `POST /v1/spaces/{space_id}/sessions/{session_id}/remember`        | 提交消息批次                   |
| 写入 | `GET /v1/spaces/{space_id}/operations`                             | 列出任务                       |
| 写入 | `GET /v1/spaces/{space_id}/operations/{operation_id}`              | 读取任务                       |
| 写入 | `GET /v1/spaces/{space_id}/operations/{operation_id}/fact-changes` | 列出任务的事实变更             |
| 召回 | `POST /v1/spaces/{space_id}/recall`                                | 召回证据                       |
| 事实 | `GET /v1/spaces/{space_id}/facts`                                  | 列出事实                       |
| 事实 | `GET /v1/spaces/{space_id}/facts/{fact_id}`                        | 读取事实                       |
| 事实 | `GET /v1/spaces/{space_id}/facts/{fact_id}/changes`                | 列出事实变更                   |
| 事实 | `DELETE /v1/spaces/{space_id}/facts/{fact_id}`                     | 删除事实                       |
| 原文 | `GET /v1/spaces/{space_id}/messages/{message_id}`                  | 读取原文                       |
| 画像 | `GET /v1/spaces/{space_id}/profile`                                | 读取画像                       |
| 画像 | `POST /v1/spaces/{space_id}/profile/refresh`                       | 请求刷新；202 和 `Location`    |
| 健康 | `GET /livez`                                                       | 存活检查                       |
| 健康 | `GET /readyz`                                                      | 就绪检查                       |
| 契约 | `GET /openapi.json`                                                | OpenAPI 文档                   |

删除事实、擦除会话、清空空间或删除空间时，按[数据生命周期指南](../guides/data-lifecycle.md#选择操作)选择接口、判断重试方式并核对结果。

## 空间配置与派生数据

### 空间配置

`PATCH /v1/spaces/{space_id}` 更新空间名称或记忆策略，正文至少包含一个字段：


| 字段                      | 类型                | 说明                   |
| --------------------------- | --------------------- | ------------------------ |
| `name`                    | string，1–128 字符 | 空间名称               |
| `memory_decay`            | boolean             | 启用显著度衰减         |
| `profile_auto_refresh`    | boolean             | 事实变更后自动刷新画像 |
| `session_summary_enabled` | boolean             | 追加原文后滚动摘要     |

```json
{
  "profile_auto_refresh": true,
  "session_summary_enabled": true
}
```

成功时返回更新后的空间。画像刷新和会话摘要的自动机制按空间开启，默认关闭；行为细节见[记忆引擎架构](../architecture/memory.md#后台派生数据)。

### 用户画像

`GET /v1/spaces/{space_id}/profile` 返回当前可见画像和刷新状态：


| 字段                                | 含义                                                 |
| ------------------------------------- | ------------------------------------------------------ |
| `profile.basic`                     | 身份与基础信息                                       |
| `profile.communication_preferences` | 沟通偏好                                             |
| `profile.preferences`               | 一般偏好                                             |
| `profile.constraints`               | 约束与禁止事项                                       |
| `updated_at`                        | 最近一次成功刷新时间                                 |
| `refresh_status`                    | `idle`、`queued`、`running`、`succeeded` 或 `failed` |
| `next_retry_at`                     | 下次可重试时间                                       |
| `refresh_error.code`                | 最近一次刷新的公开错误分类                           |

每个画像条目包含 `text` 和支撑它的 `fact_ids`。`POST /v1/spaces/{space_id}/profile/refresh` 发送空正文，返回 `202` 和 `Location`；之后轮询 `GET /profile` 查看刷新状态。

## 幂等重试与恢复

`remember` 的 `Idempotency-Key` 使用 1 到 128 个可见 ASCII 字符，按空间生效。客户端应为每批逻辑写入保留原键、空间 ID、会话 ID 和消息。同一空间用相同键重放原会话与原消息，会返回原任务及其当前状态和领取次数。用同一键换会话或消息，则返回 `409 idempotency_conflict`。

- **提交结果不明**。连接中断、提交超时或收到 `503 commit_outcome_unknown` 时，用原请求和原幂等键重试。
- **已经取得任务 ID**。连接恢复或客户端等待超时后，继续查询同一任务。有 `next_retry_at` 时等到该时刻再查。停止轮询后，后台仍可处理任务。
- **worker 中断**。保持轮询同一任务。租约到期后，有剩余尝试次数的任务由可用的 worker 自动接管，任务 ID 保持不变。 [worker 恢复](../../deploy/README.md#恢复后台写入)
- **任务最终失败**。先处理失败原因。如果仍需写入且原空间和会话可用，使用客户端保留的输入和新幂等键提交。新任务进入该会话的队尾，旧键仍返回原失败任务。失败任务结束时，服务清除其待处理消息。

## 召回选项

`recall` 的请求字段：


| 字段                 | 类型与默认值         | 用法                                                                                                             |
| ---------------------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `query`              | string，≤4096 字符  | 必填。用于生成查询向量和词法检索。                                                                               |
| `top_k`              | int，1–100，默认 10 | 限制返回的证据条数。                                                                                             |
| `as_of`              | RFC 3339，可选       | 在当前已提交的数据中按业务有效时间筛选；省略时查询当前有效事实。                                                 |
| `filters`            | object，可选         | `labels` 为标签数组，任一匹配即可；`source` 为 `extracted` 或 `manual`。                                         |
| `graph`              | object，可选         | `origin_entity_ids` 最多 32 个；`predicates` 为 1–32 个谓词；`max_hops` 为 1–4，默认 2。省略起点时由服务选择。 |
| `rerank`             | bool，可选           | 省略时使用已配置的重排器；`false` 跳过，`true` 要求服务端已配置重排器。                                          |
| `prefer_recent`      | bool，默认 `false`   | 为近期生效的事实小幅加分，只对省略 `as_of` 的查询生效。                                                          |
| `track_usage`        | bool，默认 `true`    | 对启用了显著度衰减的空间，可设为 `false` 关闭此次召回的使用记录。                                                |
| `include_sensitive`  | bool，默认 `false`   | 设为 `true` 可纳入带敏感标记的事实。                                                                             |
| `max_evidence_chars` | int，≥1，可选       | 限制证据正文的总字符数；预算内只返回完整条目，裁剪时 `truncated` 为 `true`。                                     |

召回证据字段：


| 字段                 | 含义                         |
| ---------------------- | ------------------------------ |
| `fact_id`            | 事实 ID                      |
| `text`               | 事实正文                     |
| `valid_at`           | 业务有效起点，未知时为空     |
| `invalid_at`         | 业务有效截止，尚无截止时为空 |
| `source_message_ids` | 支撑该事实的原文 ID          |
| `relation`           | 实体关系端点；普通事实为空   |

`rerank_applied` 表示本次是否实际重排。成功响应的 `degraded_reason` 有值时，当前结果来自降级的检索路径；取值为 `embedding_timeout` 或 `reranker_timeout`。空的 `evidence` 表示此次请求未返回证据。

## 列表、分页和审计

列表接口返回 `items` 和 `next_cursor`。首页可传 `limit`（1–100，默认 50）。获取下一页时保留原查询的资源范围和筛选条件，把响应中的 `next_cursor` 原样作为 `cursor` 传入；`next_cursor` 为 `null` 时已到末页。

`GET /v1/spaces/{space_id}/facts` 支持 `state`、`as_of`、`include_sensitive`、`labels` 和 `source`：


| 参数                | 用法                                                                                                       |
| --------------------- | ------------------------------------------------------------------------------------------------------------ |
| `state`             | `current`（默认，当前有效）、`history`（在参考时点已截止）或 `all`（完整时间线）；`all` 时不能传 `as_of`。 |
| `as_of`             | 按业务有效时间筛选；省略时使用当前时点。                                                                   |
| `include_sensitive` | 默认 `false`；设为 `true` 纳入敏感事实。                                                                   |
| `labels`、`source`  | 按标签和创建来源筛选。                                                                                     |

`GET /v1/spaces/{space_id}/operations` 支持按 `status`（`queued`、`running`、`succeeded`、`failed`）和 `session_id` 筛选。

任务的事实变更和事实的有效期变更通过端点索引中的 `fact-changes` 和 `changes` 接口查询。审计记录包含变更前后的有效期和截止归因；行为细节见[记忆引擎架构](../architecture/memory.md#事实生命周期)。

## 事实与原文

`GET /v1/spaces/{space_id}/facts/{fact_id}` 在召回证据字段之外返回：

| 字段 | 含义 |
| --- | --- |
| `recorded_at` | 服务记录该事实的时间 |
| `expired_at` | 首次记录截止的时间；尚无截止时为空 |
| `sensitive` | 事实是否带敏感标记 |
| `source` | `extracted` 或 `manual` |
| `labels` | 标签数组 |

`GET /v1/spaces/{space_id}/messages/{message_id}` 返回：

| 字段 | 含义 |
| --- | --- |
| `message_id` | 原文 ID |
| `session_id` | 所属会话 |
| `role` | `user` 或 `assistant` |
| `content` | 原文正文 |
| `event_time` | 发言时间；未知时为空 |
| `recorded_at` | 服务记录时间 |

召回证据中的 `relation` 非空时，包含 `subject_entity_id`、`predicate` 和 `object_entity_id`。

## 错误响应

HTTP 错误使用 `application/problem+json`。客户端按 `status` 和 `code` 处理，字段校验失败时可查看 `errors` 中的位置和错误码。排查时记录正文的 `request_id` 或响应头 `X-Request-ID`。

```json
{
  "type": "about:blank",
  "title": "Conflict",
  "status": 409,
  "detail": "该幂等键已用于不同的会话或消息批次。",
  "code": "idempotency_conflict",
  "request_id": "…"
}
```

422 响应额外包含 `errors`：

```json
{
  "type": "about:blank",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "请求字段未通过校验。",
  "code": "invalid_request",
  "request_id": "…",
  "errors": [
    {"location": "body", "path": "/messages/0/role", "code": "invalid_value"}
  ]
}
```


| HTTP 状态 | 常见错误码                                                                                           | 处理方式                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| 400       | `invalid_json`、`unexpected_body`                                                                    | 修正 JSON 或按接口要求发送空正文。                                    |
| 401       | `invalid_api_key`                                                                                    | 核对 Bearer 密钥是否有效。                                            |
| 404       | `space_not_found`、`session_not_found`、`operation_not_found`、`fact_not_found`、`message_not_found` | 核对资源 ID、所属空间和账号权限。                                     |
| 409       | `idempotency_conflict`                                                                               | 恢复原批次时复用原参数。新批次使用新键。                              |
| 413 / 415 | `request_too_large`、`unsupported_media_type`                                                        | 缩小请求体，或为 JSON 正文设置正确媒体类型。                          |
| 422       | `invalid_request`、`invalid_cursor`                                                                  | 按错误字段修正请求，续页沿用原筛选条件。                              |
| 500       | `internal_error`                                                                                     | 记录 `request_id`，检查服务日志。                                     |
| 502       | `inference_failed`                                                                                   | 检查模型服务和推理响应。                                              |
| 503       | `remember_queue_full`                                                                                | 退避后用原请求和幂等键重试。                                          |
| 503       | `write_models_unavailable`、`embedding_not_configured`、`reranker_not_configured`                    | 按[模型配置参考](model-configuration.md#应用配置并验收)核对对应角色。 |
| 503       | `commit_outcome_unknown`                                                                             | `remember` 提交结果不明时，用原请求和幂等键重放。                     |
| 503       | `database_unavailable`、`database_not_ready`                                                         | 按运维指南检查数据库与迁移账本。                                      |

调试单个请求时，可直接用 `curl -sS -i` 带上相同的请求头和正文，查看响应头和错误正文。部署相关的检查命令及常见故障见[自托管部署与运维](../../deploy/README.md#常见故障)。

## 相关文档

- [快速开始](../../README.zh-CN.md#快速开始)：启动服务、签发 API 密钥并跑通写入和召回。
- [TypeScript SDK](../../clients/typescript/README.md)：在 Node.js 中写入、召回并追溯原文。
- [记忆引擎架构](../architecture/memory.md)：事实时间轴、证据组、召回和擦除的一致性边界。
- [数据生命周期指南](../guides/data-lifecycle.md)：删除事实、擦除会话、清空或删除空间。
- [模型配置参考](model-configuration.md)：补全、嵌入和重排端点。
- [自托管部署与运维](../../deploy/README.md)：部署、备份、密钥轮换和故障排查。
