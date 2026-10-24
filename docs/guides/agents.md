# Agent 接入与会话生命周期

多个客户端共享记忆时，使用同一个授权空间或本地 namespace；不同用户编号保持隔离。`session_id` 标记来源和收尾范围，不是另一份记忆库。服务映射多个 `key` 到同一租户时也可共享，不能简单把“不同 `key`”解释为必然隔离。

## 选择接入方式

| 方式 | 适合场景 |
| --- | --- |
| 本地 stdio MCP | 个人工作站，无 HTTP 服务；客户端启动同一 Python 安装 |
| MCP → HTTP | 多客户端连接已有服务，授权、锁与存储由服务管理 |
| HTTP / TypeScript SDK | 应用自行组织生命周期 |
| OpenAI-compatible chat | 已有聊天应用注入 memory 扩展 |

从当前源码检出安装，发行包名不意味着已有 `PyPI/npm` 发布。离线入门见[快速开始](../quickstart.md)，服务配置见[部署说明](../../deploy/README.md)。

## 本地安装

```bash
python -m pip install -e '.[mcp]'
mnemotrace-agent-bootstrap --local --dry-run --namespace user123 --python /path/to/python
mnemotrace-agent-bootstrap --local --namespace user123 --python /path/to/python
```

bootstrap 默认安装 Codex 与项目 `.mcp.json`，并运行 doctor。`--bootstrap-targets codex` 或 `mcp-json` 可只安装一侧。指定的 Python 必须已安装当前检出与 MCP extra，仓库目录不能掩盖缺失的安装。

`--install-policy` 额外把建议的运行策略写入目标项目 AGENTS 受管块，`--uninstall-policy` 仅移除该块；这是用户显式接入产品的功能，与本仓库已移除的 Spec Kit 自动计划回写不同。

## 接入已有 HTTP 服务

```bash
mnemotrace-agent-bootstrap --dry-run --python /path/to/python --api-url http://127.0.0.1:8000 --prompt-api-key
mnemotrace-agent-bootstrap --python /path/to/python --api-url http://127.0.0.1:8000 --prompt-api-key
```

隐藏输入后创建用户私有凭据文件，配置仅引用其绝对路径。也可显式提供 `--api-key-file /private/path/service.key`。原始 `Key` 不应放进共享配置或复制命令；环境凭据与文件错误的处理见[服务架构](../architecture/service.md#凭据安装与诊断)。

仅生成片段而不安装：

```bash
mnemotrace-agent-setup --client codex --local --namespace user123 --python /path/to/python
mnemotrace-agent-setup --client claude-code --api-url http://127.0.0.1:8000 --api-key-file /private/path/service.key
```

## 客户端配置

Codex 本地示例：

```toml
[mcp_servers.mnemotrace]
command = "/path/to/python"
args = ["-m", "mnemotrace.mcp", "--namespace", "user123"]
```

Claude Code、Cursor 或接受 `.mcp.json` 的客户端远程代理示例：

```json
{
  "mcpServers": {
    "mnemotrace": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "mnemotrace.mcp", "--api-url", "http://127.0.0.1:8000", "--api-key-file", "/private/path/service.key"]
    }
  }
}
```

直接安装分别用 `--install-codex`、`--install-mcp-json`；均支持 dry-run、备份和 uninstall。卸载只移除工具配置，不删除凭据文件或记忆。尽量由生成器处理 Windows 路径和 `TOML/JSON` 转义。

需要 streamable HTTP MCP 时可显式启动 `python -m mnemotrace.mcp --http --host 127.0.0.1 --port 8765`，客户端连接 `http://127.0.0.1:8765/mcp`；远程代理另外指定 API URL 和私有凭据引用，网络暴露边界由部署方管理。

## 验证接线

```bash
mnemotrace-agent-doctor --client codex --python /path/to/python
mnemotrace-agent-doctor --client codex --python /path/to/python --codex-config ~/.codex/config.toml --mcp-json ./.mcp.json
```

HTTP 模式使用相同 URL 和 `--api-key-file`。doctor 校验解释器、真实 stdio 启动、配置引用及跨 Agent 生命周期；本地检查使用临时 namespace，远程检查以所选授权目标执行合成验证。不要用另一个 Python 的成功代替实际客户端解释器。

## 每个会话的调用顺序

```text
agent_status(session_id)       接线、空间和计数自检
recall(query, session_id)      在依赖过往上下文的任务前检索
remember(..., session_id)      保存持久事实或会话态
close_session(session_id)      完成或切换任务时整理并保存
session_report(session_id)     审计该会话贡献
```

`session_id` 推荐 `codex:<repo>:<thread>`、`claude-code:<repo>:<thread>` 或 `app:<product>:<conversation>`；`recall`、`remember` 和 close 保持一致。长期偏好、约定、决定用 `auto`/`long`，临时任务状态用 `working`；凭据、密钥和大段原始日志不作为长期画像。

`close_session` 不是删除会话。结论蒸馏需要 LLM；同义改写去重、来源追溯和单会话删除的边界见[问题台账](../issues.md)。

用户纠错时先列出事实取得 ID，再 `update/delete`；整空间 `forget` 仅用于明确的整空间擦除。默认导出是安全结构化范围，完整私有导出须显式选择。工具参数见 [MCP](../reference/mcp.md)。

## 应用与已有会话导入

TypeScript SDK 见 [clients/typescript](../../clients/typescript/README.md)，HTTP 会话操作及 OpenAI-compatible memory 扩展见[HTTP 参考](../reference/http-api.md)。应用自行维持稳定 `session_id` 并在结束时收尾。

watch 批量解析本机转录，按内容指纹幂等导入并关闭会话。安装器支持 `launchd/systemd` 用户任务，cron 提供配置行；Claude Code SessionEnd hook 与 watcher 用目标指纹及 `seen/claim` 交接。先用 `mnemotrace-watch --help` 选择状态、安装和卸载操作，再核验目标解释器及数据空间。

watch 的 seen 不能单独证明服务端收尾成功；Codex 的事件覆盖也不能由 Claude hook 推定。导入与收尾验收独立于标准 benchmark，度量见 [eval/facility.py](../../eval/facility.py)。

客户端外部配置格式如有变化，以各自官方说明为准：[Codex MCP](https://developers.openai.com/codex/mcp)、[Claude Code MCP](https://docs.anthropic.com/en/docs/claude-code/mcp)。
