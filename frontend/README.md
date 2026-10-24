# MnemoTrace 记忆控制台

React + TypeScript 控制台由 Python 服务托管，面向当前 0.1.0 Beta。页面、凭据、查询、分页与图谱机制见[控制台架构](../docs/architecture/console.md)，HTTP 契约见[参考](../docs/reference/http-api.md)。

## 本地开发

使用 Node 22，与 CI / Docker 保持一致；当前 SHA-256 依赖要求 Node >=20.19。包管理器版本由 `packageManager` 固定。

```bash
pnpm install
# 在另一个终端启动记忆服务（API 端口为 :8000）
#   MNEMOTRACE_OPEN=1 MNEMOTRACE_EMBEDDER=bge-small uvicorn mnemotrace.server.app:app --port 8000
pnpm dev            # Vite 开发服务：http://localhost:5173/ui/（将 /v1 与 /health 代理至 :8000）
```

## 构建（由 Python 服务提供页面）

```bash
pnpm test           # 自动发现并运行全部前端测试
pnpm build          # → frontend/dist
```

FastAPI 记忆服务（`mnemotrace/server/app.py`）自动检测 `frontend/dist`，在 **`/ui`** 提供控制台，
并通过 SPA 回退让深层链接在刷新后仍可访问；`/` 会重定向到该页面。
尚未构建 `dist/` 时，`/` 和 `/ui` 返回 HTTP 503 并展示构建说明。构建控制台后重启服务即可启用。
无需执行 Node 构建，HTTP API、健康检查和离线 quickstart 仍可使用；旧的内联仪表盘已移除。

连接不同源的 API 时，使用 `VITE_API_BASE` 配置地址（见 `.env.example`）。

图谱第三方来源与 MIT 许可见 [Quartz 适配说明](vendor/quartz/README.md)。构建应保留 `licenses/quartz.txt`。
