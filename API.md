# 忆痕记忆服务 · 接口文档

一套多租户的长期记忆 HTTP API：生产模式下，**API key 映射到稳定租户命名空间**，同一租户可配置多个 key
做无停机轮换；不同租户互相隔离。显式开发开放模式才把 Bearer 文本本身当作命名空间。

---

## 0. 自己部署（数据完全本地）

从本仓库源码检出的根目录运行下列命令；发行包尚未发布到 PyPI。
```bash
cp deploy/.env.example deploy/.env
# 把 deploy/.env 的 MNEMOTRACE_API_KEYS 示例值换成 tenant:强随机密钥
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
curl -fsS http://127.0.0.1:8000/ready
```
> 生产不要设置 `MNEMOTRACE_OPEN`/`MNEMOTRACE_ALLOW_ANONYMOUS`。直接 Python 部署时，
> 在本检出根目录运行 `pip install -e '.[server]'`，设置 `MNEMOTRACE_API_KEYS="alice:key-a,bob:key-b"` 后运行 Uvicorn。
> 同一租户轮换：`alice:key-new,alice:key-old`；一个 key 不允许映射到多个租户。

网页控制台仅使用构建后的 React 应用。源码部署未生成 `frontend/dist` 时，`/` 与 `/ui/*`
返回 503 和构建命令，不提供简易增删改网页；API、`/health` 和 `/ready` 继续独立工作。
在 `frontend` 目录执行 `pnpm install --frozen-lockfile`、`pnpm run build` 后重启服务。
Docker 构建会自动生成前端。

跨 Claude Code / Codex / Cursor / 自研 agent 的推荐生命周期见
[`docs/cross-agent-memory.md`](docs/cross-agent-memory.md) 和
[`docs/agent-adapters.md`](docs/agent-adapters.md)：开工先看 agent status，回答前 recall，长期事实 remember，
线程结束 close session，再用 session report 审计这一轮保存了什么。单会话生命周期自测（本地零服务或 HTTP）见
[`examples/cross_agent_lifecycle.py`](examples/cross_agent_lifecycle.py)。

### API 嵌入模型：BGE-M3 / 1024

零配置默认使用离线 hashing。直接 Python 部署时，安装 API 嵌入依赖：

```bash
python -m pip install -e '.[server,llm]'
```

在服务进程的环境中设置：

```text
MNEMOTRACE_EMBEDDER=openai:
OPENAI_BASE_URL=<提供 BAAI/bge-m3 的 OpenAI 兼容接口地址>
OPENAI_API_KEY=<该供应商的凭据>
```

`openai:` 省略模型名时使用 `openai/BAAI/bge-m3`，也可显式写成
`MNEMOTRACE_EMBEDDER=openai:openai/BAAI/bge-m3`。这里的 `openai/` 表示 LiteLLM 的接口路由，
实际模型为 BGE-M3；供应商凭据与 MnemoTrace 的登录 key 不同。容器部署需把这些供应商环境
变量显式注入容器，仅写在宿主机环境或 Compose 的插值文件中不会自动传入。

BGE-M3 在启动时固定为 **1024 维**；响应维度不符会报错，不会动态修改维度。其他 API 模型
通过 Python SDK 使用时须显式提供实际维度，例如 `make_embedder("openai:<model>", dim=...)`，
不再提供 1536 的通用猜测。切换已有存储的模型前先备份并显式重嵌入，不能仅把 manifest
中的维度改为 1024。完整验证与边界见 [重构验收](results/api_bge_1024_refactor_validation.md)。

---

## 1. 鉴权
所有 `/v1/*` 接口都要带：
```
Authorization: Bearer <你的key>
Content-Type: application/json
```

key 必须是非空、可打印 ASCII（英文、数字及常见符号），不能包含中文、重音字符或控制字符；
租户 ID 和空间名称可以用中文。`MNEMOTRACE_API_KEYS` 与 `MNEMOTRACE_SPACES` 都校验 key。
非法 key 配置使 `/ready` 返回 503，相关鉴权请求返回 503；请求中不支持的凭证字符返回 401。
错误响应不回显 key。配置解析保留原有首尾空白处理与分隔符语法，不自动编码或转换 key。

健康检查不需要鉴权：
```bash
curl -s $B/health
curl -f $B/ready
```
`/health` 是 liveness/诊断，始终返回不含密钥/用户内容的运行形态；`/ready` 只有在鉴权和存储可接流量时
返回 200，否则返回 503：
```json
{
  "ok": true,
  "ready": true,
  "service": "mnemotrace",
  "version": "0.1.0",
  "auth_mode": "api_keys",
  "anonymous_allowed": false,
  "embedder": "HashingEmbedder",
  "llm_configured": false,
  "answerer_configured": false,
  "storage": "memory",
  "users_hot": 0,
  "max_hot_users": 64
}
```

## 2. 写入记忆

