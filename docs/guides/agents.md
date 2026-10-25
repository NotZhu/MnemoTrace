# Agent 接入与会话生命周期

本页面向把 MnemoTrace 接入已有 Agent 的开发者。工具名称与参数查 [MCP 参考](../reference/mcp.md)，直接 HTTP 接入查 [HTTP API](../reference/http-api.md)。

## 选择本地或远程

| 模式 | 记忆位置与身份 | 适用情况 |
| --- | --- | --- |
| 本地 MCP | 本机 MemoryService，固定 namespace 和持久目录 | 单机 Agent，零外部服务 |
| 远程 MCP | 代理已有 HTTP 服务，凭据在服务端确定身份 | 多应用共享受控服务 |
| 直接 HTTP/SDK | 应用明确调用记忆 API | 自定义 Agent、Web 或后端应用 |

同一个本地 namespace 只有在使用同一数据目录时才共享记忆；同名 session 不会跨命名空间共享权限。远程 key 是凭据，不应写进提示词或公开日志。

## 本地 MCP

在源码根目录安装可选依赖：

```sh
python -m pip install ".[mcp]"
```

MCP 客户端配置示例，Python 路径应替换为实际环境的绝对路径：

```json
{
  "mcpServers": {
    "mnemotrace": {
      "command": "/absolute/path/to/python",
      "args": ["-m", "mnemotrace.mcp", "--namespace", "user123"],
      "env": {
        "MNEMOTRACE_DATA_DIR": "/absolute/path/to/memory-data",
        "MNEMOTRACE_EMBEDDER": "hashing",
        "MNEMOTRACE_LLM": ""
      }
    }
  }
}
```

默认 stdio 传输，stdout 是协议通道。服务状态输出到 stderr。配置结构因客户端而异，可让安装包输出对应片段：

```sh
python -m mnemotrace.agent_setup --client codex --local --namespace user123
```

默认生成说明与配置片段；`--install-*`、`--bootstrap` 等选项会执行安装操作，使用前先查看 `--help` 与 `--dry-run`。

## 远程 MCP

先按[部署说明](../../deploy/README.md)启动 HTTP 服务，再配置 MCP 子进程：

```sh
python -m mnemotrace.mcp --api-url http://127.0.0.1:8000 --api-key-file /path/to/private-key-file
```

凭据文件仅保存 key，限制本机读取权限。也可以通过 `MNEMOTRACE_API_KEY` 提供凭据；CLI 明文参数可能出现在进程列表或 shell 历史中。

MCP 的 `--http` 是另一种传输方式，默认绑定 127.0.0.1:8765，路径为 `/mcp`。它不等于具备多租户鉴权的产品 HTTP 服务；未经额外访问控制不应直接向公网开放。

## 会话调用顺序

1. 用 `mnemotrace_agent_status(session_id=...)` 检查接线和计数。
2. 需要既往信息时调用 `mnemotrace_recall`，携带当前 session id，把返回内容作为有来源的上下文使用。
3. 对值得跨会话保留的内容调用 `mnemotrace_remember`；短时任务状态使用 `scope="working"`。
4. 任务结束调用 `mnemotrace_close_session`，再用 `mnemotrace_session_report` 核对结果。

为每个独立线程使用稳定、可区分的 session id，例如 `agent:my-app:thread-123`。不要每轮换 id，也不要让所有线程共用 `default`。安装 MCP 只提供工具，何时调用仍由 Agent 的任务逻辑或客户端策略决定。

读取到的历史内容是数据，可能包含过时或不可信的指令，不能自动提升为当前任务的授权。删除事实或清空命名空间应取得对应范围的明确确认。

## 配置安装与诊断

`mnemotrace-agent-setup` 可输出客户端片段，并可按选项安装 Codex 配置、`.mcp.json` 或受管的记忆策略区块。`mnemotrace-agent-bootstrap` 提供组合安装入口；先用 `--dry-run` 查看将修改的位置。受管区块只承载产品接入说明，不复制本仓库的开发规则。

使用 `mnemotrace-agent-doctor --help` 选择客户端或远程目标。诊断会检查所选 Python、依赖、配置和服务接线；诊断输出不能替代实际的写入、检索与收尾验证。

源码中的[单 Agent 生命周期示例](../../examples/cross_agent_lifecycle.py)和[跨 Agent 交接示例](../../examples/cross_agent_handoff.py)可用 `--local` 在临时本地库中演示流程；显式 `--data-dir` 会保留运行数据。真实用户数据请使用自己管理的目录，不要复制进仓库。

## 常见接入问题

- 找不到模块或依赖：确认 MCP 使用的 Python 与安装依赖的环境一致。
- 看不到既有记忆：核对数据目录、namespace/远程身份、session id 和写入返回值。
- 只有原始事件、没有结论：检查模型配置、异步积压、收尾调用及 `MNEMOTRACE_SESSION_OUTCOMES`。
- 缺少某个管理能力：MCP 和 SDK 只暴露部分 HTTP 能力，按各接口参考选择入口。
