# Agent 适配器配方

这些配方把 MnemoTrace 变成一个可供多个 agent 共享的 memory（记忆）层。你可以用两种方式运行它：

- **本地零服务 MCP**：每个 agent 启动 `python -m mnemotrace.mcp`，并共享同一个本地
  namespace（命名空间）/data 目录。
- **HTTP 服务**：agent 通过一个本地或托管的 MnemoTrace 服务器代理，使用一个 Bearer key 作为 namespace。

安装命令均在仓库根目录运行；`mnemotrace-memory` 尚未发布到 PyPI，
请勿从上游来源安装来代替本检出。要最快完成个人工作站搭建，先使用本地 MCP：

```bash
pip install -e '.[mcp]'
mnemotrace-agent-bootstrap --local --dry-run --namespace me --python /path/to/python
mnemotrace-agent-bootstrap --local --namespace me --python /path/to/python --install-policy
```

上述命令会针对同一个本地 namespace 安装 Codex + 项目的 `.mcp.json`，运行 doctor，并可以写入 AGENTS.md 的 memory 策略。doctor 会校验已安装的配置文件确实包含所请求的本地 `--namespace`，从而让 Codex 与项目 MCP 客户端不会悄悄指向不同的 memory 空间。

对于托管或自托管的 HTTP 服务，先启动 MnemoTrace：

```bash
pip install -e '.[server]'
export MNEMOTRACE_OPEN=1
export MNEMOTRACE_EMBEDDER=hashing
uvicorn mnemotrace.server.app:app --port 8000
```

用一个 Bearer key 作为用户的 namespace：

```bash
export MNEMOTRACE_API_URL=http://localhost:8000
export MNEMOTRACE_API_KEY=me
```

预期的生命周期是：

```text
recall before work -> remember durable information during work -> close the session at the end
```

你可以生成下面的代码片段，而不必手工复制它们：

```bash
mnemotrace-agent-setup --client all --local --namespace me
mnemotrace-agent-setup --client codex --api-url http://localhost:8000 --api-key me
mnemotrace-agent-setup --client claude-code --local --namespace me
```

对于 HTTP 接入，使用相同的 bootstrap 命令并带上 URL/key。它会安装 Codex 的 `config.toml`
条目与项目的 `.mcp.json` 条目，运行 doctor，并打印一段可直接粘贴的 AGENTS.md 策略：

```bash
mnemotrace-agent-bootstrap --dry-run \
  --api-url http://localhost:8000 \
  --api-key me

mnemotrace-agent-bootstrap \
  --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me

mnemotrace-agent-bootstrap \
  --install-policy \
  --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me
```

如果你只想要搭建中的一侧，使用 `--bootstrap-targets codex` 或 `--bootstrap-targets mcp-json`。`--install-policy` 会把建议的 MnemoTrace 运行策略写入一段受管理的 AGENTS.md block；`--uninstall-policy` 只移除那段 block。仅在需要跳过运行时校验时才使用 `--no-doctor`。

对于 Claude Code、Cursor，以及读取项目 `.mcp.json` 的通用 MCP 客户端，你可以直接安装项目配置。写入模式是显式的，会在改动前备份 `.mcp.json`，并支持 dry-run/uninstall：

```bash
mnemotrace-agent-setup --install-mcp-json --dry-run \
  --api-url http://localhost:8000 \
  --api-key me

mnemotrace-agent-setup --install-mcp-json --doctor \
  --doctor-client claude-code \
  --api-url http://localhost:8000 \
  --api-key me

mnemotrace-agent-setup --uninstall-mcp-json
```

对于 Codex，你也可以直接安装配置。写入模式是显式的，会在改动前备份
`~/.codex/config.toml`，并支持 dry-run/uninstall：

```bash
mnemotrace-agent-setup --install-codex --dry-run \
  --api-url http://localhost:8000 \
  --api-key me

mnemotrace-agent-setup --install-codex \
  --api-url http://localhost:8000 \
  --api-key me \
  --doctor

mnemotrace-agent-setup --uninstall-codex
```

如果 `python -m mnemotrace.mcp` 失败，是因为该 Python 没有安装 MCP SDK，请把生成器指向你安装了
在本检出运行 `pip install -e '.[mcp]'` 的那个 Python 可执行文件：

