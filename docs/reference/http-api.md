# HTTP API

HTTP 服务是多应用接入和控制台管理的边界。启动方式见[部署说明](../../deploy/README.md)，字段定义与路由来源见 [app.py](../../mnemotrace/server/app.py)。

## 地址、鉴权与公共端点

产品 API 前缀为 `/v1`，请求和响应通常使用 JSON。受保护路由发送 `Authorization: Bearer <key>`，身份由服务器配置解析，不由请求体 user_id 决定。

- `MNEMOTRACE_API_KEYS` 按 `tenant:key` 映射，多个 key 可以属于同一 tenant。
- 开放模式必须显式启用；Bearer 文本本身成为 namespace，默认仍拒绝共享匿名访问。
- remember/recall 还支持 space principal 和 ACL。其他管理路由使用普通鉴权映射，不能假定只配置 space key 就能访问全部管理路由。
- key 必须为非空可打印 ASCII。生产部署使用足够长的随机凭据并通过 HTTPS 或受控网络传输。

| 公共端点 | 用途 |
| --- | --- |
| `GET /health` | 进程存活和运行配置概况，`ok=true` 不等于 ready |
| `GET /ready` | 就绪时 200，否则 503；健康门禁使用此端点 |
| `GET /metrics` | 聚合计数、延迟与 token 观测，不包含记忆正文 |
| `GET /docs`、`GET /openapi.json` | 框架生成的交互参考和 schema；不包含全部响应语义 |
| `GET /ui/` | 已构建的管理控制台，未构建时返回 503 |

HTTP 请求体默认上限为 2 MiB，可通过配置调整；实际接收字节与 Content-Length 都受检查。鉴权失败和错误请求不能作为有效操作处理。

## 写入与读取

### POST /v1/remember

| JSON 字段 | 默认值与含义 |
| --- | --- |
| content | 必填，1–1,000,000 字符 |
| session_id | `default`，1–512 字符 |
| scope | `auto`；支持 long/working |
| space | 未设置时写调用者 home；显式设置需有写权限 |

默认同步提取并保存。正常长期写入返回 `ok`、`scope`、`extracted` 等；异步模式可返回 `queued=true`、`episode_id`；提取降级可返回 `degraded`、`stored_raw=true`。working 路径返回工作条目 id 与事件 id。调用方应根据这些分支判断处理进度。

### POST /v1/recall

| JSON 字段 | 默认值与含义 |
| --- | --- |
| query | 必填，1–32,768 字符 |
| lean | true，返回上下文；false 请求直接事实答案 |
| n_chunks | 6，范围 0–50 |
| session_id | null，设定时可包含该 session 工作状态 |
| as_of | null；epoch 秒 |
| redact_sensitive | false；按当前敏感标记过滤相关返回内容 |
| spaces | 默认为 home；最多 16 个，每个均须可读 |

单 space 的 lean 路径返回 `context`、`tokens_est`、`as_of`、`redacted_sensitive`，HTTP 还可返回 `answer` 和 `full_tokens`。无作答模型时不应把回退内容视为通用模型答案。`lean=false` 返回 `answer` 和事实文本列表。

多 space 路径融合各自的上下文并保留来源信息，不保证与单 space 的答案或字段组合完全相同。`tokens_est/full_tokens` 是估算值，不等于所有模型调用的总计费 token。

## 会话与检查

| 端点 | 输入 | 主要用途 |
| --- | --- | --- |
| `POST /v1/sessions/close` | session_id=default、summarize=true、clear_working=true、outcomes=null | 整合与收尾，不删除原始事件 |
| `GET /v1/sessions` | limit、offset=0、q="" | 不含正文的会话索引和分页 |
| `GET /v1/sessions/report` | session_id 必填、include_sensitive=false | 某 session 的贡献与事实报告 |
| `GET /v1/agent/status` | session_id 可选 | 接线、计数和推荐操作，不返回记忆正文 |
| `GET /v1/stats` | 无 | 命名空间统计，不包含原文和存储路径 |
| `GET /v1/audit` | limit=40，归一到 1–200 | 启发式体检发现，不自动修复 |

close 返回处理计数，例如 `pending_consolidated`、`facts_added`、`summaries`、`working_cleared` 和 `outcomes`。结论提取需要 LLM；服务端 `MNEMOTRACE_SESSION_OUTCOMES=0` 会强制关闭，即使请求传 outcomes=true。

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
| category | 精确分类过滤；空分类归为“其他” |
| include_sensitive | 默认 false；true 是所有者的完整检查视图 |
| expected_revision、evaluated_at | 后续页成对沿用首屏返回值，固定提交集合与状态评估时刻 |

