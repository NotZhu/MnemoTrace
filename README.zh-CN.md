# MnemoTrace · 忆痕

[English](README.md)

为 LLM 智能体提供长期记忆。Python 包和导入名均为 `mnemotrace`，HTTP 服务和写入 worker 的部署配置位于 [deploy/](deploy/)。

HTTP v1 的空间、会话、原文、事实和写入任务标识由 32 位小写十六进制字符组成。资源路径和任务列表的 `session_id` 筛选参数直接使用 API 返回的标识。

HTTP v1 以 UTC 返回时间，并拒绝超出日历范围的输入。未知的事件时刻和事实有效期边界可以为 `null`。

## 本地部署

请从空数据库开始。按[数据库配置示例](deploy/.env.example)填写根目录 `.env`，按[模型配置示例](deploy/.env.models.example)填写 `deploy/.env.models`。在仓库根目录运行：

```sh
docker compose --env-file .env -f deploy/compose.yaml up --build --wait
```

Compose 会安装 pgvector 并创建数据库角色，完成迁移和授权后启动 API 和 worker。自管数据库需要在执行迁移前将 pgvector 安装到 `public` schema。

## 开发验证

在仓库根目录运行：

```sh
uv sync --locked
uv run --locked python scripts/check.py
```

真库回归通过 `MNEMOTRACE_TEST_POSTGRES_DSN` 指向独立的 PostgreSQL/pgvector 测试实例。部署后运行 `uv run --locked python scripts/http_smoke.py --help` 查看 HTTP 闭环验收所需的参数和凭据配置。
