# HTTP API

自托管可用 [Compose 部署](../../deploy/next/README.md)。以下为源码启动方式。

在 `backend` 下运行以下命令。先应用显式迁移并配置运行和管理角色。数据库连接串放在环境变量中。 [PostgreSQL 设置](postgres.md)

| 环境变量 | 用途 |
| --- | --- |
| `MNEMOTRACE_NEXT_MIGRATION_DSN` | 使用 `python -m mnemotrace_next.adapters.postgres.migrate up` 应用迁移。 |
| `MNEMOTRACE_NEXT_ADMIN_DSN` | 在本机签发和撤销账号凭据。 |
| `MNEMOTRACE_NEXT_POSTGRES_DSN` | API 与 worker 的运行角色连接。 |
| `MNEMOTRACE_NEXT_MAX_BODY_BYTES` | API 请求体字节上限，默认 65536。 |
| `MNEMOTRACE_NEXT_QUEUE_LIMIT` | API 接收写入时每个空间的待处理任务上限，包含等待和执行中的任务，默认 100。 |
| `MNEMOTRACE_NEXT_EXTRACTION_TIMEZONE` | worker 解释“今天”“明天”等日历时间时使用的 IANA 时区，默认 `UTC`。Compose 部署在 `deploy/next/.env` 中设置，例如 `Asia/Shanghai`。 |

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
| `GET /openapi.json` | OpenAPI 3.1 契约。 |

`/v1` 的每个操作都有显式的 OpenAPI `operationId`，供生成客户端使用。服务关闭 Swagger UI（`/docs`）和 ReDoc（`/redoc`）。

可放入路径的资源标识以响应字段和 `Location` 中的值为准。路径安全的普通 ID 保持原样。包含斜杠等字符、是 `.` 或 `..`、或以保留前缀 `~` 开头的存量 ID，使用 `~` 加无填充 Base64URL 暴露。调用方可直接在路径中使用返回的值。事实证据的 `source_message_ids` 和任务结果的 `message_ids` 也采用同一表示。

## 空间和会话

| 端点 | 成功结果 |
| --- | --- |
| `POST /v1/spaces` | JSON 请求 `{"name":"研究"}`。返回 201、空间及 `Location`。 |
| `GET /v1/spaces` | 分页列出账号拥有的空间，含空空间。 |
| `GET /v1/spaces/{space_id}` | 返回 `space_id`、`name`、`memory_decay` 和 UTC `created_at`。 |
| `PATCH /v1/spaces/{space_id}` | 修改 `name`、`memory_decay` 或同时修改两项。返回更新后的空间，`operationId` 仍为 `renameSpace`。 |
| `POST /v1/spaces/{space_id}/sessions` | 正文可省略，提交的正文仍受字节上限约束。返回 201、`session_id`、UTC `created_at` 和 `Location`。 |
| `GET /v1/spaces/{space_id}/sessions` | 分页列出已登记的会话，含空会话。 |
| `GET /v1/spaces/{space_id}/sessions/{session_id}` | 返回会话登记的 `session_id` 和 `created_at`。 |

空间名去除首尾空白后须为 1–128 字符。空间详情和列表均返回严格布尔开关 `memory_decay`，新建与升级空间默认为 `false`。发送 `{"memory_decay":true}` 可为指定空间启用显著度加权及自动强化。PATCH 至少提供一个字段，省略的字段保留原值。存量空间的 `created_at` 超出日历范围时返回 `null`，并列在 `out_of_range_fields` 中，正常时该数组为空。会话列表按创建时间倒序，同刻按会话 ID 升序。历史会话的创建时间未知时为 `null`，排在末尾。

列表使用同一分页契约。请求使用 `limit`（1–100，默认 50）和可选的 `cursor`，响应含 `items` 和 `next_cursor`。续页原样回传上页的 `next_cursor`。值为 `null` 时已到末页。

错误响应使用 `application/problem+json`，包含 `code` 和 `request_id`。无效或已撤销的凭据返回 401。不存在或不可访问的空间、会话返回 404。若收到 `503 commit_outcome_unknown`，写入可能已提交。先查询资源列表核对结果，再决定是否重试。

`422 invalid_request` 的 `errors` 为字段校验列表，每项包含参数位置 `location`（`body`、`query`、`path` 或 `header`）、JSON Pointer `path` 和稳定的 `code`。例如 `{"location":"body","path":"/graph/max_hops","code":"out_of_range"}`。`path` 为空字符串时指向该位置的根。未知字段的错误定位到包含它的对象。错误码包括 `required`、`unknown_field`、`out_of_range`、`invalid_length` 和 `invalid_value`。无法编码为 UTF-8 的文本字段返回 `invalid_value`，JSON 语法错误返回 `400 invalid_json`。

## 写入与任务

