# HTTP API

生产模式将 Bearer `key` 映射到稳定租户空间，同租户可轮换多个 `key`，不同空间隔离。Base URL 示例为 http://127.0.0.1:8000，部署与网关要求见 [deploy](../../deploy/README.md)。接口实现见 [server/app.py](../../mnemotrace/server/app.py)。

## 鉴权与健康

受保护模式下，所有 `/v1/*` 接口携带 `Authorization: Bearer <key>`，JSON 请求设置 `Content-Type: application/json`。`key` 为非空可打印 ASCII，租户名称可以中文。非法凭据返回 401，非法服务配置使鉴权和 readiness 返回 503，错误不回显 `key`。

生产默认失败关闭；开放模式及匿名访问分别需要显式开关。健康检查不鉴权：`/health` 是存活与诊断入口，`/ready` 仅在鉴权与存储可接流量时返回 200，否则为 503。健康结果不含 `Key` 或记忆正文。

## 写入

`POST /v1/remember`：

```json
{
  "content": "I work at Acme.",
  "session_id": "app:my-app:thread-123",
  "scope": "long"
}
```

`scope` 可为 `auto`（默认）、`long`、`working`；可选 `space` 选择获准写入的空间，省略时使用凭据的 `home`。自动瞬态路由和长期提取是不同阶段；原始 `Episode` 始终保存。`POST /v1/facts` 使用 `subject`/`predicate`/`object` 和可选 `sensitive`/`category` 记录用户明确断言，不能被自动抽取悄悄覆盖。

## 召回与会话

`POST /v1/recall`：

```json
{
  "query": "Where do I work?",
  "lean": true,
  "n_chunks": 4,
  "as_of": null,
  "redact_sensitive": false
}
```

单空间 `lean=true` 返回 `context`、`tokens_est`、`full_tokens` 和 `answer`；`lean=false` 返回直接事实答案及 `facts`，不提供相同的上下文字段。`n_chunks` 默认 6，HTTP 范围为 0–50。token 字段为估算，不能替代模型计费量或 benchmark 总成本。`as_of` 是单个 epoch 秒条件，不提供独立现实时间/事务时间参数。

可选 `spaces` 读取授权空间列表，省略时只读 `home`；每个空间都需授权。多空间请求走融合上下文路径，不按 `lean=false` 切换成单空间事实搜索。多空间响应含 `context`、`tokens_est` 和 `spaces`，不能依赖单空间回答字段。

`redact_sensitive=true` 只返回已分类的非敏感结构化事实，省略画像、摘要和原始对话。它不等同于对全部自由文本完成隐私审计。

`POST /v1/sessions/close`：

```json
{
  "session_id": "app:my-app:thread-123",
  "summarize": true,
  "clear_working": true
}
```

关闭会话整理待处理记录、补齐摘要、清理工作记忆并保存，不删除原文。配置 LLM 且允许 `outcomes` 时蒸馏四类结论；请求 `outcomes=false` 可关闭本次，环境 `MNEMOTRACE_SESSION_OUTCOMES=0` 强制关闭。无 LLM 不执行该分支。精确去重不保证同义结论幂等。

`GET /v1/sessions/report?session_id=...` 审计该会话保存的事实，默认隐藏敏感正文；完整私有审计才选择 `include_sensitive=true`。`GET /v1/agent/status?session_id=...` 只返回空间、计数、focus 和下一步建议，不含记忆正文。

## OpenAI-compatible chat

`POST /v1/chat/completions` 接受 memory 扩展：

```json
{
  "model": "mnemotrace",
  "messages": [
    {
      "role": "user",
      "content": "Continue the project."
    }
  ],
  "memory": {
    "session_id": "app:my-app:thread-123",
    "recall": true,
    "remember": true,
    "scope": "auto",
    "n_chunks": 6,
    "as_of": null,
    "redact_sensitive": false
  }
}
```

`memory.recall=false` 可关闭召回，`memory.remember=false` 可关闭写回；`scope` 选择长期或工作记忆路由，`n_chunks` 控制原文数量，`as_of` 传递时点，`redact_sensitive=true` 限制为非敏感结构化事实。

回答前召回，响应生成后在后台保存用户轮次；响应的 `remembered` 表示已安排后台写入，不代表已完成持久化。客户端仍需在会话结束时调用 close。`Python/TypeScript` 应用的接入和生命周期见 [Agent 指南](../guides/agents.md)。