返回包含事实、事件、计数及分页信息；`fact_categories` 描述相同类型及可见范围内的分类集合。默认安全视图不返回 profile、原始事件正文或摘要，计数不代表所有这些内容已经返回。

分页响应给出 `data_revision` 与 `evaluated_at`；下一页分别传为 expected_revision 和 evaluated_at。字段必须成对提供。版本发生变化返回 409，`detail.code="memory_revision_changed"`，应从第一页重取；不要继续累加旧结果。

## 事实与派生视图

| 端点 | 输入与语义 |
| --- | --- |
| `POST /v1/facts` | predicate、object 必填；subject=user，sensitive/category 可选；添加用户断言 |
| `PATCH /v1/facts/{fact_id}` | 三元组字段或 statement，及 sensitive/category；statement 非空且不能与三元组字段混用 |
| `DELETE /v1/facts/{fact_id}` | 删除单事实；检查响应中的 ok |
| `POST /v1/facts/clear-slot` | subject、predicate、expect_count 必填；受当前数量、来源和历史链保护 |
| `GET /v1/profile` | 派生画像与关键事实 |
| `GET /v1/profile/structured` | 结构化画像的确认与待确认视图 |
| `GET /v1/graph` | as_of、include_sensitive=false、q、live_only=false、limit |
| `GET /v1/focus` / `PUT /v1/focus` | track/mute 列表，省略保持，[] 清空 |
| `GET /v1/policy` / `PUT /v1/policy` | extract_instruction、extract_system、summary_system、persona_system；空字符串恢复默认 |
| `GET /v1/conflicts` | 待确认冲突，需要相应检测配置 |
| `POST /v1/conflicts/{conflict_id}/resolve` | keep=newer、older 或 both |

内容编辑使事实变为用户来源；只改敏感标记或分类保留原来源。槽位清理可能通过 JSON 结果表达拒绝，不能只检查 HTTP 状态。服务端权限不会被客户端筛选或隐私开关替代。

## 工作记忆、导入与导出

| 端点 | 输入与语义 |
| --- | --- |
| `POST /v1/working` | content；session_id=default、kind=state、ttl_seconds 可选 |
| `GET /v1/working` | session_id 可选；仅返回活跃条目 |
| `DELETE /v1/working` | session_id 必填；清理该 session 的工作条目 |
| `POST /v1/import` | sessions 或 data + format；consolidate=null、summarize=true、dedupe=true |
| `POST /v1/documents` | text 或 data_base64；filename、content_type 可选，session_id=document |
| `GET /v1/export` | include_sensitive=false；返回结构化导出 |
| `POST /v1/forget` | JSON `{"confirm": true}`；当前也接受查询参数 confirm=true |

导入同时给 sessions 和 data 时以 sessions 为准。consolidate=null 按输入来源选择，Agent 会话与普通导出存在差异。PDF/DOCX 需要 documents extra，图片描述依赖视觉模型；base64 内容仍受请求体上限约束。

默认导出省略敏感事实与自由文本层；完整私有导出需要 include_sensitive=true。forget 永久清空命名空间；anonymous 和 mnemotrace-test 禁止整库清空。单事实删除和清理工作状态不会完整擦除原始会话。

## OpenAI 兼容聊天

`POST /v1/chat/completions` 接受 model、非空 messages、stream 和可选 memory：

```json
{
  "model": "mnemotrace",
  "messages": [{"role": "user", "content": "Where do I live?"}],
  "memory": {"session_id": "demo", "recall": true, "remember": true, "n_chunks": 6}
}
```

memory 还支持 scope、as_of 和 redact_sensitive。需要配置作答 LLM，否则返回 503；model 字段不负责动态选择服务器的实际后端。部分兼容参数会被接受但不参与生成，不能把此端点视为其他服务的全部接口实现。

stream=true 返回 SSE；当前先获得完整生成结果，再通过事件切分返回，不承诺模型 token 实时透传。记忆写入在响应之外安排，响应中的 remembered 标志表示已安排该路径，不能代替后续审计。

## 错误处理

常见状态：400 输入或确认缺失；401 凭据缺失/无效；403 权限或受保护擦除；404 目标不存在；409 记忆分页版本变化；413 请求过大；422 字段或编辑校验；503 就绪、配置、模型或控制台构建条件不满足。

错误通常位于 `detail`，可能是字符串、对象或字段错误列表。成功响应中的 queued、degraded、ok=false 等业务状态也需要处理。响应 schema 尚未覆盖全部载荷，应结合本页和[实现](../../mnemotrace/server/app.py)核对。