在 API 与 worker 进程配置同一组补全、嵌入模型及 PostgreSQL 运行连接串，确保向量模型的版本和维度一致。应用迁移、配置运行角色后，在单独的进程运行 worker。默认并发为 2，可用 `--concurrency` 设置为 1–16。收到停机信号后，worker 停止领取新任务。 [模型配置](inference.md#环境变量) · [PostgreSQL 设置](postgres.md)

```sh
uv run --locked --extra server python -m mnemotrace_next.worker
```

可用 `--readiness-port` 启用本机 TCP 就绪探针。worker 成功装配模型配置并核对数据库迁移后开始监听，停机时关闭端口。上游模型调用是否成功由闭环示例验证。

`POST /v1/spaces/{space_id}/sessions/{session_id}/remember` 接受包含 1–64 条消息的 `messages` 数组，每条有 `role`（`user` 或 `assistant`）、非空 `content` 和可选的 RFC 3339 `event_time`。省略发言时间时用任务接收时刻。请求受全局正文大小上限约束。先登记会话，并为每批消息生成一个含 1–128 个可见 ASCII 字符的 `Idempotency-Key`。

```sh
curl -i -X POST "$BASE_URL/v1/spaces/$SPACE_ID/sessions/$SESSION_ID/remember" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $BATCH_ID" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"星河项目默认导出 XLSX。"}]}'
```

任务持久接收后返回 `202`，响应体包含 `operation_id`、`session_id`、`status`、`created_at` 和 `updated_at`，`Location` 指向 `GET /v1/spaces/{space_id}/operations/{operation_id}`。在同一空间，用同键和同参重送会返回原任务的当前状态。同键更换会话或消息返回 `409 idempotency_conflict`。连接中断或收到 `503 commit_outcome_unknown` 时，用原键和原参数安全重送。

| 状态 | 可用字段及行为 |
| --- | --- |
| `queued` | 等待领取。处于重试退避时返回 `next_retry_at`。 |
| `running` | worker 持有任务租约。 |
| `succeeded` | 返回按输入顺序排列的 `message_ids` 和实际 `fact_change_count`。零事实批次仍保留原文，计数为 0。 |
| `failed` | 返回 `error.code`，若能定位则附 `error.stage`（`extraction`、`embedding`、`adjudication` 或 `publish`）。重新提交需要新键。 |

同一会话按持久接收顺序执行，前批退避期间后批等待。其他会话可继续处理。每次尝试最多运行 180 秒，领取租约 210 秒，含过期租约在内最多尝试三次。可重试错误分别退避 5 秒、10 秒。原文、事实、向量、审计和任务成功状态在同一事务发布，终态会释放待处理的消息副本。持久任务结果和幂等记录当前随会话保留，删除会话登记会级联清理其任务记录。

worker 短暂停止期间 API 可继续接收。达到单空间上限后，新任务返回 `503 remember_queue_full`，原键重放和任务查询仍可用。缺少补全模型必填项时，HTTP 服务仍可启动，已配置嵌入模型的召回及既有任务查询继续可用，新写入返回 `503 write_models_unavailable`。worker 启动需要完整的补全和嵌入配置。

## 召回

在 HTTP 进程配置查询嵌入端点、模型、修订与维度。配置 Cohere v2 兼容重排端点后，服务自动为召回启用重排。服务启动时检查配置并复用推理客户端。未配置嵌入时，召回返回 `503 embedding_not_configured`，事实检查和原文读取仍可使用。 [推理配置](inference.md#环境变量)

`POST /v1/spaces/{space_id}/recall` 接受 JSON 对象。

| 字段 | 作用 |
| --- | --- |
| `query` | 非空查询，最多 4096 字符。 |
| `top_k` | 证据上限，取 1–100，默认 10。 |
| `as_of` | 可选业务有效时点，使用带偏移或 `Z` 的 RFC 3339 日期时间，最多六位小数。省略时查当前。 |
| `graph` | 可选，传 `{}` 启用默认图召回。可用 `max_hops`（1–4，默认 2）、`origin_entity_ids` 和 `predicates` 限定遍历。显式传空起点数组时只返回普通召回结果。可将证据中的 `relation.predicate` 原样传入 `predicates`。 |
| `rerank` | 省略或传 `null` 时采用部署默认。传 `false` 跳过重排。传 `true` 时，空间授权通过后如果未配置重排器，返回 `503 reranker_not_configured`。嵌入也未配置时优先返回 `embedding_not_configured`。 |
| `prefer_recent` | 严格布尔值，默认 `false`。开启时，在当前事实的相关候选中优先返回近期生效的事实。 |
| `track_usage` | 严格布尔值，默认 `true`。传 `false` 保持同样的排序并跳过强化。 |
| `include_sensitive` | 默认 `false`。显式传 `true` 可召回该账号空间中的敏感事实。 |
| `filters` | 可选。按已知 `labels` 和单个 `source` 筛选候选。传空标签数组时不限标签。 |
| `max_evidence_chars` | 可选正整数，限制最终证据的正文总字符数。 |

成功响应的 `evidence` 按相关性排序。每条证据包含事实 ID、文本、有效区间、来源消息 ID 和可选的 `relation`。有关系时，召回和事实响应都使用 `subject_entity_id`、`predicate`、`object_entity_id`。有效区间左闭右开。`as_of` 使用当前已经收录的证据判断该时点有效性，补录历史会影响结果。历史召回沿用相关性排序，跳过两种加成和强化。没有命中时返回空数组。

请求 `{"query":"报表导出格式"}` 采用部署默认。请求 `{"query":"报表导出格式","rerank":false,"track_usage":false}` 跳过重排和强化，便于跨入口核对排序。响应中的 `rerank_applied` 仅在有效重排结果参与排序时为 `true`。

例如，传入 `{"filters":{"labels":["technology","professional_details"],"source":"extracted"}}` 可筛选带任一指定标签的抽取事实。标签使用[领域分类](../src/mnemotrace_next/domain/values.py)，`source` 为 `extracted` 或 `manual`。词法、向量和图的每一跳都在选择候选时应用过滤，并与有效期及敏感可见性条件一起生效。

字符预算在排序并取 `top_k` 之后按 Unicode 码点累计 `text`。返回能装入预算的完整证据前缀。遇到首条放不下的证据时停止，响应的 `truncated` 为 `true`，首条超限时 `evidence` 为空。预算省略、无命中或预算容纳全部入选证据时，`truncated` 为 `false`。预算只计算证据正文。

启用重排时，按 RRF 选出最多 `min(100, max(20, 2 × top_k))` 条可见候选，由重排器为候选逐条打分。最终分为 `基础分 × (1 + min(0.05, 新近度加成 + 显著度加成))`，基础分取重排分，未使用重排时取 RRF 分。最终分相同时依次按基础分和 RRF 顺序排列。新近度加成为 `0.05 × 2^(-距生效天数 / 30)`，从 `valid_at` 算到本次查询时点，起点未知或未启用时为零。空间开启 `memory_decay` 时显著度加成为 `0.05 × (有效显著度 - 1) / 2`，未强化的事实使用基线。 [显著度领域规则](../src/mnemotrace_next/domain/salience.py)

候选池只有一条时直接返回证据，跳过外部重排。查询嵌入超时会按剩余词法和图候选加权，标记 `embedding_timeout`。重排超时或重试结束后仍收到上游 `504` 时，按已读取候选的 RRF 分加权，标记 `reranker_timeout`。其他推理失败或协议错误返回 `502 inference_failed`。

空间开启衰减且允许计数时，HTTP 对成功、非空、无降级的当前召回异步强化最终返回的事实，每条每次最多一次。消费者写入前复核空间设置、擦除标记和事实可见性。响应无需等待写回，每个 HTTP 进程的内存队列容量为 128，队列满、进程停止或写入失败时可以丢弃反馈。

## 事实和原文

| 端点 | 结果 |
| --- | --- |
| `GET /v1/spaces/{space_id}/facts` | 返回 `items`、`next_cursor` 和 `evaluated_at`。 |
| `GET /v1/spaces/{space_id}/facts/{fact_id}` | 返回事实详情。除有效区间外，还提供 `recorded_at` 和首次获知截止时间 `expired_at`。 |
| `GET /v1/spaces/{space_id}/messages/{message_id}` | 返回原文正文、会话 ID、角色、事件时间和收录时间。事件时间未知或超出日历范围时，`event_time` 为 `null`。 |

事实列表用 `state=current`（默认）、`history` 或 `all` 选择有效、已截止或完整时间线。前两种可传 `as_of`。列表和详情默认隐藏敏感事实，可用 `include_sensitive=true` 显式读取。按收录时间倒序、同刻按事实 ID 升序分页。续页沿用原筛选。同一页序列固定业务评估时点，每页读取独立数据库快照。服务器会重新校验空间归属和事实可见性。

列表也可使用 `labels=technology&labels=professional_details&source=extracted`，过滤语义与召回相同。续页时标签顺序及重复项不影响匹配。更改筛选范围返回 `422 invalid_cursor`。

事实详情、列表和召回证据中的时间以 RFC 3339 表示。存量时刻超出可表示的日历范围时，该字段返回 `null`，字段名列在 `out_of_range_fields` 中。原文收录时刻 `recorded_at` 使用同样的越界表示。没有越界字段时，`out_of_range_fields` 为空数组。业务有效期筛选和分页仍使用原始时刻。

## 任务变更和会话原文

以下路径位于 `/v1/spaces/{space_id}` 下。

| 端点 | 行为 |
| --- | --- |
| `GET /operations` | 按接收顺序倒序列出任务，可组合使用 `status` 和 `session_id`。任务详情仍可通过单任务接口读取。 |
| `GET /operations/{operation_id}/fact-changes` | 返回任务 `status`，按审计追加顺序倒序列出本次任务发布的可见变更。 |
| `GET /facts/{fact_id}/changes` | 按审计追加顺序倒序列出指定事实的可见变更和截止依据。 |
| `GET /sessions/{session_id}/messages` | 按原文追加顺序分页，沿用单条原文的字段。已登记的空会话返回空列表。 |

续页使用相同的空间、目标资源和筛选条件。游标与查询不匹配时返回 `422 invalid_cursor`。每页重新校验账号和空间归属，在当时的数据库快照中读取。不存在、已擦除或不可访问的嵌套资源返回对应的 `404`。

变更包含 `fact_id`、`changed_at`，以及前后有效区间 `before` 和 `after`。新建时 `before` 为 `null`。按裁决顺序排列的 `cutoff_triggers` 包含 `fact_id`、`valid_end` 和 `source_message_ids`，旧记录无法恢复逐项来源时该字段为 `null`。时间采用 RFC 3339。越界时间返回 `null`，并在 `out_of_range_fields` 中列出对应的 JSON Pointer，例如 `/after/invalid_at`。

两个变更接口默认隐藏当前事实或任一前后快照标为敏感的条目，并省略敏感触发事实的引用。传 `include_sensitive=true` 可显式纳入敏感项。证据 ID 可以直接用于对应的事实和原文读取端点。

任务状态为 `queued`、`running` 或 `failed` 时，`items` 为空且 `next_cursor` 为 `null`。`succeeded` 时返回仍可见的关联变更，零变更或无可见变更时也返回空列表。删除事实或来源后，相关变更可能被擦除，原任务的 `fact_change_count` 仍表示提交时的统计值。

任务与逐条审计的关联由支持迁移 `012` 的 worker 在发布时写入。升级前已经完成的任务保留原有 `fact_change_count`，逐条关联不回填，因此即使计数大于零，`/fact-changes` 仍返回 `status: "succeeded"` 和空列表。

## 删除与清空

| 端点 | 提交后生效的结果 |
| --- | --- |
| `DELETE /v1/spaces/{space_id}/facts/{fact_id}` | 移除事实、索引和依赖记录，保留来源原文。 |
| `DELETE /v1/spaces/{space_id}/sessions/{session_id}` | 撤销会话，擦除原文、摘要、相关任务及证据贡献。仍有独立证据的事实可继续读取。 |
| `POST /v1/spaces/{space_id}/purge` | 擦除空间的记忆、会话及其任务，保留空间和嵌入模型的维度登记。使用原空间写入前，先登记新会话。 |
| `DELETE /v1/spaces/{space_id}` | 删除空间和其中的全部数据，包括嵌入模型的维度登记和清空幂等记录。 |

这四个端点都要求空正文，提交后返回 `204`。目标不存在、已删除或不可访问时返回 `404`。成功响应后开启的读取会反映擦除结果。会话撤销后，其在途任务无法发布。空间清空和删除会撤销该空间下的所有会话。

清空必须带 `Idempotency-Key`，格式与写入相同。清空和写入各自记录幂等键。清空在同一空间用同键重放会返回 `204`，保留上一次清空之后写入的内容。连接中断或收到 `503 commit_outcome_unknown` 时，使用原键重试。需要再次清空时使用新键。

```sh
curl -i -X POST "$BASE_URL/v1/spaces/$SPACE_ID/purge" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $PURGE_ID"
```

## HTTP 闭环示例

服务和 worker 启动后，把签发响应中的 `token` 注入 `MNEMOTRACE_NEXT_SMOKE_TOKEN`。从 `backend` 运行以下命令。示例只操作新建的验收空间，写入合成消息，检查任务结果、召回证据、来源原文和清空效果，并删除该空间。重放旧的清空幂等键时，示例会检查清空后新写入的原文仍然可读。

```sh
uv run --locked --extra server python scripts/http_smoke.py
```

使用 `--base-url` 指定其他本机地址或 HTTPS 地址。`--timeout` 调整每项写入任务的轮询上限，默认 240 秒。`--recall-read-timeout` 单独调整召回请求的 HTTP 读取超时，默认 45 秒。增大服务端查询嵌入超时时，为召回请求预留数据库查询时间。 [推理超时配置](inference.md#环境变量)

创建空间的结果不明时，脚本会按本次唯一名称在账号的空间列表中查找，以便清理。无法确认清理时，脚本会输出该名称，供后续核对。