```bash
mnemotrace-agent-setup --client codex --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me
```

在把配置接入真实 agent 之前，针对同一个 Python 可执行文件运行 doctor。它会使用一个临时本地 namespace，
针对同一个 data dir 启动两个真实的 `python -m mnemotrace.mcp` stdio 服务，并端到端地校验跨 agent 的 MCP
生命周期：目标服务预加载一个空 session，源 session 写入、关闭、报告已保存的内容，已在运行的目标服务
recall 源端的 memory，运行时返回一份安全的 memory 导出，两个 session 都关闭。
当你也想让它确认已安装的文件包含预期的 MnemoTrace 服务条目时，传入配置路径：

```bash
mnemotrace-agent-doctor --client codex --python /path/to/python

mnemotrace-agent-doctor --client codex --python /path/to/python \
  --codex-config ~/.codex/config.toml \
  --mcp-json ./.mcp.json
```

对于托管或自托管的 HTTP 服务，加入 agent 配置将使用的同一个 URL/key。这会检查本地 MCP 运行时、真实的
stdio 启动路径、可选的配置文件，以及远程用户 namespace，并以相同的源 agent 到目标 agent 的
handoff（交接）加上 session 报告审计：

```bash
mnemotrace-agent-doctor --client codex --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me
```

## Claude Code

Claude Code 可以从 JSON 添加 MCP 服务。对于远程或自托管的 MnemoTrace HTTP 服务，通过 MnemoTrace MCP 服务代理它：

```bash
claude mcp add-json mnemotrace \
  '{"type":"stdio","command":"python","args":["-m","mnemotrace.mcp","--api-url","http://localhost:8000","--api-key","me"]}'
```

对于完全本地、不带 HTTP 服务的 memory store：

```bash
claude mcp add-json mnemotrace \
  '{"type":"stdio","command":"python","args":["-m","mnemotrace.mcp","--namespace","me"]}'
```

项目可共享的 `.mcp.json`：

```json
{
  "mcpServers": {
    "mnemotrace": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m",
        "mnemotrace.mcp",
        "--api-url",
        "${MNEMOTRACE_API_URL:-http://localhost:8000}",
        "--api-key",
        "${MNEMOTRACE_API_KEY:-me}"
      ]
    }
  }
}
```

带备份的直接项目安装：

```bash
mnemotrace-agent-setup --install-mcp-json \
  --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me
```

安装并立即校验 MCP 运行时：

```bash
mnemotrace-agent-setup --install-mcp-json --doctor \
  --doctor-client claude-code \
  --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me
```

回滚/卸载只从 `.mcp.json` 中移除 `mnemotrace` 条目，且会先备份当前文件：

```bash
mnemotrace-agent-setup --uninstall-mcp-json
```

在项目指令中一次性告诉 Claude Code 运行策略：

```text
Use MnemoTrace as the user's cross-agent long-term memory.
At session start or when debugging memory wiring, call mnemotrace_agent_status with a session_id like
claude-code:<repo>:<thread>.
Before tasks that may depend on prior user/project context, call mnemotrace_recall with a session_id like
claude-code:<repo>:<thread>.
When the user states a durable preference, project rule, decision, or reusable fact, call
mnemotrace_remember with that session_id and `scope="long"` or `scope="auto"`.
When the user states short-lived task state, call mnemotrace_remember with `scope="working"`.
When the user asks you to emphasize or suppress a class of memories, call mnemotrace_set_focus; use
mnemotrace_get_focus to inspect the current focus policy.
When the thread ends or you switch tasks, call mnemotrace_close_session with the same session_id.
Do not store secrets, credentials, or large logs.
```

## Codex

Codex 把 MCP 服务存储在 `config.toml` 中。CLI 与 IDE 扩展共享这份配置。

CLI 搭建：

```bash
codex mcp add mnemotrace -- python -m mnemotrace.mcp \
  --api-url http://localhost:8000 \
  --api-key me
```

如果 Codex 应当启动某个特定的 virtualenv 或 Python 安装，使用
`mnemotrace-agent-setup --client codex --python /path/to/python ...`。

带备份的直接安装：

