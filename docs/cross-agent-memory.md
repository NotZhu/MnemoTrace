# 跨 Agent 记忆层

MnemoTrace 的产品定位不是又一个聊天日志。它是用户自己的 memory（记忆）层，可被多个 agent
共享：Claude Code、Codex、Cursor、一个本地脚本，或一款托管应用，都应当读写同一份持久的 memory
namespace（命名空间）。

针对特定客户端的复制粘贴式搭建配方位于
[`agent-adapters.md`](agent-adapters.md)。

## 心智模型

```text
user namespace
  global memory              stable user preferences, identity, standing instructions
  project memory             repo/product decisions, benchmark rules, architecture notes
  session/thread provenance  where each memory came from
  working memory             short-lived state for the current session only
```

Bearer key 选定 namespace。`session_id` 是 provenance（溯源）与生命周期作用域；它不是一个独立的
"大脑"。用户的各个 session 通常应当汇入同一个 namespace，这样未来的 agent 才能检索到正确的那一部分。

## 客户端生命周期

每个 agent 适配器都应实现这个循环：

```text
0. Start / wiring check
   agent_status(session_id)    # content-free: namespace/session/focus/counts/next actions

1. Before relevant work
   recall(query, session_id)

2. During work
   remember(durable fact/preference/decision, session_id, scope="auto" or "long")

3. End thread / switch task
   close_session(session_id)

4. Optional user-visible audit
   session_report(session_id) # what this session saved, sensitive facts redacted by default
```

不要每次 `remember` 调用都去问用户。适配器应当记住稳定的事实、偏好、决策、项目规则、bug 结论以及架构要点。
瞬态状态可以用 `scope="working"` 存储，或交给 `scope="auto"` 路由处理。

无需服务器即可演练整个循环：

```bash
python examples/cross_agent_lifecycle.py --local
```

## 跨 Agent 交接

核心产品行为是：一个 agent 可以写入，另一个 agent 之后可以从中受益：

```text
Codex session
  remember("Project decision: the launch checklist must include committed eval logs.",
           session_id="codex:mnemotrace:handoff-source")
  close_session("codex:mnemotrace:handoff-source")

Claude Code session
  recall("What launch checklist decision did the previous agent record?",
         session_id="claude-code:mnemotrace:handoff-target")
```

两个 session 必须使用同一个用户 namespace，例如同一个 Bearer key。session ID 是 provenance（溯源）与
清理作用域；它们不会创建独立的大脑。你可以先运行一个零服务的本地冒烟测试：

```bash
python examples/cross_agent_handoff.py --local
```

要测试真实的 HTTP 服务：

```bash
python examples/cross_agent_handoff.py --base http://localhost:8000 --key me --project mnemotrace
```

## Namespace 策略

每个用户拥有的 memory 空间使用一个 namespace。

对于个人本地搭建：

```text
MNEMOTRACE_API_KEY=me
```

对于本地 MCP，每个 agent 可以启动自己的 `python -m mnemotrace.mcp` stdio 进程。只要这些进程指向同一个
namespace 与 `MNEMOTRACE_DATA_DIR`，当另一个进程保存了更新的 manifest 时，MnemoTrace 就会刷新热内存缓存，
这样已在运行的目标 agent 能在下一次 recall/status 调用时看到源 agent 的最新写入。本地写事务也按
namespace 串行化，因此两个 agent 同时保存时不会互相覆盖对方的 read-modify-save 快照。

对于托管的多租户服务：

```text
Authorization: Bearer <user-private-key>
```

要对项目做更严格的隔离，要么使用不同的 key，要么把项目身份编码进 session id。优先采用共享用户
namespace 加上项目感知的 `session_id`，因为跨项目的用户偏好仍应迁移过来。

## Session ID 策略

使用稳定、可检查的 ID：

```text
claude-code:<repo-name>:<thread-id>
codex:<repo-name>:<thread-id>
cursor:<workspace-name>:<thread-id>
app:<product-name>:<conversation-id>
```

示例：

```text
claude-code:mnemotrace:2026-06-26T10-30
codex:mnemotrace:thread-abc123
cursor:mnemotrace-frontend:issue-42
```

## MCP 集成

安装命令在本仓库根目录运行；发行包尚未发布到 PyPI。

