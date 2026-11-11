# HTTP API

HTTP 服务是多应用接入和控制台管理的边界。启动方式见[部署说明](../../deploy/README.md)，字段定义与路由来源见 [app.py](../../mnemotrace/server/app.py)。

## 地址、鉴权与公共端点

产品 API 前缀为 `/v1`。受保护路由发送 `Authorization: Bearer <key>`。
后端在 Postgres 中解析有效 key 并取得账号身份。单空间写请求在 JSON 中传 `space_id`，
读取和 DELETE 在查询参数传 `space_id`，空间管理使用路径中的 ID。
同一账号的有效 key 可访问其拥有的空间。未知或他人空间均返回 `404 space_not_found`。

`GET /v1/spaces` 返回空间列表及下一页游标，空空间也会列出。
`POST /v1/spaces` 接受 `{"name":"研究"}`。同一格式可用于 `PATCH /v1/spaces/{space_id}` 改名。
删除使用 `DELETE /v1/spaces/{space_id}?confirm=true`。

| 公共端点 | 用途 |
| --- | --- |
| `GET /health` | 进程存活和运行配置概况，`ok=true` 不等于 ready |
| `GET /ready` | 就绪时 200，否则 503；健康门禁使用此端点 |
| `GET /metrics` | 聚合计数、延迟与 token 观测，不包含记忆正文 |
| `GET /docs`、`GET /openapi.json` | 框架生成的交互参考和 schema；不包含全部响应语义 |
| `GET /ui/` | 已构建的管理控制台，未构建时返回 503 |

HTTP 请求体默认上限为 2 MiB，可通过配置调整；实际接收字节与 Content-Length 都受检查。鉴权失败和错误请求不能作为有效操作处理。

存储记录校验失败返回 `500`，`detail.code=data_corruption`；存储连接、超时或操作失败返回 `503`，`detail.code=database_unavailable`。两者均返回不含数据与凭据的错误信息。存储失败不会作为空结果或模型提取降级的成功响应返回。

## 写入与读取

### POST /v1/messages

对话消息的统一入口，一次请求属于一个会话。

```json
{"space_id":"space-example","session_id":"chat-001","messages":[
  {"message_id":"m1","role":"user","content":"请制定迁移计划。","event_time":1793500000},
  {"message_id":"m2","role":"assistant","content":"建议先备份，再迁移。","event_time":1793500010}
]}
```

`message_id`、`role`、`content` 必填；role 仅支持 user/assistant，不接收流式碎片。每条独立保存，数组顺序成为会话持久化序号。`event_time` 可省略或为 null，表示来源时间未知。
`processing` 默认为 buffered，可显式 immediate；`extract` 默认为 `true`，设为 `false` 时只保存对话原文。可选 `agent_id` 记录摄入来源。

消息 ID 在空间和会话内唯一；同 ID 同输入返回原事件及当前状态，不重置空闲计时，不重复生成。不同输入返回 409/message_conflict；批内冲突在任何写入前拒绝。不同 ID 的相同正文允许存在。
自动处理先验证模型配置，再提交原文与任务。immediate 等待核心提交，buffered 返回当前操作状态。
单个会话的多个消息可能共享一个操作 ID，也可能关联不同操作。调用方通过返回的关联查询进度。

### POST /v1/remember

传 `space_id`、`content`，可选 `session_id`、`agent_id`、`extract` 和 `processing`。
`extract=false` 只保存原文，返回 `status="stored"`、`source_ids` 和空的 `operation_ids`、`operations`，无需模型。
`extract=true`（默认）请求自动抽取，`processing` 默认 immediate；选择 buffered 时在原文和任务提交后返回 `status="accepted"` 及操作回执。没有配置 LLM 时仍可手工添加事实。

### 缓冲处理状态与刷新

摄入返回 `source_ids`、`operation_ids` 和 `operations`。对话批次的 `messages` 项各自记录
`episode_id` 和关联的 `operation_ids`。通过 `GET /v1/operations/{operation_id}?space_id=…` 查询阶段，
通过 `POST /v1/operations/{operation_id}/retry` 携带 `space_id` 重试已有任务。
`GET /v1/processing` 按空间和可选的 `session_id` 分页浏览操作，返回 `operations` 和 `next_after_id`。`limit` 默认 100，范围 1–1000。下一页将响应的 `next_after_id` 作为 `after_id` 传入；游标为 `null` 或返回空页时结束。
`POST /v1/processing/flush` 可带 `session_id` 整理指定会话并等待本批核心处理。
独立设置 `include_digests` 可同时等待摘要。

### POST /v1/recall

单空间请求传 `space_id`。组合读取传非空且无重复的 `space_ids`，所有空间均通过授权后才执行。
`lean=true` 返回 `context`、估算 token 和检索覆盖状态。`context_sections` 中每段包含
`kind`、`text` 和 `space_id`。预算不足时跳过放不下的完整条目，并在 `limitations` 中返回
`context_budget`。组合结果按空间标记事实和摘要。画像通过独立入口读取。