### 2.1 灌对话（自动抽取事实）— 主用
`POST /v1/remember`
```json
{ "content": "用户：我在字节跳动做后端，最喜欢周杰伦。\n助手：好的，记住啦。",
  "session_id": "s1",          // 同一会话用同一个 id（可选）
  "scope": "auto" }            // auto(默认) | long(强制长期) | working(临时,不进长期)
```
返回：`{"ok":true,"extracted":2,"total_facts":N}`，或临时记忆 `{"ok":true,"scope":"working",...}`。
> `scope=auto` 会把"今天嗓子不舒服"这类临时状态自动路由到工作记忆（不污染长期画像，但原始对话仍按日期留存、可查询）。批量灌长期记忆建议 `scope:"long"`。

### 2.2 直接写结构化事实（已抽取好的）
`POST /v1/facts`
```json
{ "subject": "user", "predicate": "works_at", "object": "字节跳动" }
```
> 手动写/改的事实标记为"用户设定 🔒"，权威，不会被自动抽取覆盖。

## 3. 检索 / 测效果

### 3.1 召回 + 答题 + 省 token 对比 — 主用
`POST /v1/recall`
```json
{ "query": "我最喜欢哪个歌手", "lean": true, "n_chunks": 4, "as_of": null, "redact_sensitive": false }
```
`as_of` 可选，传 epoch 秒时返回该历史时刻的记忆视图。
`redact_sensitive=true` 时只返回已分类的非敏感结构化事实，不包含画像、摘要或原始对话片段，适合
共享上下文或注入第三方模型。
返回：
```json
{ "answer": "你最喜欢周杰伦。",
  "context": "USER PROFILE:...\nFACTS:...\nRELEVANT CONVERSATIONS:...",  // 喂给模型的精炼上下文
  "tokens_est": 9958,        // 精炼上下文 token
  "full_tokens": 81408 }     // 整段历史 token（full-context 基线）→ 省 8×
```

### 3.2 结束会话整理 — agent 客户端收尾
`POST /v1/sessions/close`
```json
{ "session_id": "codex:mnemotrace:thread-123",
  "summarize": true,
  "clear_working": true }
```
用于 Claude Code / Codex / Cursor 这类客户端在一个线程结束或切换任务时调用。它不会删除原始对话，
而是补齐这个 session 的待整理 episode、生成缺失摘要、把知识更新反映到摘要层、清掉该 session 的
临时 working memory，并保存到磁盘。

配置了 LLM 时，关闭会话还会把这段会话蒸馏成结论事实（`decision` / `finding` / `lesson` /
`open_question`，谓词即结论类型、主语是 session id），返回体里的 `outcomes` 是本次新增的结论条数；
重复关闭同一个未变化的 session 不会重复写入。这是**每次关会话花一次 LLM 调用**的默认行为：
请求体可传 `"outcomes": false` 单次关闭，运维要设成本上限则设 `MNEMOTRACE_SESSION_OUTCOMES=0`
（服务端强制关闭，请求体的 `outcomes: true` 也无法解除）。没有配置 LLM 时该分支不执行，`outcomes` 恒为 0。

如需审计这一轮到底写入了哪些长期事实：
`GET /v1/sessions/report?session_id=codex:mnemotrace:thread-123`

默认 `include_sensitive=false`，敏感事实正文会被替换为 `[redacted sensitive fact]`；用户明确要完整私有审计时再传
`include_sensitive=true`。

推荐生命周期：
```text
会话开始 / 接线自检：GET /v1/agent/status?session_id=...
回答前：POST /v1/recall
用户说了稳定偏好、项目约定、长期事实：POST /v1/remember
会话结束 / 切换任务：POST /v1/sessions/close
需要审计保存结果：GET /v1/sessions/report?session_id=...
```

### 3.3 Agent 接线状态 — 不返回记忆正文
`GET /v1/agent/status?session_id=codex:mnemotrace:thread-123`

返回当前 key/namespace、session 计数、working memory 数、focus 策略、长期记忆计数和建议下一步调用。
它不返回 profile、fact 文本、原始对话或摘要，适合 agent 开工前确认自己接到了正确的记忆层。

### 3.4 OpenAI-compatible chat 的 memory 扩展
`POST /v1/chat/completions` 可在请求体里带：
```json
{ "model": "mnemotrace",
  "messages": [{ "role": "user", "content": "继续上次 benchmark 的事" }],
  "memory": {
    "session_id": "codex:mnemotrace:thread-123",
    "recall": true,
    "remember": true,
    "scope": "auto",
    "n_chunks": 6,
    "as_of": null,
    "redact_sensitive": false
  } }
```
这样已有 OpenAI SDK 应用只要换 `base_url`，就能在回答前自动召回、回答后把用户轮次写回同一个
agent session。