```bash
mnemotrace-agent-setup --install-codex \
  --python /path/to/python \
  --api-url http://localhost:8000 \
  --api-key me \
  --doctor
```

回滚/卸载只移除 `[mcp_servers.mnemotrace]`，且会先备份当前配置：

```bash
mnemotrace-agent-setup --uninstall-codex
```

等价的 `~/.codex/config.toml`：

```toml
[mcp_servers.mnemotrace]
command = "python"
args = [
  "-m",
  "mnemotrace.mcp",
  "--api-url",
  "http://localhost:8000",
  "--api-key",
  "me",
]
```

仅本地 store：

```toml
[mcp_servers.mnemotrace]
command = "python"
args = ["-m", "mnemotrace.mcp", "--namespace", "me"]
```

建议的 `AGENTS.md` 说明：

```text
Use the MnemoTrace MCP tools as shared long-term memory across agent sessions.
At the start of a task or when debugging memory wiring, call mnemotrace_agent_status with the current
session_id. Then call mnemotrace_recall for relevant user/project context when the task may depend on prior
sessions.
Use session_id = codex:<repo-name>:<thread-id> when remembering or closing a session.
Call mnemotrace_remember with scope="long" or scope="auto" for durable preferences, decisions, project
rules, and reusable facts.
Call mnemotrace_remember with scope="working" for short-lived current-task state.
When the user asks to correct or delete a memory, call mnemotrace_list_facts to get the fact id, then call
mnemotrace_update_fact or mnemotrace_delete_fact(confirm=true). Do not use mnemotrace_forget unless the user asks to
erase the whole namespace.
When the user asks to export their memory, call mnemotrace_export(response_format="json"); it is share-safe by
default, and include_sensitive=true should only be used for an explicit private export.
When the user asks you to remember some topics more strongly or stop surfacing a category unless asked,
call mnemotrace_set_focus. Use mnemotrace_get_focus before changing focus when the current policy matters.
Call mnemotrace_close_session when the task/thread ends.
Never store secrets, credentials, or large raw logs.
```

## Cursor / 通用 MCP 客户端

在你客户端所接受的任何 MCP 配置中，使用相同的 stdio MCP 服务形态：

```json
{
  "mcpServers": {
    "mnemotrace": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m",
        "mnemotrace.mcp",
        "--api-url",
        "http://localhost:8000",
        "--api-key",
        "me"
      ]
    }
  }
}
```

如果客户端支持 streamable HTTP MCP，MnemoTrace 可以直接为其提供服务：

```bash
python -m mnemotrace.mcp --http --host 127.0.0.1 --port 8765 \
  --api-url http://localhost:8000 \
  --api-key me
```

然后把客户端连接到：

```text
http://127.0.0.1:8765/mcp
```

## OpenAI 兼容应用

如果你的应用已经在使用 OpenAI SDK，把它指向 MnemoTrace 并传入 memory 扩展。

Python：

```python
from openai import OpenAI

session_id = "app:my-product:conversation-123"
client = OpenAI(base_url="http://localhost:8000/v1", api_key="me")

client.chat.completions.create(
    model="mnemotrace",
    messages=[{"role": "user", "content": "Continue the release note work."}],
    extra_body={
        "memory": {
            "session_id": session_id,
            "recall": True,
            "remember": True,
            "scope": "auto",
        }
    },
)
```

当 conversation 结束时关闭 session：

```bash
curl -s -X POST "$MNEMOTRACE_API_URL/v1/sessions/close" \
  -H "Authorization: Bearer $MNEMOTRACE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"app:my-product:conversation-123"}'
```

## Session ID 约定

使用能让 provenance（溯源）一目了然的 ID：

```text
claude-code:<repo>:<thread>
codex:<repo>:<thread>
cursor:<workspace>:<thread>
app:<product>:<conversation>
```

对 recall、remember 和 close 保持同一个 `session_id`。当你希望多个工具共享 memory 时用同一个 API key；
当你需要硬性隔离时用不同的 key。

## 来源

- Codex MCP 配置：<https://developers.openai.com/codex/mcp>
- Claude Code MCP 配置：<https://docs.anthropic.com/en/docs/claude-code/mcp>
