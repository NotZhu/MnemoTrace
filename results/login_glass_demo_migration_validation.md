# 登录毛玻璃与演示空间迁移验收（2026-10-12）

## 界面

- 登录输入框聚焦时浅蓝边框、3px 柔光，钥匙图标同步提亮。
- 输入有内容时显示清空按钮，点击后值为空、焦点仍在输入框、提交禁用。
- 卡片计算样式：`rgba(27, 35, 46, 0.72)`，`blur(16px) saturate(1.2)`。
- 用户明确反馈对象为文字选区：保留原有聚焦样式和自动聚焦，仅增强 Key 输入的 `::selection`。
  浏览器全选演示 Key（选区 0–15）后，选区底色为 `rgb(156, 177, 207)`，文字为 `rgb(18, 23, 31)`。构建通过。
- 演示按钮填入 `mnemotrace-test`，进入控制台后显示 1 条事实、1 段对话。
- 演示空间的隐私页显示保护提示，永久清空按钮禁用。
- 已登录旧演示 Key 的持久化状态自动升级；其他用户 Key 保持原值。

## 数据迁移

- 暂停本机后端后，将 `test` JSONL 存储完整备份到 `~/.mnemotrace/backups/demo-rename-20261012-181454-161565/`。
- 对原文件逐个计算 SHA-256，确认备份完整且源数据没有并发修改。
- 迁移到 `mnemotrace-test` 的安全摘要目录，更新记录归属及 manifest 内的身份索引。
- 逐字段比较确认：正文、向量、记录 ID、时间戳、来源、历史链、策略保持原值。
- 来源及图谱引用校验通过，其他空间 manifest 未变化。
- 迁移前后计数一致：episodes 1、facts 1、entities 2、relations 1、working 0、conflicts 0。
- 校验通过后删除源空间，目录检查确认旧源不存在，新空间和备份保留。
- 恢复后端热更新；`GET /health` 和 `GET /ready` 均为 200；新空间只读查询返回 facts 1、episodes 1。

## 检查命令与结果

- `cd frontend && npm run build`：通过。
- `cd frontend && npm run test:demo-auth`：3 项通过，覆盖旧演示状态迁移、其他 Key 保持及未登录状态。
- `python -m pytest tests/test_server.py::test_forget_rejects_public_demo_and_anonymous_namespaces tests/test_server.py::test_forget_clears_namespace tests/test_service_paths.py -q`：27 项通过；本机验证关闭无关自动加载插件。
- 一次性迁移工具先在合成数据上自检，覆盖归属变更、正文和引用保持、状态重映射、目标存在时拒绝覆盖、保留源数据。
- 真实演示空间只做 GET 验收；删除保护通过离线合成数据测试，不对真实新空间发送清空请求。

本记录为 UI 和本机数据运维验收，不涉及 benchmark 数字。备份及真实记忆正文均未写入仓库。
