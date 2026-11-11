# MCP 工具参考

安装和客户端配置见[Agent 接入](../guides/agents.md)。可信本地 stdio 绑定显式账号和空间，远程代理使用 HTTP key 与空间；工具定义见 [server.py](../../mnemotrace/mcp/server.py)。

自托管环境先按[部署说明](../../deploy/README.md)初始化 Postgres 并创建空间。部署的数据库只对内部网络开放时，外部客户端通过 HTTP 或远程 MCP 接入。

## 读取与会话

对话使用 `mnemotrace_remember_messages(messages, session_id, processing="buffered", extract=True)`。
每条要求 message_id、role（user/assistant）和完整 content；event_time 可省略，表示来源时间未知。
一次请求属于同一会话。缓冲对话由独立 worker 在窗口空闲 60 秒或达到预算后处理，也可显式刷新。
同空间、会话和消息 ID 的重传返回现有事件状态；内容或不可变处理意图改变时冲突。
笔记使用 `mnemotrace_remember`，其 `processing=immediate|buffered` 默认 immediate。两个工具默认抽取事实，传 `extract=false` 时只保存原文。
`mnemotrace_processing_status(session_id=null)` 查询无原文的任务状态；
`mnemotrace_flush_processing(session_id=null, timeout=30, retry_failed=false, include_digests=false)` 推进并等待当前会话的核心处理。
使用 `mnemotrace_get_operation(operation_id)` 查询操作，`mnemotrace_retry_operation(operation_id)` 重试原操作。
刷新返回 completed、failed 或 timed_out，仅完成时 ok=true；retry_failed 显式重新处理可重试的失败任务。
工具在本地与远程后端使用相同契约，缓冲接收不表示事实已可检索。

多数读取工具支持 `response_format="markdown"`（默认）或 `"json"`。JSON 指格式化后的工具结果，不应假定其等同于原始 HTTP 响应的所有字段。

| 工具 | 主要参数 | 用途 |
| --- | --- | --- |
| `mnemotrace_agent_status` | session_id 可选、response_format | 不含正文的接线与状态 |
| `mnemotrace_recall` | query 必填；max_digests=20（0–50）、answer=false、as_of、redact_sensitive=false、response_format | 返回相关上下文，JSON 含 context、context_sections、tokens_est 等 |
| `mnemotrace_search` | query 必填；subject、predicate、as_of、redact_sensitive=false、response_format | 返回候选证据或显式槽位的唯一值 |
| `mnemotrace_list_sessions` | limit=20（1–100）、offset=0、q=""、response_format | 不含正文的会话索引 |
| `mnemotrace_session_report` | session_id 必填，include_sensitive=false、limit=100、offset=0、response_format | 按页查看会话事实，使用 facts_page.next_offset 读取后续结果 |
| `mnemotrace_list_facts` | limit=20（1–100）、offset=0、include_superseded=false、response_format | 分页事实，用于检查或定位 id |
| `mnemotrace_profile` | response_format | 读取七栏画像缓存，不调用模型 |
| `mnemotrace_refresh_profile` | 无 | 显式请求模型刷新，返回操作 ID |
| `mnemotrace_stats` | response_format | 不含内容的命名空间统计 |
| `mnemotrace_audit` | limit=40、response_format | 只读检查图边缺失、投影不一致和无引用节点 |
| `mnemotrace_get_focus` | response_format | track/mute 配置 |

`max_digests` 是 MCP 参数，HTTP 对应字段为 `n_digests`，范围均为 0–50。`as_of` 使用 epoch 秒；默认读取不保证自动脱敏，私有上下文需要调用方按场景设置 redact_sensitive。

## 写入与管理

