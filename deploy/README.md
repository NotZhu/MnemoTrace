# 自托管部署与运维

部署 MnemoTrace HTTP API 和管理控制台。接口语义见 [HTTP API](../docs/reference/http-api.md)，模型与环境参数见[配置参考](../docs/reference/configuration.md)。

## 选择部署方式

| 方式 | 适用情况 |
| --- | --- |
| 生产 Compose 模板 | 构建控制台和 Python 运行镜像，连接已有 Postgres 数据库 |
| 源码启动 | 本地开发、调试或自行管理进程 |
| systemd 模板 | 已准备 Python 环境与部署目录的 Linux 主机 |

默认嵌入使用 hashing，不下载模型；自动事实抽取需要 LLM 配置和凭据，缺少时写入请求明确报错。真实嵌入、回答或图片描述需要对应 extras。Python wheel 本身不包含控制台构建。

## Compose

以下命令在仓库根目录的 Bash 中执行；PowerShell 可用 Copy-Item 完成同样的文件复制：

```sh
cp deploy/.env.example deploy/.env
```

按[数据库配置](../docs/reference/configuration.md#postgres-数据库基础)初始化独立的 Postgres/pgvector 数据库并签发 Bearer key。
在 `deploy/.env` 填写容器可连接的运行角色连接串。不要提交此文件。

启动服务：

```sh
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build
```

模板只绑定宿主机 127.0.0.1:8000，使用 UID/GID 10001、只读根文件系统和 `/ready` 健康检查。
外部访问通过自己的 HTTPS 反向代理或受控隧道提供，前端优先与 API 同源。
Compose 只传递模板列出的变量，额外模型设置需在自己的部署配置中明确传入。

`docker-compose.demo.yml` 默认绑定 127.0.0.1:8456。通过 `MNEMOTRACE_DEMO_DATABASE_URL` 提供独立演示库的运行角色连接串，并用为演示账号签发的 key 登录。

## 源码启动

在根目录安装 HTTP 依赖：

```sh
python -m pip install ".[server,llm,postgres]"
```

Bash 环境示例，请换成自己的运行角色连接串：

```sh
export MNEMOTRACE_DATABASE_URL="postgresql://app_runtime:REPLACE_ME@localhost:5432/mnemotrace"
export MNEMOTRACE_EMBEDDER="hashing"
export MNEMOTRACE_LLM="deepseek"
python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000 --workers 1
```

PowerShell 对应设置方式：

```powershell
$env:MNEMOTRACE_DATABASE_URL = "postgresql://app_runtime:REPLACE_ME@localhost:5432/mnemotrace"
$env:MNEMOTRACE_EMBEDDER = "hashing"
$env:MNEMOTRACE_LLM = "deepseek"
python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000 --workers 1
```

在前端目录执行 `pnpm install --frozen-lockfile` 与 `pnpm run build`，启动或重启服务后访问 `http://127.0.0.1:8000/ui/`。没有前端构建时，UI 返回 503，API 仍可单独使用。

开发依赖与 Vite 代理见[贡献指南](../CONTRIBUTING.md#控制台开发)。当前发布模板使用单 worker；更复杂的部署拓扑需要单独验证锁、后台整合和共享存储边界。

## systemd

[mnemotrace.service](mnemotrace.service) 约定源码和虚拟环境位于 /opt/mnemotrace，环境文件位于 /etc/mnemotrace/mnemotrace.env。将运行角色连接串配置在环境文件中。

先按自己的主机目录、Python 环境和凭据配置核对模板，再安装服务。模板使用 DynamicUser、文件系统限制和 SIGINT 优雅停止；直接复制到不同目录不会自动修正路径。更新后检查服务日志及 /ready。

## 鉴权与检查

客户端使用管理员签发的 Bearer key。服务在数据库中逐请求校验 key，并验证调用方对请求空间的归属。

| 检查 | 含义 |
| --- | --- |
| /health 的 ok | 进程可响应 |
| /ready 的 HTTP 200 与 ready=true | 已建立运行角色连接，数据库结构满足就绪条件 |
| 带正确 key 和 space_id 的 /v1/stats | 实际受保护路由可访问 |
| 预期会话的写入、读取和收尾 | 对应使用路径可工作，需使用专用测试空间 |

`/health`、`/ready` 和 `/metrics` 是公开探针，部署方可通过网络边界限制访问。

## 备份与恢复

应用的持久状态位于 Postgres。部署方按数据库的备份策略管理数据、角色和恢复验证。HTTP 导出用于共享和数据访问，默认会省略原文和敏感内容。

## 故障定位

- 401：核对 Bearer 头、key 的撤销状态和服务实例。
- 503 /ready：核对连接、运行角色和迁移状态。若仅 UI 返回 503，核对前端构建。
- 写入已受理或返回抽取错误：使用操作 ID 查看持久化进度，见 [HTTP API](../docs/reference/http-api.md)。

漏洞报告见[安全政策](../SECURITY.md)。本页的部署步骤不替代实际环境的验证。

示例模型需要对应的 DEEPSEEK_API_KEY；使用其他模型时替换名称及凭据。容器运行时传入凭据，不能写入镜像或提交到仓库。未配置模型的服务仍可读取已有记忆和管理显式事实。