## 4. 查看 / 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/agent/status?session_id=...` | Agent 开工自检：namespace/session/focus/counts/下一步建议；不含记忆正文 |
| GET | `/v1/sessions?limit=20&q=codex` | 跨 agent/app 会话索引：session id、时间、episode/fact/working 计数；不含正文 |
| GET | `/v1/sessions/report?session_id=...` | 审计某个 session 写入了哪些长期事实；默认隐藏敏感事实正文 |
| GET | `/v1/stats` | 内容无关统计：episode pending/consolidated、hot/cold facts、cold page-in/out、时间范围、敏感事实数量、pending conflicts、运行后端；适合监控 |
| GET | `/v1/memories` | 默认安全视图：非敏感 facts + 数量；完整私有查看需 `include_sensitive=true`；`kind=outcomes` 只看会话结论、`kind=attributes` 只看属性事实（未知值忽略，与 `status` 同语义）；`category` 按分类精确筛选，空分类归入“其他”；返回的 `fact_categories` 为同空间、同类型及敏感可见范围内的完整分类候选，不受状态、搜索或分页限制 |
| GET | `/v1/profile/structured` | 结构化用户画像(基本信息/偏好/习惯) |
| GET | `/v1/graph` | 默认安全关系图：排除敏感 fact 对应的边；完整私有图需 `include_sensitive=true` |
| GET | `/v1/conflicts` | 待确认的疑似冲突(LLM 检测，需开 `MNEMOTRACE_CONFLICT_DETECTION=1`) |
| POST | `/v1/conflicts/{id}/resolve` | `{"keep":"newer\|older\|both"}` 让冲突由人确认 |
| PATCH | `/v1/facts/{id}` | 属性事实通过 `subject`、`predicate`、`object` 修正；会话结论还支持整句 `statement`；实际修改内容后标为用户设定，仅修改敏感/分类或原样保存时保留原来源 |
| DELETE | `/v1/facts/{id}` | 删一条 |
| GET | `/v1/audit?limit=60` | 记忆体检：逐条列出值得修的事实（机器 token、空值、未归约断言、孤立实体等）以及按槽分组的 `slot_overflow`；只读，不改任何数据 |
| POST | `/v1/facts/clear-slot` | **不可逆**：`{"subject":"user","predicate":"occupation","expect_count":84}` 硬删除该 `(subject, predicate)` 槽上的全部 live 事实。用于处理 `slot_overflow`；`expect_count` **必填**，是乐观并发校验（与体检时的条数不一致则整体拒绝、不删除任何行）——无保护的批量删除是这个端点唯一不该提供的形态，因此没有省略它的写法。已被 supersede 的历史事实、以及 `source="user"`（你亲手写过或就地改过的）事实都不在删除范围内 |
| GET | `/v1/export?include_sensitive=false` | 安全结构化导出：仅非敏感 facts + graph；不含画像、摘要、原始对话 |
| POST | `/v1/forget` | 需 `{"confirm":true}`；清空该 key 的全部记忆（不可逆） |

整句编辑的 `statement` 仅适用于已有谓词为 `decision`、`finding`、`lesson`、`open_question` 的
会话结论；属性事实必须通过 `subject`、`predicate`、`object` 修正。对属性事实提交 `statement`
（即使与原文相同）、空白 statement 或混合两种内容字段，均返回 HTTP 422，不修改事实或图谱。
结论的整句更新保存到 object/text/display，并保留依据子句；不调用模型猜测三元组。
事实 ID、来源 Episode ID 沿用现有编辑语义。编辑响应包含当前 `display`；审计的单条发现也新增
`display`，前端优先使用它。审计 `samples[].text` 是已保存的事实内容样本，不是来源对话逐字原文。

`GET /v1/audit` 按默认处理优先级稳定排序后再截取 `limit`：红色（缺少信息/检索支持不足）、
琥珀色（异常标识/多个取值/内容不完整/指代不明）、蓝色（内容过长/代码路径）、灰色（未关联对象）。
同级保留原发现顺序；`by_kind` 和 `total_findings` 仍反映全部发现。

`GET /v1/profile/structured` 的条目 `evidence` 包含：

- `kind`：`user`（用户设定）、`mentions`（来源提及）或 `reinforced`（访问强化）。
- `count`：保留既有语义；`user` 为 1，`mentions` 为来源数（旧的无来源兜底为 1），
  `reinforced` 为访问次数。
- `source_count`：关联的去重来源 Episode ID 数量，无来源时为 0；画像以 `xN` 展示。
  用户设定和访问强化不替代这个数量，手动设定以带悬浮提示的 UserCheck 图标同时展示。

## 5. 控制台（可视化）
浏览器开 **`<Base URL>/ui/`** → 输入你的 key → 看「画像 / 会话结论 / 事实管理 / 时间线 / 关系图谱 / 记忆问答 / 冲突待确认 / 记忆体检」。

---

## 6. 批量灌入（脚本）
把数据整理成一个 JSON 文件（一个数组，每条是对话或事实），用配套脚本一键灌：
```bash
python examples/ingest_client.py \
  --base http://127.0.0.1:8000 --key demo-test \
  --data your_data.json --reset
```
数据格式见 `examples/ingest_client.py` 顶部说明（支持"原始对话"和"结构化事实"两种记录）。

---

## 7. 最小可跑示例（curl）
```bash
B=http://127.0.0.1:8000 ; K=demo-test
# 灌
curl -s -X POST $B/v1/remember -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"content":"我在字节跳动做后端，最喜欢周杰伦。","scope":"long"}'
# 测
curl -s -X POST $B/v1/recall -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"query":"我喜欢哪个歌手"}'
```
