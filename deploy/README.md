# 自托管部署与运维

部署 MnemoTrace HTTP API 和管理控制台。接口语义见 [HTTP API](../docs/reference/http-api.md)，模型与环境参数见[配置参考](../docs/reference/configuration.md)。

## 选择部署方式

| 方式 | 适用情况 |
| --- | --- |
| 生产 Compose 模板 | 构建控制台和 Python 运行镜像，持久卷保存记忆 |
| 源码启动 | 本地开发、调试或自行管理进程 |
| systemd 模板 | 已准备 Python 环境与部署目录的 Linux 主机 |

默认配置使用 hashing 和规则提取，不下载模型。真实嵌入、抽取、回答或图片描述需要安装对应 extras、提供模型配置和凭据。Python wheel 本身不包含控制台构建。

## Compose

以下命令在仓库根目录的 Bash 中执行；PowerShell 可用 Copy-Item 完成同样的文件复制：

```sh
cp deploy/.env.example deploy/.env
```

编辑 deploy/.env，将示例 key 替换为足够长的随机凭据。不要使用示例值或提交此文件。然后启动：

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
```

模板只绑定宿主机 127.0.0.1:8000，使用持久卷、非 root 用户、只读根文件系统与 /ready 健康检查。外部访问通过自己的 HTTPS 反向代理或受控隧道提供；前端优先与 API 同源。

容器使用 UID/GID 10001。改为宿主机 bind mount 时，提前确认该用户可以写入数据目录。Compose 只传递模板列出的变量；额外模型设置需在自己的部署配置中明确传入。

`docker-compose.demo.yml` 使用公开演示语义和不同端口/卷，不能代替有鉴权的生产模板。示例模式的 Bearer namespace 不具备生产凭据的保密性。

## 源码启动

在根目录安装 HTTP 依赖：

```sh
python -m pip install ".[server]"
```

Bash 环境示例，先替换 YOUR_API_KEY 并使用自己管理的数据目录：

```sh
export MNEMOTRACE_API_KEYS="user123:YOUR_API_KEY"
export MNEMOTRACE_DATA_DIR="/absolute/path/to/memory-data"
export MNEMOTRACE_EMBEDDER="hashing"
export MNEMOTRACE_LLM=""
python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000 --workers 1
```

PowerShell 对应设置方式：

```powershell
$env:MNEMOTRACE_API_KEYS = "user123:YOUR_API_KEY"
$env:MNEMOTRACE_DATA_DIR = "C:\path\to\memory-data"
$env:MNEMOTRACE_EMBEDDER = "hashing"
$env:MNEMOTRACE_LLM = ""
python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000 --workers 1
```

服务会按 namespace 保存 JSONL 记忆。控制台先在 frontend/ 执行 `pnpm install --frozen-lockfile` 与 `pnpm run build`，再启动或重启服务，访问 `http://127.0.0.1:8000/ui/`。没有前端构建时，UI 返回 503，API 仍可单独使用。

开发依赖与 Vite 代理见[贡献指南](../CONTRIBUTING.md#控制台开发)。当前发布模板使用单 worker；更复杂的部署拓扑需要单独验证锁、后台整合和共享存储边界。

## systemd

[mnemotrace.service](mnemotrace.service) 约定源码和虚拟环境位于 /opt/mnemotrace，环境文件位于 /etc/mnemotrace/mnemotrace.env，状态目录由 StateDirectory 管理。

先按自己的主机目录、Python 环境和凭据配置核对模板，再安装服务。模板使用 DynamicUser、文件系统限制和 SIGINT 优雅停止；直接复制到不同目录不会自动修正路径。更新后检查服务日志及 /ready。

## 鉴权与检查

生产使用 `MNEMOTRACE_API_KEYS=tenant:key`。同一 tenant 可同时接受多个 key，便于轮换。不要把 user_id、namespace 或浏览器本地选项当作服务器权限检查。

| 检查 | 含义 |
| --- | --- |
| /health 的 ok | 进程可响应 |
| /ready 的 HTTP 200 与 ready=true | 当前鉴权配置与数据目录满足实现的就绪条件 |
| 带正确 key 的 /v1/stats | 实际受保护路由可访问 |
| 预期会话的写入、读取和收尾 | 对应使用路径可工作，需使用专用测试空间 |

/ready 不代表模型质量、所有外部服务和全部业务功能已经通过验证。HTTP 默认拒绝匿名访问；开放或匿名模式只在明确受控的场景启用。当前 /health、/metrics 公开，部署方仍应按自己的暴露范围配置网络边界。

## 备份与恢复

1. 暂停导入、客户端写入与定时任务，优雅停止服务；异步整合需要足够的停止时间。
2. 复制整个数据目录或持久卷，保留所有 namespace、JSONL 集合、manifest 和权限。记录代码版本、embedder 身份、维度与必要配置；凭据另行安全管理。
3. 恢复到独立位置，使用相同运行组件，先验证加载与数据范围，再切换实际服务。
4. 模型变化走[重嵌入流程](../docs/architecture/memory.md#嵌入模型一致性)，升级前保留可恢复备份。

不要在持续写入时逐个复制集合文件并假定得到一致快照。完整 HTTP 导出适合可携带性检查，但不是包含全部运行状态和存储结构的原样备份；默认安全导出还会省略原文和敏感内容。

清空 namespace 不会删除外部备份、客户端导出或其他系统副本。备份包含私有原文与向量，应按用户数据管理，而不是提交到仓库。

## 故障定位

- 401：核对 Bearer 头、服务器 tenant:key 映射及是否连接到正确实例。
- 503 /ready：核对配置合法性与数据目录权限；UI 的 503 还可能表示尚未构建。
- 写入 queued 或 degraded：核对异步积压、模型配置及返回字段；先确认原始事件是否保存。
- 存储或模型不匹配：保留现场和备份，读取[持久化契约](../docs/architecture/memory.md)，不要通过删除数据或忽略错误来恢复表面可用。
- 文本已导入但没有会话结论：核对收尾入口、LLM 与 SESSION_OUTCOMES 配置，见 [Agent 指南](../docs/guides/agents.md)。

漏洞报告见[安全政策](../SECURITY.md)。本页的部署步骤不替代实际环境的验证。
