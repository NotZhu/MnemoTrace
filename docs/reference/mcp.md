# MCP 工具参考

安装和客户端配置见[Agent 接入](../guides/agents.md)。工具通过本地 MemoryService 或远程 HTTP 后端执行，默认 stdio；工具定义见 [server.py](../../mnemotrace/mcp/server.py)。

## 读取与会话

多数读取工具支持 `response_format="markdown"`（默认）或 `"json"`。JSON 指格式化后的工具结果，不应假定其等同于原始 HTTP 响应的所有字段。

| 工具 | 主要参数 | 用途 |
| --- | --- | --- |
| `mnemotrace_agent_status` | session_id 可选、response_format | 不含正文的接线与状态 |
| `mnemotrace_recall` | query 必填；max_chunks=6（0–20）、session_id、as_of、redact_sensitive=false、response_format | 返回相关上下文，JSON 含 context、tokens_est 等 |
| `mnemotrace_search` | query 必填；as_of、redact_sensitive=false、response_format | 直接事实答案与依据 |
| `mnemotrace_list_sessions` | limit=20（1–100）、offset=0、q=""、response_format | 不含正文的会话索引 |
| `mnemotrace_session_report` | session_id 必填；include_sensitive=false、response_format | 会话贡献审计 |
| `mnemotrace_list_facts` | limit=20（1–100）、offset=0、include_superseded=false、response_format | 分页事实，用于检查或定位 id |
| `mnemotrace_profile` | response_format | 派生画像与关键事实 |
| `mnemotrace_stats` | response_format | 不含内容的命名空间统计 |
| `mnemotrace_audit` | limit=40、response_format | 启发式体检与建议，不执行修复 |
| `mnemotrace_get_focus` | response_format | track/mute 配置 |

`max_chunks` 是 MCP 参数，HTTP 对应字段为 `n_chunks`，两者上限不同。`as_of` 使用 epoch 秒；默认读取不保证自动脱敏，私有上下文需要调用方按场景设置 redact_sensitive。

## 写入与管理

| 工具 | 主要参数 | 用途 |
| --- | --- | --- |
| `mnemotrace_remember` | content 必填；session_id=default、scope=auto/long/working | 保存并通过服务层处理整合 |
| `mnemotrace_close_session` | session_id=default、summarize=true、clear_working=true、response_format | 收尾，不删除原始事件 |
| `mnemotrace_add_fact` | predicate、object 必填；subject=user、sensitive、category、topic | 添加用户明确断言 |
| `mnemotrace_update_fact` | fact_id 必填；subject、predicate、object、sensitive、category、topic、reset_topic、response_format | 编辑事实，找不到时检查错误结果 |
| `mnemotrace_set_focus` | track、mute 可选；response_format | None 不变，[] 清空 |
| `mnemotrace_import` | content 必填；format=auto | 导入原始导出内容，content 不是服务器文件路径 |
| `mnemotrace_export` | include_sensitive=false、response_format | 默认安全导出，显式选择才返回私有原文 |
| `mnemotrace_delete_fact` | fact_id 必填；confirm=false、response_format | 确认后永久删除单个事实 |
| `mnemotrace_forget` | confirm=false | 确认后清空整个命名空间 |

格式名称以工具 schema 为准。写入可能产生模型调用、持久化与派生状态变化；原始写入、异步排队和整合降级的状态应分别解释。

topic 使用工具 schema 的固定枚举；来源由服务端记录。人工选择 unknown 与 `reset_topic=true` 不同：重置立即按当前内容执行离线规则，不调用模型，结果为明确主题/rule 或 unknown/unset。不能同时指定主题与重置标志；仅改主题不改变事实来源和历史。新增未指定主题、内容变化且未指定新主题时使用规则。新增或编辑结果展示主题及来源，列表的 JSON 与 Markdown 结果保留该元数据；脱敏报告省略它。完整语义见[主题与来源](../architecture/memory.md#主题与来源)。

`mnemotrace_list_facts` 增加可选 topic，省略表示全部，unknown 同时匹配人工未知与未赋值未知。主题过滤由本地服务或远端 HTTP 服务先执行，再应用工具的历史过滤和分页，total 为这些筛选后的总数。非法枚举由工具 schema 拒绝；默认敏感过滤及命名空间权限保持生效。

## 权限与能力边界

- 本地后端绑定一个 namespace；远程后端使用服务端凭据解析身份。现有 MCP 工具未暴露 HTTP recall/remember 的任意 spaces/space 参数。
- MCP 不提供 HTTP 的所有管理端点，例如独立工作记忆管理、结构化画像、记忆策略、冲突裁决和槽位清理。需要这些能力时选择 HTTP。
- delete_fact 与 forget 必须 confirm=true；读取工具的只读注解只是客户端提示，实际副作用由工具实现决定。
- 工具错误可能表现为错误文本，而非标准 HTTP 状态。调用方应检查结果并在失败时报告，不能因为工具调用已返回就宣布完成。
- 默认敏感标记过滤不是完整隐私保证。清空范围、工作记忆持久化与原始事件保留见[记忆生命周期](../architecture/memory.md)。

## 最小生命周期

```text
agent_status(session_id)
→ recall(query, session_id)
→ remember(content, session_id, scope)
→ close_session(session_id)
→ session_report(session_id)
```

客户端负责在恰当时机调用，安装工具不会自动使所有对话被记住。持续导入和客户端接线见[Agent 指南](../guides/agents.md)。

模型抽取的配置错误、运行失败和排队状态与 [HTTP 契约](http-api.md#模型抽取状态与错误)一致。本地和远程模式均返回安全错误码与原文保存状态；queued 不表示已完成抽取。
