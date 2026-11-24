# 自托管部署与运维

本指南面向使用仓库自带 Docker Compose 部署 MnemoTrace 的维护者。所有命令从仓库根目录运行，首次启动和签发 API 密钥见[快速开始](../README.zh-CN.md#快速开始)。

## 部署构成

[Compose](compose.yaml) 依次启动 PostgreSQL、运行 `migrate` 迁移任务和 `grant` 授权任务，再启动 `api` 和 `worker`。`admin` 用于按需管理 API 密钥。API 默认通过 `127.0.0.1:8100` 提供服务，宿主机端口可通过 `MNEMOTRACE_HTTP_PORT` 修改。

需要 Docker Compose 2.30.0 或以上版本，以支持模型配置文件的 `env_file.format: raw`。在宿主机运行验收脚本时还需要 `uv`。 [Compose 配置参考](https://docs.docker.com/reference/compose-file/services/#format)

## 配置

| 文件 | 用途 |
| --- | --- |
| 根目录 `.env` | 配置数据库初始化、迁移、运行和管理角色的密码，以及 API 端口等运行参数。首次启动前为四个角色分别生成不同的十六进制密码。 [数据库配置模板](.env.example) |
| `deploy/.env.models` | 配置补全和嵌入模型，可按需启用重排。API 和写入进程使用此文件配置模型服务。 [模型配置模板](.env.models.example) · [模型配置参考](../docs/reference/model-configuration.md) |

首次部署使用空数据库。数据库初始化脚本在空数据目录首次启动时创建角色。已有数据库卷上的角色密码通过数据库角色管理更新，并同步到相应配置。 [初始化脚本](init-postgres.sql) · [PostgreSQL 容器初始化说明](https://hub.docker.com/_/postgres)

自管 PostgreSQL 时，在迁移前将 `vector` 扩展安装到 `public` schema，并准备独立的迁移、运行和密钥管理角色。随附 Compose 连接其内置 `db` 服务。接入自管数据库时，需要调整各进程的连接和启动配置，可参考仓库的授权脚本。 [数据库授权](grant-roles.sql)

## 部署验收

按[快速开始](../README.zh-CN.md#快速开始)启动后，先查看服务状态和 API 健康检查。

```sh
docker compose --env-file .env -f deploy/compose.yaml ps -a
curl -fsS http://127.0.0.1:8100/livez
curl -fsS http://127.0.0.1:8100/readyz
```

- `migrate` 和 `grant` 成功退出后，`db`、`api` 和 `worker` 应处于健康状态。
- `/livez` 确认 HTTP 进程可响应，`/readyz` 核对数据库连接和迁移账本。
- 按快速开始运行 HTTP 验收脚本，检查模型调用、写入、召回和原文追溯。修改端口后，用 `--base-url` 指定实际地址。非本机地址使用 HTTPS。 [快速开始](../README.zh-CN.md#快速开始) · [验收脚本](../scripts/http_smoke.py)

## API 密钥管理

将签发返回的 `token` 保存到凭据存储，客户端用它构造 Bearer 请求头。查找和撤销密钥时使用 `key_id`。下面以 `local-demo` 为示例账号，撤销前将 `KEY_ID` 换成 `list` 返回的目标 ID。

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm admin list --owner-id local-demo
docker compose --env-file .env -f deploy/compose.yaml run --rm admin revoke --owner-id local-demo KEY_ID
```

轮换时先为同一账号签发新密钥，更新客户端配置并验证请求通过，再撤销旧密钥。

## 停机、备份与升级

使用 `stop` 或普通 `down` 停机时，Compose 保留 `postgres` 命名卷。`down --volumes` 会删除该卷及其中的数据库。 [Compose down 文档](https://docs.docker.com/reference/cli/docker/compose/down/)

需要备份时，使用 PostgreSQL 备份工具保存数据库，并记录对应的部署版本、数据库版本和模型配置。恢复先在独立实例验证数据库角色、pgvector、迁移账本和 HTTP 闭环。 [PostgreSQL 备份与恢复](https://www.postgresql.org/docs/18/backup-dump.html)

更新前先核对[变更日志](../CHANGELOG.md)里的兼容性说明。当前数据库基线从空库初始化，旧库无法就地升级到该基线。

## 常见故障

```sh
docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 db migrate grant api worker
docker compose --env-file .env -f deploy/compose.yaml run --rm migrate status
```

| 现象 | 优先检查 |
| --- | --- |
| `/readyz` 返回 `database_unavailable` | `db` 是否就绪，运行角色密码是否与数据库中的密码一致。 |
| `/readyz` 返回 `database_not_ready` | `migrate` 和 `grant` 的退出状态与日志，以及迁移账本是否匹配当前运行版本。 |
| 写入返回 `write_models_unavailable`，或 `worker` 一直未就绪 | 结合 `api` 和 `worker` 日志，核对补全与嵌入配置。 [模型排查](../docs/reference/model-configuration.md#应用配置并验收) |
| API 就绪但 HTTP 验收失败 | 核对端口、验收账号密钥和模型服务调用结果，再按脚本报错的阶段查看对应日志。 |
