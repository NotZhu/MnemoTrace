# HTTP API

在 `backend` 下运行以下命令。先应用显式迁移并配置运行和管理角色。数据库连接串放在环境变量中。 [PostgreSQL 设置](postgres.md)

| 环境变量 | 用途 |
| --- | --- |
| `MNEMOTRACE_NEXT_MIGRATION_DSN` | 使用 `python -m mnemotrace_next.adapters.postgres.migrate up` 应用迁移。 |
| `MNEMOTRACE_NEXT_ADMIN_DSN` | 在本机签发和撤销账号凭据。 |
| `MNEMOTRACE_NEXT_POSTGRES_DSN` | HTTP 运行角色连接。 |
| `MNEMOTRACE_NEXT_MAX_BODY_BYTES` | 请求体字节上限，默认 65536。 |

## 管理凭据和启动

```sh
uv run --locked python -m mnemotrace_next.admin issue --owner-id personal
uv run --locked python -m mnemotrace_next.admin list --owner-id personal
uv run --locked python -m mnemotrace_next.admin revoke --owner-id personal <key-id>
uv run --locked --extra server python -m mnemotrace_next.server
```

为已有账号签发密钥时，`--owner-id` 使用数据库中保存的原值。签发命令以 JSON 输出 `token`，仅此次可以取得明文。把它保存到本地凭据配置，发送请求时使用 `Authorization: Bearer <token>`。同一账号的多个有效 key 均可访问该账号拥有的空间。轮换前先签发新 key，切换调用方后再撤销旧 key。

服务在启动前校验数据库连接串格式，错误信息只显示配置项名称。服务默认监听 `127.0.0.1:8100`，支持 `--host`、`--port`。对外提供服务时，在反向代理终止 HTTPS。

| 公共端点 | 用途 |
| --- | --- |
| `GET /livez` | 进程可响应时返回 200。 |
| `GET /readyz` | 数据库可访问且迁移账本匹配时返回 200，其他情况返回 503。 |
| `GET /openapi.json`、`GET /docs` | OpenAPI 契约和交互文档。 |

`/v1` 的每个操作都有显式的 OpenAPI `operationId`，供生成客户端使用。

## 空间和会话

| 端点 | 成功结果 |
| --- | --- |
| `POST /v1/spaces` | JSON 请求 `{"name":"研究"}`。返回 201、空间及 `Location`。 |
| `GET /v1/spaces` | 分页列出账号拥有的空间，含空空间。 |
| `GET /v1/spaces/{space_id}` | 返回 `space_id`、`name` 和 UTC `created_at`。 |
| `PATCH /v1/spaces/{space_id}` | 用同一 JSON 结构改名，返回更新后的空间。 |
| `POST /v1/spaces/{space_id}/sessions` | 正文可省略，提交的正文仍受字节上限约束。返回 201、`session_id`、UTC `created_at` 和 `Location`。 |
| `GET /v1/spaces/{space_id}/sessions` | 分页列出已登记的会话，含空会话。 |
| `GET /v1/spaces/{space_id}/sessions/{session_id}` | 返回会话登记的 `session_id` 和 `created_at`。 |

空间名去除首尾空白后须为 1–128 字符。会话列表按创建时间倒序，同刻按会话 ID 升序。历史会话的创建时间未知时为 `null`，排在末尾。

空间、会话和事实列表使用同一分页契约。请求使用 `limit`（1–100，默认 50）和可选的 `cursor`，响应含 `items` 和 `next_cursor`。续页原样回传上页的 `next_cursor`。值为 `null` 时已到末页。

错误响应使用 `application/problem+json`，包含 `code` 和 `request_id`。无效或已撤销的凭据返回 401。不存在或不可访问的空间、会话返回 404。若收到 `503 commit_outcome_unknown`，写入可能已提交。先查询资源列表核对结果，再决定是否重试。

`422 invalid_request` 的 `errors` 为字段校验列表，每项包含参数位置 `location`（`body`、`query` 或 `path`）、JSON Pointer `path` 和稳定的 `code`。例如 `{"location":"body","path":"/graph/max_hops","code":"out_of_range"}`。`path` 为空字符串时指向该位置的根。未知字段的错误定位到包含它的对象。错误码包括 `required`、`unknown_field`、`out_of_range`、`invalid_length` 和 `invalid_value`。JSON 语法错误返回 `400 invalid_json`。

## 召回

在 HTTP 进程配置查询嵌入端点、模型、修订与维度。服务启动时检查配置并复用推理客户端。未配置嵌入时，召回返回 `503 embedding_not_configured`，事实检查和原文读取仍可使用。 [嵌入配置](inference.md#环境变量)

`POST /v1/spaces/{space_id}/recall` 接受 JSON 对象。

| 字段 | 作用 |
| --- | --- |
| `query` | 非空查询，最多 4096 字符。 |
| `top_k` | 证据上限，取 1–100，默认 10。 |
| `as_of` | 可选业务有效时点，使用带偏移或 `Z` 的 RFC 3339 日期时间，最多六位小数。省略时查当前。 |
| `graph` | 可选，传 `{}` 启用默认图召回。可用 `max_hops`（1–4，默认 2）、`origin_entity_ids` 和 `predicates` 限定遍历。显式传空起点数组时只返回普通召回结果。 |
| `include_sensitive` | 默认 `false`。显式传 `true` 可召回该账号空间中的敏感事实。 |

成功响应的 `evidence` 按相关性排序。每条证据包含事实 ID、文本、有效区间、来源消息 ID 和可选的 `relation`。有关系时，召回和事实响应都使用 `subject_entity_id`、`predicate`、`object_entity_id`。有效区间左闭右开。`as_of` 使用当前已经收录的证据判断该时点有效性，补录历史会影响结果。没有命中时返回空数组。查询嵌入超时时返回词法和已启用图路的结果，`degraded_reason` 为 `embedding_timeout`。嵌入失败或响应无效时返回 `502 inference_failed`。

## 事实和原文

| 端点 | 结果 |
| --- | --- |
| `GET /v1/spaces/{space_id}/facts` | 返回 `items`、`next_cursor` 和 `evaluated_at`。 |
| `GET /v1/spaces/{space_id}/facts/{fact_id}` | 返回事实详情。除有效区间外，还提供 `recorded_at` 和首次获知截止时间 `expired_at`。 |
| `GET /v1/spaces/{space_id}/messages/{message_id}` | 返回原文正文、会话 ID、角色、事件时间和收录时间。事件时间未知或超出日历范围时，`event_time` 为 `null`。 |

事实列表用 `state=current`（默认）、`history` 或 `all` 选择有效、已截止或完整时间线。前两种可传 `as_of`。列表和详情默认隐藏敏感事实，可用 `include_sensitive=true` 显式读取。按收录时间倒序、同刻按事实 ID 升序分页。续页沿用原筛选。同一页序列固定业务评估时点，每页读取独立数据库快照。服务器会重新校验空间归属和事实可见性。