## 查看与管理

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/v1/sessions` | `limit`/`offset`/`q`；不含正文的会话索引 |
| GET | `/v1/stats` | 不含正文的计数、积压、冷热及后端状态 |
| GET | `/v1/memories` | 默认非敏感视图，筛选和一致分页见下文 |
| GET | `/v1/profile/structured` | 从事实派生的结构化画像 |
| GET | `/v1/graph` | 默认排除敏感事实对应的边，完整私有图需 `include_sensitive=true` |
| GET | `/v1/conflicts` | 需要开启冲突检测的待确认候选 |
| POST | `/v1/conflicts/{id}/resolve` | keep=`newer/older/both` |
| PATCH | `/v1/facts/{id}` | 三元组、元数据或四类结论的 `statement` |
| DELETE | `/v1/facts/{id}` | 显式擦除指定事实 |
| GET | `/v1/audit` | 只读发现、全部计数与截取结果 |
| POST | `/v1/facts/clear-slot` | `subject`/`predicate`/`expect_count`；仅删除 `live` 非手写槽位事实，数量变化整体拒绝 |
| GET | `/v1/export` | 默认非敏感 facts+graph；完整私有导出需 `include_sensitive=true` |
| POST | `/v1/forget` | confirm=true；擦除授权空间 |

### 记忆列表的筛选与一致分页

`GET /v1/memories` 在分页前组合 `kind`、`outcome_kind`、`status`、`category`、`q` 与敏感可见范围。
`outcome_kind` 仅允许 `decision`、`finding`、`lesson`、`open_question`，未传表示不限制，非法值返回 422。
事实按 `(valid_at, id)` 倒序，原始对话按 `(event_time, id)` 倒序；同一时间的顺序确定。

- `facts_limit` / `facts_offset`、`episodes_limit` / `episodes_offset` 分别控制两类列表；显式 `limit` 上限为 500 / 200，0 表示不取该类记录，未传 `limit` 保留不分页的全文语义。
- `facts_page` / `episodes_page` 含 `items`、`total`、`offset`、`limit`、`has_more`、`next_offset`；只按服务端 `next_offset` 前进，末页该值为 null。每类页面的 total 与记录来自同一匹配集合；`counts` 仍为原有空间级统计，不随搜索和分类收缩。
- 首次请求省略 `expected_revision` 和 `evaluated_at`，响应返回不透明 `data_revision` 与数值 `evaluated_at`（Unix 秒）。后续页成对传回这两个值。缺一项、负数或非有限时间返回 422。
- 同一批分页的 `live`/历史过滤、事实行 `status` 和 `counts.facts_live` / `facts_superseded` / `facts_outcomes` 都按首次评估时刻判定。自然时间流逝不改变当前批次；重新从第一页省略上下文请求才取得新时刻。
- 后续请求的提交版本不符时返回 HTTP 409：`{"detail":{"code":"memory_revision_changed","message":"记录已变化，请重新加载"}}`，不附带可追加页面。调用方应保留已有展示，停止追加，并由用户重新加载；不自动重试旧上下文。

版本保护适用于通过标准 `MemoryService` 持久化写入路径提交的变化；`"empty"` 代表尚无持久化提交。
裸 `Memory` 的未保存修改不受此协议保护。该接口不保留历史快照，也不提供相互独立的有效时间与事务时间查询。

TypeScript SDK 使用 camelCase 请求选项，响应保持 HTTP 字段名；完整遍历示例：

```typescript
import { MnemoTraceClient } from 'mnemotrace-memory'

const client = new MnemoTraceClient({ apiKey: 'synthetic-key' })
let offset = 0
let revision: string | undefined
let evaluatedAt: number | undefined
const ids = new Set<string>()
for (;;) {
  const page = await client.memories({
    factsLimit: 60, factsOffset: offset, episodesLimit: 0,
    kind: 'outcomes', outcomeKind: 'lesson',
    expectedRevision: revision, evaluatedAt,
  })
  revision = page.data_revision
  evaluatedAt = page.evaluated_at
  for (const fact of page.facts) ids.add(fact.id)
  if (!page.facts_page.has_more) break
  offset = page.facts_page.next_offset!
}
```

SDK 非成功响应抛出 `MnemoTraceError`；409 的机器错误码保留在错误的 `detail.detail.code` 中，SDK 不自动重试。

### 事实编辑

整句编辑的 `statement` 仅适用于已有谓词为 `decision`、`finding`、`lesson`、`open_question` 的
会话结论；属性事实必须通过 `subject`、`predicate`、`object` 修正。对属性事实提交 `statement`
（即使与原文相同）、空白 `statement` 或混合两种内容字段，均返回 HTTP 422，不修改事实或图谱。
结论的整句更新保存到 `object`/`text`/`display`，并保留依据子句；不调用模型猜测三元组。
事实 ID、来源 `Episode` ID 沿用现有编辑语义。编辑响应包含当前 `display`；审计的单条发现也新增
`display`，前端优先使用它。审计 `samples[].text` 是已保存的事实内容样本，不是来源对话逐字原文。

### 记忆体检

`GET /v1/audit` 按默认处理优先级稳定排序后再截取 `limit`：红色（缺少信息/检索支持不足）、
琥珀色（异常标识/多个取值/内容不完整/指代不明）、蓝色（内容过长/代码路径）、灰色（未关联对象）。
同级保留原发现顺序；`by_kind` 和 `total_findings` 仍反映全部发现。

### 结构化画像的依据

`GET /v1/profile/structured` 的条目 `evidence` 包含：

- `kind`：`user`（用户设定）、`mentions`（来源提及）或 `reinforced`（访问强化）。
- `count`：保留既有语义；`user` 为 1，`mentions` 为来源数（旧的无来源兜底为 1），
  `reinforced` 为访问次数。
- `source_count`：关联的去重来源 `Episode` ID 数量，无来源时为 0；画像以 `xN` 展示。
  用户设定和访问强化不替代这个数量，手动设定以带悬浮提示的 UserCheck 图标同时展示。

控制台位于 `/ui/`，未构建时返回 503，HTTP API 与健康检查独立工作。错误、并发、目录与凭据边界见[服务架构](../architecture/service.md)。
