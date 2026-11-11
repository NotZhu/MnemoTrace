# 历史导入源码

历史聊天导入与 watch 已退出当前产品支持范围。解析器、导入编排及本地 CLI 源码保留在本目录，供后续接入使用。

解析器通过 `parse` 输出会话。独立函数 `dialogue.import_history(service, context, ...)` 和 `dialogue.import_messages(memory, sessions, ...)` 将会话接到正常对话写入接口。产品服务和离线引擎的类接口不再承担历史导入。

重新接入时，从上述函数恢复 HTTP、MCP、SDK 入口和发行命令。watch 的远程上传目前仍指向已下线的 HTTP 导入接口，需要在恢复该接口后使用。

专属测试通过 `python -m pytest --run-history-import` 显式运行。重新启用前须验证解析、消息身份、来源时间和重试行为。

文档文件摄入已移除。结构化记忆快照与 Postgres 归档由各自的恢复接口处理。