单空间传 `lean=false` 搜索事实与摘要候选。返回 `status`、`facts`、`fact_ids`、`source_ids`、`sources` 和
`answer`。自由文本查询返回 `evidence_only` 或 `no_evidence`。同时传 `subject` 和 `predicate`
且在完整筛选视图中只有一个不同的值时，返回 `answered` 和该值。其他情况下 `answer` 为空。

单空间传 `lean=true, answer=true` 可显式调用问答模型，`answer_status` 区分 `answered`、
`no_evidence`、`model_not_configured`、`model_failed` 和 `empty`。回答失败时证据仍可读取。
HTTP 的 `as_of` 查询指定事件时间，仍考虑之后导入的历史证据。Python 直连可用 `FactQuery` 单独指定已知时间。


`POST /v1/recall` 使用混合召回，可传 `state`、`valid_at` 和 `known_at` 选择历史视图。显式时间与 `as_of` 互斥。

| 路径 | 请求与返回 |
| --- | --- |
| `POST /v1/aggregate` | 必填 `space_id` 和 `predicate`，`operation` 为 `count` 或 `count_distinct`。可传 `subject`、有效时间区间 `[start,end)`、事实状态与时间、`redact_sensitive`、`limit`。返回 `count`、证据 `facts`、摘要 `candidates` 及 `limitations`。空匹配时可传 `query` 检索摘要候选。 |
| `POST /v1/follow-path` | 必填 `space_id`、`anchor` 和 2–4 个有序 `predicates`，可传同样的事实状态、时间、脱敏与证据预算。返回 `paths`，每项含 `endpoint` 和支持路径的 `facts`，以及结果的 `limitations`。 |

## 会话与检查

`GET /v1/sessions` 返回无正文的会话索引。用 `GET /v1/sessions/report` 按会话审计来源和事实。
报告通过 `limit` 和 `offset` 分页，默认返回前 100 条事实，每页最多 500 条。继续读取时使用
`facts_page.next_offset`。每页的 `facts_added` 和 `facts_redacted` 都是对应会话的总数。
`GET /v1/stats` 和 `GET /v1/agent/status` 返回无正文的空间与会话统计。

## 记忆列表的筛选与一致分页

`GET /v1/memories` 支持：

| 查询参数 | 作用 |
| --- | --- |
| facts_limit、facts_offset | 事实分页 |
| episodes_limit、episodes_offset | 原始事件分页 |
| q | 搜索；HTTP 正式参数为 q |
| status | live/current、superseded/old/history、all；未知值按实现忽略 |
| kind | outcomes 或 attributes |
| outcome_kind | decision、finding、lesson、open_question |
| categories | 可重复传入，匹配任一标签 |
| include_sensitive | 默认 false；true 是所有者的完整检查视图 |
| expected_revision、evaluated_at | 后续页成对沿用首屏返回值，固定提交集合与状态评估时刻 |

返回包含事实、事件、计数及分页信息；`fact_categories` 描述相同类型及可见范围内的分类集合。默认安全视图不返回 profile、原始事件正文或摘要，计数不代表所有这些内容已经返回。

分页响应给出 `data_revision` 与 `evaluated_at`；下一页分别传为 expected_revision 和 evaluated_at。字段必须成对提供。版本发生变化返回 409，`detail.code="memory_revision_changed"`，应从第一页重取；不要继续累加旧结果。

## 事实与派生视图

| 端点 | 输入与语义 |
| --- | --- |
| `POST /v1/facts` | text、predicate、object 必填；subject=user，sensitive、categories、object_kind 可选；添加用户断言 |
| `PATCH /v1/facts/{fact_id}` | 内容修改同时提交非空 subject、predicate、object、text；sensitive、categories、object_kind 等元数据可单独提交；非法输入返回 422 |
| `DELETE /v1/facts/{fact_id}` | 删除单事实；检查响应中的 ok |
| `POST /v1/facts/clear-slot` | subject、predicate、expect_count 必填；受当前数量、来源和历史链保护 |
| `GET /v1/profile` | 七栏画像缓存、更新时间、待更新事实数与刷新状态 |
| `POST /v1/profile/refresh` | 请求体 space_id，返回 202 和画像刷新操作；后续查询或重试操作 |
| `GET /v1/graph` | as_of、include_sensitive=false、q、live_only=false、limit |
| `GET /v1/audit` | limit=40，检查图谱投影 |
| `GET /v1/focus` / `PUT /v1/focus` | track/mute 列表，省略保持，[] 清空 |
| `GET /v1/policy` / `PUT /v1/policy` | extract_instruction、extract_system、digest_system、profile_system；空字符串恢复默认 |
| `GET /v1/conflicts` | 待确认冲突，需要相应检测配置 |
| `POST /v1/conflicts/{conflict_id}/resolve` | keep=newer、older 或 both |

`track` 在检索时给匹配的事实加分，清空即可撤销。`mute` 即时从召回和画像中排除匹配内容，管理界面仍可查看原记录。
普通项按不区分大小写的词或短语匹配，拉丁词使用边界。例如 `Java` 不会命中 `JavaScript`。
使用 `category:<id>` 可显式匹配事实的分类。

