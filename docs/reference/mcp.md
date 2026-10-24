# MCP 工具

MCP 服务将工具调用映射到同一个 `MemoryService`，可直接使用本地 namespace，也可代理 HTTP 服务。实现及完整类型约束见 [mcp/server.py](../../mnemotrace/mcp/server.py)，安装见 [Agent 指南](../guides/agents.md)。

## 启动

```bash
python -m pip install -e '.[mcp]'
python -m mnemotrace.mcp --namespace user123
```

本地 stdio 模式使用服务进程自己的数据目录；共享目录和相同 namespace 的客户端可以共享持久记忆。远程代理指定 `--api-url` 和安全凭据文件 `--api-key-file`，配置文件不应包含原始 `Key`。

## 读取与审计

| 工具 | 主要输入 | 输出 |
| --- | --- | --- |
| `mnemotrace_agent_status` | `session_id`（可选） | 空间、计数、focus 和建议，不含记忆正文 |
| `mnemotrace_recall` | query、`session_id`、`max_chunks`、`as_of`、`redact_sensitive` | 带来源上下文；JSON 含 `context/tokens_est/as_of/redacted_sensitive` |
| `mnemotrace_search` | query、`as_of`、`redact_sensitive` | 直接答案和支撑事实，无依据时拒答 |
| `mnemotrace_list_sessions` | `limit`、`offset`、`q` | 不含正文的会话索引和分页 |
| `mnemotrace_session_report` | `session_id`、`include_sensitive` | 该会话保存的事实审计，默认脱敏 |
| `mnemotrace_list_facts` | `limit`、`offset`、`include_superseded` | 带当前/历史状态的事实列表 |
| `mnemotrace_profile` | `response_format` | 画像与事实 |
| `mnemotrace_get_focus` | `response_format` | `track`/`mute` 策略 |
| `mnemotrace_audit` | `limit` | 只读体检发现和处理建议 |
| `mnemotrace_stats` | `response_format` | 不含正文的计数和运行状态 |

支持 `response_format` 的工具可选择 `markdown` 或 `json`。`recall` 的 `max_chunks` 默认 6、范围 0–20；`list_facts/list_sessions` 的 `limit` 默认 20、范围 1–100。准确签名以工具 schema 为准。

## 写入与生命周期

| 工具 | 用法与边界 |
| --- | --- |
| `mnemotrace_remember` | `content`、`session_id`、`scope`=`auto`/`long`/`working`；尽力整合，原始记忆先保存 |
| `mnemotrace_add_fact` | `predicate`、`object`、`subject`=`user` 及可选元数据；明确用户断言 |
| `mnemotrace_update_fact` | `fact_id` 及三元组/元数据；实际修改内容与元数据分别处理 |
| `mnemotrace_set_focus` | `track`/`mute`；None 保持、[] 清空 |
| `mnemotrace_import` | `content`、format=`auto`；批量导入历史 |
| `mnemotrace_close_session` | `session_id`、summarize、`clear_working`；整理、摘要、结论及保存 |

`close_session` 不删除原始会话；无 LLM 不蒸馏 `outcomes`。精确去重不保证同义改写幂等。MCP `update_fact` 的 schema 当前不提供 HTTP `statement` 整句参数，不把两个接入面描述为完全相同。

## 导出与擦除

- `mnemotrace_export` 默认仅非敏感 facts + graph；完整私有导出才使用 `include_sensitive=true`。
- `mnemotrace_delete_fact` 需要 `fact_id` 和 confirm=true，永久擦除该条事实。
- `mnemotrace_forget` 需要 confirm=true，擦除整个授权空间；不传只说明操作。

MCP 没有 HTTP clear-slot 工具。数据归属、私有凭据和并发边界见[服务架构](../architecture/service.md)，未完成能力见[问题台账](../issues.md)。
