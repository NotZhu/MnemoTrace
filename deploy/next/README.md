# 新后端自托管部署

在 Docker 主机上部署 PostgreSQL 18 与 pgvector、HTTP `/v1` 服务和独立写入 worker。API 与 worker 共用通过 `backend/uv.lock` 构建的镜像，以非 root 用户运行。数据库留在 Compose 网络内，宿主机默认通过 `127.0.0.1:8100` 访问 API。远程访问时，在该入口前配置 HTTPS 反向代理。 [HTTP API](../../backend/docs/http-api.md)

## 首次启动

使用 Docker Engine 28.0 及以上、Docker Compose 2.30 及以上版本。 [Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)

进入 `deploy/next`，把 `.env.example` 复制为 `.env`，把 `.env.models.example` 复制为 `.env.models`。在 `.env` 中分别填写四个不同的随机密码。可用 `openssl rand -hex 32` 生成十六进制值，直接用于初始化与连接串。`.env.models` 使用原样读取的 `名称=值` 格式，填入补全和嵌入配置。API 与 worker 读取同一个模型配置文件。使用其他容器或宿主机上的模型时，配置从两个容器都可访问的 HTTPS 地址和密钥。两份文件留在本机并限制读取权限。 [模型配置](../../backend/docs/inference.md#环境变量) · [Docker Compose 环境文件](https://docs.docker.com/reference/compose-file/container-spec/#env_file)

```sh
docker compose config -q
docker compose up -d --wait db
docker compose build api
docker compose run --rm migrate
docker compose run --rm grant
docker compose up -d --wait --wait-timeout 90 api worker
docker compose run --rm admin issue --owner-id personal
```

`api` 的构建同时生成 worker 和管理命令使用的镜像。`db` 首次初始化时安装向量扩展，创建迁移、运行和凭据管理角色。`migrate` 执行版本迁移，`grant` 更新运行和管理角色权限。`admin issue` 输出包含一次性显示的 `token`，把它保存到调用方配置。 [PostgreSQL 角色和授权](../../backend/docs/postgres.md#运行账号与会话) · [密钥管理](../../backend/docs/http-api.md#管理凭据和启动)

只检查已有数据和任务时，可省略 `.env.models`，并在启动命令中只指定 `api`。如需在此模式下召回，按嵌入角色填写模型文件。API 健康检查调用 `/readyz`。worker 健康检查连接容器内的本机就绪端口，确认进程已装配模型配置并核对数据库迁移。若启动等待超时，可用 `docker compose logs --tail 50 worker` 检查 worker 启动状态。实际模型调用和任务处理用 [HTTP 闭环示例](../../backend/docs/http-api.md#http-闭环示例) 验证。

## 升级与停机

升级前保留 PostgreSQL 备份。执行 `docker compose stop worker api`，依次重新构建镜像、运行 `migrate` 和 `grant`、启动 API 与 worker，再运行闭环验收。可用 `docker compose run --rm migrate status` 核对账本。启动前完成迁移，恢复备份时使用与账本匹配的镜像。

停机使用 `docker compose stop worker api db`。数据库数据保留在 `postgres` 命名卷。worker 收到停止信号后停止领取任务，在途任务可由重启后的 worker 通过持久租约恢复。提交结果不明时，调用方用原键和原参数重送。 [写入与任务](../../backend/docs/http-api.md#写入与任务)

API 停机默认等待最多 45 秒，给在途请求留出完成时间。增大查询嵌入超时时，通过 `.env` 中的 `MNEMOTRACE_NEXT_API_STOP_GRACE_PERIOD` 调整宽限期，并为数据库查询和资源关闭预留时间。 [推理超时配置](../../backend/docs/inference.md#环境变量)

初次创建数据库使用 `.env` 中的密码。轮换已有库的登录密码时，先在 PostgreSQL 更新对应角色，再同步部署配置并重建使用该连接的容器。

## 验收

| 检查 | 成功条件 |
| --- | --- |
| 启动 | 启动命令成功，API 与 worker 均健康，`/livez` 和 `/readyz` 返回 200，迁移状态均为已应用。 |
| HTTP 闭环 | 用隔离的账号与合成输入运行[闭环示例](../../backend/docs/http-api.md#http-闭环示例)。验收写入、同键重送、召回及来源、清空和旧清空键重放。 |
| 任务恢复 | 在独立验收环境中停止 worker，提交写入后观察任务保持 `queued`。重启 worker 后原任务完成。进程异常中断时，未完成的 `running` 任务在租约到期后重新领取。 |
| 真实模型 | 在临时库使用实际部署的补全和嵌入端点运行一次同样的闭环示例，记录所用模型、嵌入修订和结果。 |
