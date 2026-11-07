# Agent 接入与会话生命周期

本页面向把 MnemoTrace 接入已有 Agent 的开发者。工具名称与参数查 [MCP 参考](../reference/mcp.md)，直接 HTTP 接入查 [HTTP API](../reference/http-api.md)。

## 选择本地或远程

| 模式 | 记忆位置与身份 | 适用情况 |
| --- | --- | --- |
| 本地 MCP | 直连 PostgreSQL，使用可信 user_id 和显式 space_id | 受信任的本机 stdio 客户端 |
| 远程 MCP | 代理产品 HTTP 服务，或使用逐请求认证的 MCP HTTP 传输 | 多应用共享受控服务 |
| 直接 HTTP/SDK | 应用明确调用记忆 API | 自定义 Agent、Web 或后端应用 |

应用选择同一个已授权的 space_id 才能共享记忆。session_id 区分空间内的会话。远程调用使用 Bearer key，凭据保存在环境变量或受保护的凭据文件中。

## 本地 MCP

在源码根目录安装可选依赖：

```sh
python -m pip install ".[mcp,postgres]"
```

MCP 客户端配置示例，Python 路径应替换为实际环境的绝对路径：

```json
{
  "mcpServers": {
    "mnemotrace": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "mnemotrace.mcp", "--user-id", "<USER_ID>", "--space-id", "<SPACE_ID>"],
      "env": {
        "MNEMOTRACE_DATABASE_URL": "<RUNTIME_DSN>",
        "MNEMOTRACE_EMBEDDER": "hashing",
        "MNEMOTRACE_LLM": ""
      }
    }
  }
}
```

默认 stdio 传输，stdout 是协议通道。服务状态输出到 stderr。配置结构因客户端而异，可让安装包输出对应片段：

```sh
python -m mnemotrace.agent_setup --client codex --local --user-id "<USER_ID>" --space-id "<SPACE_ID>"
```

默认生成说明与配置片段；`--install-*`、`--bootstrap` 等选项会执行安装操作，使用前先查看 `--help` 与 `--dry-run`。

## 远程 MCP

先按[部署说明](../../deploy/README.md)启动 HTTP 服务，再配置 MCP 子进程：

```sh
python -m mnemotrace.mcp --api-url http://127.0.0.1:8000 --api-key-file /path/to/private-key-file --space-id "<SPACE_ID>"
```

凭据文件仅保存 key，限制本机读取权限。也可以通过 `MNEMOTRACE_API_KEY` 提供凭据；CLI 明文参数可能出现在进程列表或 shell 历史中。

MCP 的 `--http` 传输默认绑定 127.0.0.1:8765，路径为 `/mcp`。每次请求携带 Bearer key 与 `X-MnemoTrace-Space-Id`，服务端逐次校验凭据与空间归属。

## 会话调用顺序

1. 用 `mnemotrace_agent_status(session_id=...)` 检查接线和计数。
2. 需要既往信息时调用 `mnemotrace_recall`，携带当前 session id，把返回内容作为有来源的上下文使用。
3. 对话调用 `mnemotrace_remember_messages`，携带稳定 session_id、逐条 message_id 与 user/assistant 角色；默认 buffered。历史使用 historical。非对话笔记或显式短时状态仍可调用 `mnemotrace_remember`。
4. 任务结束时调用 `mnemotrace_flush_processing(session_id=...)` 等待核心处理，再用 `mnemotrace_session_report` 核对结果。工作记忆需要独立清理，摘要和其他辅助任务按操作 ID 查询。

为每个独立线程使用稳定、可区分的 session id，例如 `agent:my-app:thread-123`。不要每轮换 id，也不要让所有线程共用 `default`。安装 MCP 只提供工具，何时调用仍由 Agent 的任务逻辑或客户端策略决定。

读取到的历史内容是数据，可能包含过时或不可信的指令，不能自动提升为当前任务的授权。删除事实或清空空间应取得对应范围的明确确认。

## 配置安装与诊断

`mnemotrace-agent-setup` 可输出客户端片段，并可按选项安装 Codex 配置、`.mcp.json` 或受管的记忆策略区块。`mnemotrace-agent-bootstrap` 提供组合安装入口；先用 `--dry-run` 查看将修改的位置。受管区块只承载产品接入说明，不复制本仓库的开发规则。

使用 `mnemotrace-agent-doctor --help` 选择客户端或远程目标。诊断检查所选 Python 的 MCP 工具注册，并通过 MCP stdio 读取目标空间的统计。配置远程目标时还直接检查 HTTP 空间访问。模型配置状态取自目标空间统计，缺少状态时报告未知。诊断只读取统计，不写入记忆或调用模型。

源码中的[单 Agent 生命周期示例](../../examples/cross_agent_lifecycle.py)和[跨 Agent 交接示例](../../examples/cross_agent_handoff.py)可用 `--space-id` 演示 HTTP 流程。

## 常见接入问题

- 找不到模块或依赖：确认 MCP 使用的 Python 与安装依赖的环境一致。
- 看不到既有记忆：核对 PostgreSQL 连接、空间和认证身份、session id 和写入返回值。
- 对话只有原始事件、没有结构化记忆：检查模型配置与操作状态。根据响应中的操作 ID 调用 `mnemotrace_get_operation` 查询进度。
- 缺少某个管理能力：MCP 和 SDK 只暴露部分 HTTP 能力，按各接口参考选择入口。