| 工具 | 主要参数 | 用途 |
| --- | --- | --- |
| `mnemotrace_remember_messages` | messages、session_id 必填；processing=buffered/immediate、extract=true/false | 逐条接收对话，返回幂等身份与处理状态 |
| `mnemotrace_remember` | content 必填；session_id=default、extract=true/false | 保存并通过服务层处理整合 |
| `mnemotrace_flush_processing` | session_id、timeout、include_digests | 整理会话并等待核心处理 |
| `mnemotrace_retry_operation` | operation_id | 重试同一操作 |
| `mnemotrace_add_fact` | text、predicate、object 必填；subject=user、sensitive、categories、object_kind | 添加用户明确断言 |
| `mnemotrace_update_fact` | fact_id 必填；内容修改同时提供 subject、predicate、object、text；sensitive、categories、object_kind、response_format | 编辑事实，找不到时检查错误结果 |
| `mnemotrace_set_focus` | track、mute 可选；response_format | None 不变，[] 清空 |
| `mnemotrace_export` | include_sensitive=false、response_format | 默认安全导出，显式选择才返回私有原文 |
| `mnemotrace_delete_fact` | fact_id 必填；confirm=false、response_format | 确认后永久删除单个事实 |
| `mnemotrace_forget` | confirm=false | 确认后清空整个命名空间 |

格式名称以工具 schema 为准。写入可能产生模型调用、持久化与派生状态变化；原始写入、异步排队和整合降级的状态应分别解释。

字段及编辑规则见[分类与对象类型](../architecture/memory.md#分类与对象类型)。

`mnemotrace_list_facts` 接受可选 `categories` 数组，任一标签匹配。筛选在分页前执行，默认敏感过滤及空间权限继续生效。

## 权限与能力边界

- 本地 stdio 使用 `--user-id` 和 `--space-id`。远程 stdio 代理使用 `--api-url`、`--api-key-file` 和 `--space-id`。
  Streamable HTTP 在每次请求中使用 Bearer key 和 `X-MnemoTrace-Space-Id`。
- MCP 不提供 HTTP 的所有管理端点，例如记忆策略、冲突裁决和槽位清理。需要这些能力时选择 HTTP。
- delete_fact 与 forget 必须 confirm=true；读取工具的只读注解只是客户端提示，实际副作用由工具实现决定。
- 工具错误可能表现为错误文本，而非标准 HTTP 状态。调用方应检查结果并在失败时报告，不能因为工具调用已返回就宣布完成。
- 默认敏感标记过滤不是完整隐私保证。清空范围与原始事件保留见[记忆生命周期](../architecture/memory.md)。

## 最小生命周期

```text
agent_status(session_id)
→ recall(query)
→ remember_messages(messages, session_id)
→ flush_processing(session_id)
→ session_report(session_id)
```

客户端负责在恰当时机调用，安装工具不会自动使所有对话被记住。持续导入和客户端接线见[Agent 指南](../guides/agents.md)。

模型抽取的配置错误、运行失败和排队状态与 [HTTP 契约](http-api.md#模型抽取状态与错误)一致。本地和远程模式均返回安全错误码与原文保存状态；queued 不表示已完成抽取。


## 条件摘要与原文读取

用 `mnemotrace_get_episode(episode_id, space_id=null, response_format)` 读取召回结果的原文。
默认在绑定空间读取，指定其他空间时验证同一账号对该空间的归属。
核心任务和摘要状态各自可见，`auxiliary_operation_ids` 指向辅助任务。

按事实谓词统计使用 `mnemotrace_aggregate`，指定 `count` 或 `count_distinct`。事实零匹配时可给查询返回摘要候选。多跳使用 `mnemotrace_follow_path`，传入实体名称和 2–4 个有序的出边谓词。普通召回、统计和多跳都支持显式历史条件。

自由文本搜索返回候选事实及来源，状态为 `evidence_only` 或 `no_evidence`。显式提供主体和谓词且只有一个不同值时，搜索返回 `answered`。
`mnemotrace_recall` 的 JSON 包含 `context_sections`。传 `answer=true` 可请求模型回答，并通过 `answer_status` 读取调用结果。
