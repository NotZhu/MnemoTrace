# 自托管部署与运维

本指南面向使用仓库自带 Docker Compose 部署 MnemoTrace 的维护者。所有命令从仓库根目录运行，首次启动和签发 API 密钥见[快速开始](../README.zh-CN.md#快速开始)。

## 部署构成

[Compose](compose.yaml) 依次启动 PostgreSQL、运行 `migrate` 迁移任务，再启动 `api` 和 `worker`。`admin` 用于按需管理 API 密钥。四个应用进程使用 `MNEMOTRACE_POSTGRES_DSN`。API 默认通过 `127.0.0.1:8100` 提供服务，宿主机端口可通过 `MNEMOTRACE_HTTP_PORT` 修改。

需要 Docker Compose 2.30.0 或以上版本，以支持模型配置文件的 `env_file.format: raw`。在宿主机运行验收脚本时还需要 `uv`。 [Compose 配置参考](https://docs.docker.com/reference/compose-file/services/#format)

## 配置

| 文件 | 用途 |
| --- | --- |
| 根目录 `.env` | 填写应用数据库账号的随机十六进制密码 `MNEMOTRACE_DB_PASSWORD`。可以用 `openssl rand -hex 32` 生成。此文件也可配置 API 端口等运行参数。 [数据库配置模板](.env.example) |
| `deploy/.env.models` | 配置补全和嵌入模型，可按需启用重排。API 和写入进程使用此文件配置模型服务。 [模型配置模板](.env.models.example) · [模型配置参考](../docs/reference/model-configuration.md) |

首次部署使用空数据库卷。数据库容器初始化 `pgvector` 和非超级用户 `mnemotrace_app`，将 `mnemotrace` schema 交给该账号，并清除 PostgreSQL 初始化管理员的临时口令。应用账号通过 SCRAM 登录，拥有自己 schema 中的表、迁移账本和 API 密钥表，可修改结构及记录。数据库管理员通过容器内 Unix socket 使用 peer 认证。 [初始化脚本](init-postgres.sql) · [PostgreSQL 容器初始化说明](https://hub.docker.com/_/postgres)

API 和 worker 等待池连接的时限为 3 秒。`MNEMOTRACE_DB_LOCK_TIMEOUT_SECONDS` 默认 1 秒，`MNEMOTRACE_DB_STATEMENT_TIMEOUT_SECONDS` 默认 15 秒，`MNEMOTRACE_DB_MAINTENANCE_TIMEOUT_SECONDS` 默认 60 秒。维护时限应用于会话擦除、空间清空和空间删除的单条语句。配置须满足等锁时限小于普通语句时限，且普通语句时限小于等于维护语句时限。

## 自管 PostgreSQL 分权

自管部署自行设置各进程的 `MNEMOTRACE_POSTGRES_DSN`。按下列顺序开通数据库。随附 Compose 连接其内置 PostgreSQL。

1. 由 DBA 准备有权连接目标数据库的迁移、运行和密钥管理账号。在目标库将 `vector` 扩展安装到 `public` schema，并创建 `mnemotrace` schema，由迁移账号拥有。
2. 在迁移进程的环境中配置迁移账号的连接串，执行 `python -m mnemotrace.adapters.postgres.migrate up`。迁移成功后，用该账号执行授权脚本。下列命令假设 `psql` 的连接配置已指向目标库的迁移账号。按实际部署填写 `-v` 的角色名。

   ```sh
   psql -X -v ON_ERROR_STOP=1 -v runtime_role=mnemotrace_runtime \
     -v admin_role=mnemotrace_admin -f deploy/grant-roles.sql
   ```

3. 将 API 和 worker 的连接串设为运行账号，将凭据管理 CLI 的连接串设为密钥管理账号。每次应用新迁移后按同一顺序核对授权并验收。 [授权脚本](grant-roles.sql)

## 部署验收

按[快速开始](../README.zh-CN.md#快速开始)启动后，先查看服务状态和 API 健康检查。

```sh
docker compose --env-file .env -f deploy/compose.yaml ps -a
curl -fsS http://127.0.0.1:8100/livez
curl -fsS http://127.0.0.1:8100/readyz
```

- `migrate` 成功退出后，`db`、`api` 和 `worker` 应处于健康状态。
- `/livez` 确认 HTTP 进程可响应，`/readyz` 核对数据库连接和迁移账本。
- 按快速开始运行 HTTP 验收脚本，检查模型调用、写入、召回和原文追溯。修改端口后，用 `--base-url` 指定实际地址。非本机地址使用 HTTPS。 [快速开始](../README.zh-CN.md#快速开始) · [验收脚本](../scripts/http_smoke.py)

## API 密钥管理

将签发返回的 `token` 保存到凭据存储，客户端用它构造 Bearer 请求头。查找和撤销密钥时使用 `key_id`。下面以 `local-demo` 为示例账号，撤销前将 `KEY_ID` 换成 `list` 返回的目标 ID。

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm admin list --owner-id local-demo
docker compose --env-file .env -f deploy/compose.yaml run --rm admin revoke --owner-id local-demo KEY_ID
```

轮换时先为同一账号签发新密钥，更新客户端配置并验证请求通过，再撤销旧密钥。

## 数据库密码轮换

轮换内置 PostgreSQL 的应用账号密码时，先停止 `api` 和 `worker`。通过容器内的管理员连接使用 `\password mnemotrace_app`，按提示输入新的随机十六进制密码。

```sh
docker compose --env-file .env -f deploy/compose.yaml stop api worker
docker compose --env-file .env -f deploy/compose.yaml exec --user postgres db psql -d mnemotrace
```

退出 `psql` 后更新根目录 `.env` 中的 `MNEMOTRACE_DB_PASSWORD`，执行 `docker compose --env-file .env -f deploy/compose.yaml up --wait` 重新创建使用新配置的服务，然后运行部署验收。数据库卷内的账号密码通过 `\password` 更新，Compose 环境变量为应用提供新连接串。

## 停机、备份与升级

使用 `stop` 或普通 `down` 停机时，Compose 保留 `postgres` 命名卷。`down --volumes` 会删除该卷及其中的数据库。 [Compose down 文档](https://docs.docker.com/reference/cli/docker/compose/down/)

需要备份时，从数据库容器内以 `postgres` 系统用户使用 PostgreSQL 备份工具，并记录对应的部署版本、数据库版本和模型配置。恢复先在独立实例验证数据库角色、pgvector、迁移账本和 HTTP 闭环。 [PostgreSQL 备份与恢复](https://www.postgresql.org/docs/18/backup-dump.html)

当前开发基线从空数据库卷初始化。已有四凭据部署时，为新版使用新的 Compose 项目名（例如在启动和验收命令中统一加 `-p mnemotrace-single`）和空卷。若两套服务同时运行，为新版设置独立的 API 端口。继续保留旧项目和数据卷。 [变更日志](../CHANGELOG.md)

## 常见故障

```sh
docker compose --env-file .env -f deploy/compose.yaml logs --tail=100 db migrate api worker
docker compose --env-file .env -f deploy/compose.yaml run --rm migrate status
```

| 现象 | 优先检查 |
| --- | --- |
| `/readyz` 返回 `database_unavailable` | `db` 是否就绪，应用账号密码是否与数据库中的密码一致。 |
| 迁移任务失败，或 `/readyz` 返回 `database_not_ready` | 查看 `migrate` 日志，核对 schema、`public` 中的 `vector` 扩展及账本是否匹配当前运行版本。 |
| 写入返回 `write_models_unavailable`，或 `worker` 一直未就绪 | 结合 `api` 和 `worker` 日志，核对补全与嵌入配置。 [模型排查](../docs/reference/model-configuration.md#应用配置并验收) |
| API 就绪但 HTTP 验收失败 | 核对端口、验收账号密钥和模型服务调用结果，再按脚本报错的阶段查看对应日志。 |