对 Claude Desktop、Claude Code、Cursor，以及其他支持 MCP 的 agent 使用 MCP：

```bash
pip install -e '.[mcp]'
python -m mnemotrace.mcp
```

Agent 策略：

```text
At session start or when debugging memory wiring:
  mnemotrace_agent_status(session_id)

Before answering project/user-history-dependent requests:
  mnemotrace_recall(query, max_chunks=6)

When the user states a durable preference, project decision, or reusable fact:
  mnemotrace_remember(content, session_id, scope="long" or "auto")

When the user states temporary task/session state:
  mnemotrace_remember(content, session_id, scope="working")

When the user says to focus on or suppress a class of memories:
  mnemotrace_set_focus(track=[...]) or mnemotrace_set_focus(mute=[...])

When the thread ends or switches tasks:
  mnemotrace_close_session(session_id)
```

对于托管的 MnemoTrace 服务：

```bash
MNEMOTRACE_API_URL=http://localhost:8000 MNEMOTRACE_API_KEY=me python -m mnemotrace.mcp
```

在接入真实 agent 之前，校验该 agent 将要启动的确切 Python 运行时：

```bash
mnemotrace-agent-doctor --client codex --python /path/to/python
```

个人工作站的快速通道：

```bash
mnemotrace-agent-bootstrap --local --dry-run --namespace me --python /path/to/python
mnemotrace-agent-bootstrap --local --namespace me --python /path/to/python
mnemotrace-agent-bootstrap --local --install-policy --namespace me --python /path/to/python
```

bootstrap 命令会安装 Codex + 项目的 `.mcp.json`，运行 doctor，校验已安装的配置文件，并打印 AGENTS.md
策略文本。加上 `--install-policy` 可把该策略写入 AGENTS.md 中的一段受管理 block；`--uninstall-policy`
只移除 MnemoTrace 那段 block。它支持 `--bootstrap-targets codex` 或 `--bootstrap-targets mcp-json`
用于部分搭建。

对于 HTTP 服务模式，使用相同的流程，但用 `--api-url http://localhost:8000 --api-key me` 代替
`--local --namespace me`。

对于 Codex，`mnemotrace-agent-setup --install-codex --dry-run ...` 会预览配置变更，`--install-codex --doctor`
会写入 `[mcp_servers.mnemotrace]`（带备份）并立即校验 MCP 运行时，`--uninstall-codex` 只移除那段 MnemoTrace block。
对于 Claude Code、Cursor，以及其他项目级 MCP 客户端，`mnemotrace-agent-setup --install-mcp-json` 会把一个
`mnemotrace` 服务写入 `.mcp.json`，并提供相同的 dry-run、backup、doctor 与 uninstall 流程。

doctor 会使用一个临时本地 namespace，针对同一个本地 data dir 启动两个真实的 `python -m mnemotrace.mcp`
stdio 服务，可选地检查 `~/.codex/config.toml` 与 `.mcp.json`，并校验 MCP 工具能否预加载目标 session、
从源 session 记住内容、关闭它、报告它所保存的内容、导出一份可安全共享的 memory 负载、从已在运行的目标
服务 recall 源端的 memory、设置 focus、关闭两个 session，并至少持久化一条实时 fact。加上 `--api-url`
与 `--api-key` 还能一并校验远程 MnemoTrace 服务与用户 namespace：

```bash
mnemotrace-agent-doctor --client codex --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me \
  --codex-config ~/.codex/config.toml \
  --mcp-json ./.mcp.json
```

## 用户所有权循环

Agent 应当把 memory 视为用户拥有的状态，而不是隐藏的模型状态：

```text
Inspect:
  mnemotrace_list_sessions(response_format="json")
  mnemotrace_list_facts(response_format="json")

Correct one memory:
  mnemotrace_update_fact(fact_id, object="new value")

Delete one memory:
  mnemotrace_delete_fact(fact_id, confirm=true)

Tune recall behavior:
  mnemotrace_get_focus()
  mnemotrace_set_focus(track=["project decisions"], mute=["health details"])

Export:
  mnemotrace_export(response_format="json")                  # share-safe by default
  mnemotrace_export(include_sensitive=true, response_format="json")  # explicit private export

Erase everything:
  mnemotrace_forget(confirm=true)
```

