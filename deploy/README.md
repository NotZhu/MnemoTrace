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

首次部署使用空数据库卷。数据库容器初始化 `vector`（pgvector）和 `btree_gist` 扩展，创建非超级用户 `mnemotrace_app`，将 `mnemotrace` schema 交给该账号，并清除 PostgreSQL 初始化管理员的临时口令。应用账号通过 SCRAM 登录，拥有自己 schema 中的表、迁移账本和 API 密钥表，可修改结构及记录。数据库管理员通过容器内 Unix socket 使用 peer 认证。 [初始化脚本](init-postgres.sql) · [PostgreSQL 容器初始化说明](https://hub.docker.com/_/postgres)

API 和 worker 通过 `MNEMOTRACE_DB_POOL_TIMEOUT_SECONDS` 设置等待池连接的时限，接受正整数秒，默认 3 秒。新建数据库连接时使用 DSN 的 `connect_timeout`，填写十进制整数秒，例如 `connect_timeout=5`。省略时使用 3 秒。

`MNEMOTRACE_DB_LOCK_TIMEOUT_SECONDS` 默认 1 秒，`MNEMOTRACE_DB_STATEMENT_TIMEOUT_SECONDS` 默认 15 秒，`MNEMOTRACE_DB_MAINTENANCE_TIMEOUT_SECONDS` 默认 60 秒。维护时限应用于会话擦除、空间清空和空间删除的单条语句。配置须满足等锁时限小于普通语句时限，且普通语句时限小于等于维护语句时限。

在根目录 `.env` 中配置 worker 的消费者数量。三项均接受 1 至 16 的整数。

| 任务 | Compose 环境变量 | 独立 worker 参数 | 默认数量 |
| --- | --- | --- | ---: |
| 记忆写入 | `MNEMOTRACE_WORKER_CONCURRENCY` | `--concurrency` | 2 |
| 画像刷新 | `MNEMOTRACE_WORKER_PROFILE_CONCURRENCY` | `--profile-concurrency` | 1 |
| 会话摘要 | `MNEMOTRACE_WORKER_SUMMARY_CONCURRENCY` | `--summary-concurrency` | 1 |

每个 worker 的数据库连接池上限为三类消费者数量之和。部署多个副本时，将所有副本的连接池上限和 API 等进程的连接数一并计入 PostgreSQL 连接预算。三类任务的补全请求共享进程内的 `MNEMOTRACE_INFERENCE_MAX_IN_FLIGHT`（默认 4）。模型并发已满时，增加消费者会增加等待。

已有部署修改并发后，运行 `docker compose --env-file .env -f deploy/compose.yaml up -d --no-deps --wait worker` 应用配置并等待就绪探针通过。查看 worker 启动日志，核对三类并发和 `max_db_connections`。

`MNEMOTRACE_EXTRACTION_TIMEZONE` 接受 IANA 时区名称，默认 `UTC`。API 用它校验新写入任务的发言时刻，worker 用它解释「今天」等相对日期。API 和所有 worker 须使用相同值。切换时区前暂停接收新写入，等已有写入任务进入终态，再同时更新 API 和所有 worker 的配置。

## 自管 PostgreSQL 分权

自管部署自行设置各进程的 `MNEMOTRACE_POSTGRES_DSN`。按下列顺序开通数据库。随附 Compose 连接其内置 PostgreSQL。

1. 由 DBA 准备有权连接目标数据库的迁移、运行和密钥管理账号。在目标库的 `public` schema 安装 `vector` 和 `btree_gist` 扩展，并创建由迁移账号拥有的 `mnemotrace` schema。
2. 在迁移进程的环境中配置迁移账号的连接串，执行 `python -m mnemotrace.adapters.postgres.migrate up`。迁移成功后，用该账号执行授权脚本。下列命令假设 `psql` 的连接配置已指向目标库的迁移账号。按实际部署填写 `-v` 的角色名。

   ```sh
   psql -X -v ON_ERROR_STOP=1 -v runtime_role=mnemotrace_runtime \
     -v admin_role=mnemotrace_admin -f deploy/grant-roles.sql
   ```

3. 将 API 和 worker 的连接串设为运行账号，将凭据管理 CLI 的连接串设为密钥管理账号。用 `python -m mnemotrace.admin issue` 为默认账号签发密钥，再用该密钥调用 `POST /v1/spaces` 创建空间。每次应用新迁移后按同一顺序核对授权并验收。 [授权脚本](grant-roles.sql) · [HTTP 接入指南](../docs/reference/http-api.md)

## 部署验收

按[快速开始](../README.zh-CN.md#快速开始)启动后，先查看服务状态和 API 健康检查。

```sh
docker compose --env-file .env -f deploy/compose.yaml ps -a
curl -fsS http://127.0.0.1:8100/livez
curl -fsS http://127.0.0.1:8100/readyz
```

- `migrate` 成功退出后，`db`、`api` 和 `worker` 应处于健康状态。
- `/livez` 确认 HTTP 进程可响应。`/readyz` 在 3 秒总预算内取得池连接并核对迁移账本。数据库连接池等待时限可单独配置，调大后就绪检查仍使用 3 秒总预算。Compose 的 HTTP 探针等待 4 秒，给服务留出返回错误响应的时间。
- 按快速开始运行 HTTP 验收脚本，检查模型调用、写入、召回和原文追溯。修改端口后，用 `--base-url` 指定实际地址。非本机地址使用 HTTPS。 [快速开始](../README.zh-CN.md#快速开始) · [验收脚本](../scripts/http_smoke.py)

## API 密钥管理

内置 Compose 的 `admin bootstrap` 为默认账号 `owner` 签发首把密钥并创建名为 `default` 的空间，命令成功后返回 `token` 和 `space_id`。若该账号已有空间或密钥记录，命令会提示已配置。使用现有密钥调用 `GET /v1/spaces` 找到所需的空间。若首次运行时丢失了输出，先用 `admin issue` 签发新密钥，再查找空间并撤销遗失的密钥。 [HTTP 接入指南](../docs/reference/http-api.md)

签发的 `token` 由 `mnemo_` 前缀和 32 个随机字符组成，共 38 字符。随机部分只使用 ASCII 大小写字母和数字。服务端保存整串凭据的 SHA-256 摘要，客户端保存完整 `token` 并用它构造 Bearer 请求头。

`issue`、`list` 和 `revoke` 默认操作 `owner`，也可用 `--owner-id` 选择其他账号。撤销前将 `KEY_ID` 换成 `list` 返回的目标 ID。

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm admin issue
docker compose --env-file .env -f deploy/compose.yaml run --rm admin list
docker compose --env-file .env -f deploy/compose.yaml run --rm admin revoke KEY_ID
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

### 判断旧卷能否复用

复用旧卷前，先用 `SHOW server_version` 确认旧库运行的 PostgreSQL 大版本与目标 `db` 镜像一致。然后保持旧数据库运行，从目标版本的代码目录用原项目名和数据库配置检查应用迁移账本。将 `SOURCE_PROJECT` 换成原项目名，默认是 `mnemotrace`，将 `REVISION` 换成此次构建的标识。选择一个未被现有部署使用的镜像标签。 [PostgreSQL 大版本升级](https://www.postgresql.org/docs/18/upgrading.html)

```sh
candidate_compose() {
  MNEMOTRACE_IMAGE=mnemotrace:candidate-REVISION docker compose --env-file .env -f deploy/compose.yaml -p SOURCE_PROJECT "$@"
}
candidate_compose build api &&
  candidate_compose run --rm --no-deps migrate status
```

**复用原卷。** 迁移全部显示「已应用」时，按下一节备份源项目，备份完成后保持源 API 和 worker 停止。回到定义 `candidate_compose` 的终端和目标代码目录，在目标 `.env` 中将 `MNEMOTRACE_IMAGE` 设为刚才检查过的镜像标签，供后续启动和维护使用。用同一个 `candidate_compose` 命令切换原项目，并将下例地址替换为实际服务地址。

```sh
candidate_compose up --wait &&
  curl -fsS http://127.0.0.1:8100/readyz
```

就绪检查通过后，用已有密钥读取原空间并召回一条已知记忆，再恢复 Agent 流量。 [部署验收](#部署验收)

**启用新卷。** 遇到校验和不匹配、目标基线尚未应用或 PostgreSQL 大版本不同时，用新的项目名和空卷启动目标版本，保留旧项目和卷。四凭据部署继续用原版本和原数据库配置维护。两套服务同时运行时使用不同的 API 端口。 [变更日志](../CHANGELOG.md)

### 备份单账号 Compose

从备份对应版本的代码目录执行以下命令。示例使用默认项目名，指定过项目名的部署须在命令中继续使用同一个 `-p`。记录应用版本、PostgreSQL 版本、pgvector 版本和模型配置标识，另行保管所需凭据。备份包含记忆数据和 API 密钥校验值，应保存在访问受限的位置。

```sh
source_compose() { docker compose --env-file .env -f deploy/compose.yaml "$@"; }
(
  set -e
  source_compose stop api worker
  backup_dir=$(mktemp -d "$HOME/mnemotrace-backup.XXXXXXXX")
  backup_file="$backup_dir/mnemotrace.dump"
  container_dump=$(source_compose exec -T --user postgres db mktemp /tmp/mnemotrace-backup.XXXXXXXX)
  source_compose exec -T --user postgres db pg_dump -Fc --schema=mnemotrace -d mnemotrace -f "$container_dump"
  source_compose exec -T --user postgres db pg_restore --list "$container_dump"
  source_compose cp "db:$container_dump" "$backup_file"
  source_compose exec -T --user postgres db rm -- "$container_dump"
  printf '%s\n' "$backup_file"
)
```

命令在任一步失败时停止，成功后打印备份文件路径。单独备份后，可在源项目执行 `source_compose up --wait` 恢复服务。 [pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html) · [Compose cp](https://docs.docker.com/reference/cli/docker/compose/cp/)

### 恢复到独立实例

使用与备份相同的代码、数据库镜像及模型配置。复制配置到 `.env.restore`，设置目标项目的数据库密码、独立 API 端口及专用镜像标签（如 `MNEMOTRACE_IMAGE=mnemotrace:restore`）。首次恢复时选择未使用过的项目名。示例使用 `mnemotrace-restore`。将下方 `backup_file` 设为实际归档的绝对路径。目标项目先只启动数据库，由初始化脚本准备角色和扩展。

```sh
backup_file=/absolute/path/to/mnemotrace.dump
restore_compose() (
  unset MNEMOTRACE_IMAGE MNEMOTRACE_DB_PASSWORD MNEMOTRACE_HTTP_PORT
  docker compose --env-file .env.restore -f deploy/compose.yaml -p mnemotrace-restore "$@"
)
(
  set -e
  restore_compose up -d --wait db
  restore_compose cp "$backup_file" db:/tmp/mnemotrace-restore.dump
  restore_compose exec -T --user root db chown postgres:postgres /tmp/mnemotrace-restore.dump
  restore_compose exec -T --user postgres db chmod 600 /tmp/mnemotrace-restore.dump
  restore_compose exec -T --user postgres db pg_restore --list /tmp/mnemotrace-restore.dump
  restore_compose exec -T --user postgres db psql -X -v ON_ERROR_STOP=1 -d mnemotrace -c 'DROP SCHEMA IF EXISTS mnemotrace RESTRICT'
  restore_compose exec -T --user postgres db pg_restore --single-transaction --exit-on-error -d mnemotrace /tmp/mnemotrace-restore.dump
  restore_compose exec -T --user postgres db rm -- /tmp/mnemotrace-restore.dump
)
```

`RESTRICT` 要求已存在的目标 schema 为空。若删除失败，先核对项目名和卷。若 `pg_restore` 失败并回滚，可以修正原因后在同一目标项目重跑恢复命令。

归档恢复成功后，构建目标镜像并查看账本。如果换了终端，先重新定义上面的 `restore_compose` 函数。

```sh
restore_compose build api &&
  restore_compose run --rm --no-deps migrate status
```

账本全部显示「已应用」后执行 `restore_compose up --wait`。确认 `/readyz` 返回成功，使用备份前的 API Key 读取原空间和原文，并召回该空间里的已知记忆，再切换到恢复实例。 [pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html)

## 常见故障

从目标实例对应版本的代码目录执行以下命令。将 `project` 和 `env_file` 设为该实例启动时使用的 Compose 项目名和环境文件。默认部署使用 `mnemotrace` 和 `.env`，上文的恢复示例使用 `mnemotrace-restore` 和 `.env.restore`。

```sh
project=mnemotrace
env_file=.env
instance_compose() { docker compose --env-file "$env_file" -f deploy/compose.yaml -p "$project" "$@"; }
instance_compose logs --tail=100 db migrate api worker
instance_compose run --rm migrate status
```

| 现象 | 优先检查 |
| --- | --- |
| `/readyz` 返回 `database_unavailable` | `db` 是否就绪，应用账号密码是否与数据库中的密码一致。 |
| 迁移任务失败，或 `/readyz` 返回 `database_not_ready` | 查看 `migrate` 日志，核对 schema、`public` 中的 `vector` 扩展及账本是否匹配当前运行版本。 |
| 写入返回 `write_models_unavailable`，或 `worker` 一直未就绪 | 结合 `api` 和 `worker` 日志，核对补全与嵌入配置。 [模型排查](../docs/reference/model-configuration.md#应用配置并验收) |
| API 就绪但 HTTP 验收失败 | 核对端口、验收账号密钥和模型服务调用结果，再按脚本报错的阶段查看对应日志。 |

### 恢复后台写入

任务在 PostgreSQL 中排队。发现任务长时间停在 `queued` 或 `running` 时，先查看上面的 `worker` 日志，确认它能访问数据库和模型服务。若 worker 已停止，在定义了 `instance_compose` 的同一终端运行以下命令恢复目标实例的处理。

```sh
instance_compose up -d worker
```

每次领取后，单次执行限时 180 秒，租约有效期为 210 秒。worker 突然退出时，`running` 可持续到当前租约到期。可用的 worker 随后会领取仍有尝试次数的任务并继续处理同一 `operation_id`。旧 worker 恢复后，其过期租约无法提交结果。用原任务 ID 检查 `status` 和 `attempts`。 [客户端幂等重试与恢复](../docs/reference/http-api.md#幂等重试与恢复)

更新 worker 时，先停止旧 worker，再启动新版本并确认健康检查通过。通过画像读取接口检查积压任务的刷新状态。耗尽尝试次数的画像任务在租约到期后结算，有后续刷新时按原定时间重新排队，否则报告 `worker_lease_expired`。 [画像刷新状态](../docs/reference/http-api.md#用户画像)