画像条目的事实引用和计数与 Python API 共用[证据契约](python-api.md#profile-evidence)。

`GET /v1/audit` 返回 `checked`、`total_findings`、`by_kind`、`findings` 和 `truncated`。检查项为 `missing_edge`、`edge_mismatch` 和 `orphan_entity`，以记录 ID 定位。事实检查排除敏感事实，节点只返回 ID。统计按空间汇总后再截断。

`GET /v1/graph` 返回 `{nodes, edges, has_more}`，筛选顺序与完整性含义遵循 [Python 图谱契约](python-api.md)。`limit` 按筛选后的关系数计，显式正数上限最多为 500；省略时不限量。`has_more=true` 表示该查询结果被截断，不代表查询失败，也不包含全局总数。

内容编辑使事实变为用户来源；只改敏感标记、标签或对象类型保留原来源。槽位清理可能通过 JSON 结果表达拒绝，不能只检查 HTTP 状态。服务端权限不会被客户端筛选或隐私开关替代。

字段及编辑规则见[分类与对象类型](../architecture/memory.md#分类与对象类型)。新增或修改事实时也可指定 `cardinality` 与 `retention`。

`GET /v1/memories?categories=food&categories=music` 匹配任一标签，非法枚举返回 422。与其他事实筛选取交集，在分页前执行。原始事件、全局计数和分类候选不随标签筛选改变。后续页继续成对携带 `expected_revision` 和 `evaluated_at`。

## 导出与遗忘

| 端点 | 输入与语义 |
| --- | --- |

| `GET /v1/export` | include_sensitive=false；返回结构化导出 |
| `POST /v1/forget` | JSON `{"space_id":"…","confirm":true}`，清空指定空间并保留 ID 和设置 |

默认导出省略敏感事实及原文，显式设置 `include_sensitive=true` 获取私有导出，其中 `conflicts` 引用同一快照内的事实编号。

## OpenAI 兼容聊天

`POST /v1/chat/completions` 接受 model、非空 messages、stream 和带空间选择的 memory：

```json
{
  "model": "mnemotrace",
  "messages": [{"role": "user", "content": "Where do I live?"}],
  "memory": {"space_id": "space-example", "session_id": "demo", "recall": true, "remember": true, "n_digests": 20}
}
```

`memory` 还支持 `extract`、`as_of` 和 `redact_sensitive`。`remember` 控制是否保存对话，保存时 `extract` 控制是否抽取事实，默认 `true`。写入后响应通过 `mnemotrace.remember_extract` 返回该值。需要配置作答 LLM，否则返回 503；model 字段不负责动态选择服务器的实际后端。部分兼容参数会被接受但不参与生成，不能把此端点视为其他服务的全部接口实现。

stream=true 返回 SSE；当前先获得完整生成结果，再通过事件切分返回，不承诺模型 token 实时透传。原文和任务提交后才标记 remembered。普通响应和 SSE 结束帧中的 mnemotrace.operation_ids 用于查询实际处理结果。

## 错误处理

常见状态：400 输入或确认缺失；401 凭据缺失/无效；404 目标不存在或空间未授权；409 消息冲突、业务版本冲突或分页版本变化；413 请求过大；422 字段或编辑校验；503 就绪、配置、模型或控制台构建条件不满足。

错误通常位于 `detail`，可能是字符串、对象或字段错误列表。成功响应中的 queued、ok=false 等业务状态也需要处理。响应 schema 尚未覆盖全部载荷，应结合本页和[实现](../../mnemotrace/server/app.py)核对。

## 模型抽取状态与错误

自动抽取的 buffered 请求返回 `202`，表示原文和任务已持久化。immediate 核心结果已提交时返回 `200`。
`operations` 的每项含 `operation_id`、`kind`、`state`、`phase`、`stored_raw`、`extraction_saved`、`core_committed`、
`error_code` 及 `auxiliary_operation_ids`。摘要辅助任务有独立进度。
等待超时以 `processing_incomplete` 和 `timed_out` 返回当前操作与持久化标志，已接受的任务继续处理。
缺少抽取模型返回 `503 llm_not_configured`，原文未写入。

## 条件摘要与原文读取

`GET /v1/episodes/{episode_id}?space_id=<SPACE_ID>` 显式读取完整原文。未知空间、他人空间和不存在的事件返回 404。
响应包含 `id`、`space_id`、`session_id`、`role`、`agent_id`、`event_time`、`ingested_at` 和 `content`。

摘要进度通过核心操作的 `auxiliary_operation_ids` 查询。摘要失败不撤销已接受的原文和抽取。显式等待摘要时调用 `/v1/processing/flush` 并设置 `include_digests=true`，将摘要纳入本次等待范围；`retry_failed=true` 重试范围内的失败操作。

召回上下文只包含事实和摘要。默认最多返回 20 条摘要，`n_digests=0` 只使用事实。只保存原文的内容可按来源 ID、会话或导出读取。