当用户纠正一条 memory 时，使用精确的 fact 更新/删除。把整个 namespace 的擦除保留给明确的"遗忘一切"请求。
当用户要求改变 MnemoTrace 强调或抑制的内容时用 focus，而不是在他们纠正某条具体已存 fact 时使用。

## 面向 C 端的产品界面

面向用户的产品应当让 memory 层可见且可治理。最小有用的界面不是聊天日志页面；而是生命周期与所有权循环：

```text
Conversation setup
  show the current namespace/key
  let the app set a stable session_id such as app:<product>:<conversation-id>
  call agent_status(session_id) and show content-free counts/backlog/next actions
  list sessions with /v1/sessions so the user can see which agents/apps touched memory

During the conversation
  recall(query, session_id) before model calls that depend on prior context
  remember(content, session_id, scope="auto" or "long") for durable preferences/decisions/facts
  remember(content, session_id, scope="working") for short-lived task state

At conversation end
  close_session(session_id)
  show session_report(session_id) so the user can see what durable memory was saved

Memory management
  list/paginate/search facts with include_sensitive=false when rendering share-safe views
  allow precise edit/delete of single facts
  export share-safe JSON by default; require explicit action for include_sensitive=true
  require explicit confirmation before full namespace erase
```

`/ui` 处附带的管理控制台遵循这一模式：Conversations 视图暴露 `session_id`、写 `scope`、无内容的 agent
状态、close-session 以及 session report；Facts 与 Privacy 视图覆盖 inspect/edit/delete/focus/export/erase。

## OpenAI 兼容集成

对于已经在使用 OpenAI SDK 的应用，修改 `base_url` 并传入 MnemoTrace 的 `memory` 扩展：

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="me")

client.chat.completions.create(
    model="mnemotrace",
    messages=[{"role": "user", "content": "Continue the benchmark work."}],
    extra_body={
        "memory": {
            "session_id": "codex:mnemotrace:thread-abc123",
            "recall": True,
            "remember": True,
            "scope": "auto",
        }
    },
)
```

当 conversation 结束时：

```bash
curl -s -X POST http://localhost:8000/v1/sessions/close \
  -H "Authorization: Bearer me" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"codex:mnemotrace:thread-abc123"}'
```

## TypeScript SDK 集成

`mnemotrace-memory` SDK 使用本地 `clients/typescript/`，不是已发布 npm 包；先在本检出运行
`npm --prefix clients/typescript install` 与 `npm --prefix clients/typescript run build`，再在应用中
安装该本地目录，例如 `npm install /absolute/path/to/MnemoTrace/clients/typescript`。

```ts
import { MnemoTraceClient } from 'mnemotrace-memory'

const sessionId = 'codex:mnemotrace:thread-abc123'
const mnemotrace = new MnemoTraceClient({ baseUrl: 'http://localhost:8000', apiKey: 'me' })

const status = await mnemotrace.agentStatus({ sessionId })
console.log(status.session, status.focus)

const memory = await mnemotrace.recall('current project rules and user preferences', { sessionId })

await mnemotrace.remember('Project rule: benchmark claims require committed raw logs.', {
  sessionId,
  scope: 'long',
})

await mnemotrace.closeSession(sessionId)
const report = await mnemotrace.sessionReport(sessionId)
console.log(report.facts)
```

对于即插即用的聊天：

```ts
await mnemotrace.chat.completions.create({
  model: 'mnemotrace',
  messages: [{ role: 'user', content: 'Continue the release notes.' }],
  memory: {
    session_id: sessionId,
    recall: true,
    remember: true,
    scope: 'auto',
  },
})
```

## 该记住什么

优质的持久 memory：

- 用户偏好与常驻指令
- 项目规则与架构决策
- 可复用的调试结论
- benchmark（基准）/evaluation（评测）约束
- 人员、团队、仓库以及反复出现的工作流

应避免持久 memory：

- 密钥、token、凭据
- 一次性的命令输出
- 大日志
- 转瞬即逝的生理/情绪状态
- 临时任务状态（除非标记为 working memory）

## 产品准则

一个好的跨 agent 适配器应当让 memory 自动化：

```text
The user should not have to say "call remember now."
The user should be able to inspect, correct, delete, and export what was remembered.
The same memory should improve the next agent, not just the current chat.
```
