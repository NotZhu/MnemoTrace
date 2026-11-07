# 自托管部署与运维

部署 MnemoTrace HTTP API 和管理控制台。接口语义见 [HTTP API](../docs/reference/http-api.md)，模型与环境参数见[配置参考](../docs/reference/configuration.md)。

## 选择部署方式

| 方式 | 适用情况 |
| --- | --- |
| 生产 Compose | 启动 Postgres、API、控制台和独立任务 worker |
| 源码启动 | 本地开发、调试或自行管理进程 |
| systemd 模板 | 已准备 Python 环境与部署目录的 Linux 主机 |

默认嵌入使用 hashing，不下载模型；自动事实抽取需要 LLM 配置和凭据，缺少时写入请求明确报错。真实嵌入、回答或图片描述需要对应 extras。Python wheel 本身不包含控制台构建。

## Compose

以下命令在仓库根目录的 Bash 中执行；PowerShell 可用 Copy-Item 完成同样的文件复制：

```sh
cp deploy/.env.example deploy/.env
```

在 `deploy/.env` 填写引导密码和五条连接串。Compose 内部的主机、端口和库名固定为 `db:5432/mnemotrace`。引导连接使用 `postgres`，与 `MNEMOTRACE_POSTGRES_PASSWORD` 使用同一密码。为运行、发现、迁移及账号管理各指定不同的登录名和密码。URL 中的密码使用百分号编码。保护此文件的读权限，并将其留在本机。

```sh
dc() { docker compose --env-file deploy/.env -f deploy/docker-compose.yml "$@"; }
dc up -d --wait db
dc build mnemotrace
dc run --rm db-init
dc up -d --wait mnemotrace worker
dc run --rm admin create-user
dc run --rm admin issue-key --user-id USER_ID --name first
```

登录 `http://127.0.0.1:8000/ui/` 后创建空间，或使用 [HTTP 空间接口](../docs/reference/http-api.md)创建。原始 key 只在签发时返回，保存在受保护的客户端配置中。

数据库写入命名卷 `postgres`，只接入 Compose 内部数据库网络。API 绑定宿主机 `127.0.0.1:8000`，使用 UID/GID 10001、只读根文件系统和 `/ready` 健康检查。对外访问使用 HTTPS 反向代理。worker 使用独立的发现凭据领任务，并通过运行角色处理授权空间。API 与 worker 使用相同的抽取及嵌入设置。

`dc stop` 按依赖顺序停止应用和数据库。worker 收到停止信号后停止领取任务，宽限期内等待在途处理。进程中断后的未完成任务由现有租约机制恢复。保留命名卷即可用同一套连接配置重启。

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

开发依赖与 Vite 代理见[贡献指南](../CONTRIBUTING.md#控制台开发)。外部数据库按[数据库配置](../docs/reference/configuration.md#postgres-数据库基础)初始化。另开进程运行 `python -m mnemotrace.worker`，同时配置运行与发现连接，以及与 API 一致的模型设置。

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

使用 PostgreSQL 18 客户端的 `pg_dump -Fc` 备份完整数据库。归档含原文、事实、实体、版本、任务产物、回执和授权定义。数据库登录角色在目标环境重新配置。HTTP 导出用于交换数据，默认省略原文和敏感内容。

以下以 Bash 中的 `dc` 函数为例。把 `ARCHIVE` 设为仓库外受保护目录中的新归档绝对路径。二进制重定向应使用 Bash 或支持保留原始字节的 shell。独立备份会在成功或失败后尝试恢复源 API 与 worker。备份失败时以备份退出码为准，请另选新路径重试，只使用成功生成的归档。

```sh
dump_archive() (
    umask 077
    set -C
    dc exec -T db pg_dump -U postgres -d mnemotrace -Fc > "${ARCHIVE:?set the backup archive path}"
)

(
    : "${ARCHIVE:?set the backup archive path}"
    backup_status=0
    dc stop mnemotrace worker && dump_archive || backup_status=$?
    resume_status=0
    dc up -d --wait mnemotrace worker || resume_status=$?
    if [ "$backup_status" -ne 0 ]; then
        exit "$backup_status"
    fi
    exit "$resume_status"
)
```

用独立 Compose 项目和新的数据库卷恢复。在 `deploy/.env.restore` 配置与归档一致的角色前缀、目标登录凭据和独立的 API 端口。角色预置步骤会拒绝已有应用 schema 的目标。使用与备份匹配的 schema 版本启动恢复后的实例。

```sh
restore_dc() {
    docker compose -p mnemotrace-restore --env-file deploy/.env.restore -f deploy/docker-compose.yml "$@"
}
restore_dc up -d --wait db
restore_dc run --rm db-init python -m mnemotrace.admin init-db --bootstrap --provision-logins --roles-only
restore_dc exec -T db pg_restore -U postgres -d mnemotrace --single-transaction --exit-on-error < "$ARCHIVE"
restore_dc run --rm mnemotrace python -m mnemotrace.admin check-db
restore_dc up -d --wait mnemotrace
```

用已有 key 检查授权空间、原文、事实和任务回执，并核对另一个账号无法访问这些记录。确认模型设置与源部署一致，再运行 `restore_dc up -d worker` 并验证待办任务续跑。只恢复可信来源的归档，按部署侧保留策略保护及清理备份。

### Schema 升级

停用 API 和 worker，备份数据库后再升级。schema 6 及后续版本使用上节定义的函数，执行 `dc stop mnemotrace worker && dump_archive`，在整个迁移期间保持应用停机。从 schema 5 首次升级时，使用数据库运维侧的物理备份或停库后的存储快照。旧版逻辑归档中的两条约束在恢复时会改变目录表示，无法通过原指纹检查。

更新应用镜像后显式执行 `dc run --rm db-init`，再启动服务并验证 `/ready`。外部数据库使用迁移凭据运行 `python -m mnemotrace.admin init-db`。需要回退时，使用与备份匹配的应用和 schema。

## 故障定位

- 401：核对 Bearer 头、key 的撤销状态和服务实例。
- 503 /ready：核对连接、运行角色和迁移状态。若仅 UI 返回 503，核对前端构建。
- 写入已受理或返回抽取错误：使用操作 ID 查看持久化进度，见 [HTTP API](../docs/reference/http-api.md)。

漏洞报告见[安全政策](../SECURITY.md)。本页的部署步骤不替代实际环境的验证。

示例模型需要对应的 DEEPSEEK_API_KEY；使用其他模型时替换名称及凭据。容器运行时传入凭据，不能写入镜像或提交到仓库。未配置模型的服务仍可读取已有记忆和管理显式事实。
