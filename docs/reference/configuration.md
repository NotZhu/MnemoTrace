# 配置参考

配置分为 Python 的 Config、服务/MCP 环境变量和命令参数。它们不是一套自动互相覆盖的全局配置；以各入口的构造与解析逻辑为准。

## Python Config

`from mnemotrace import Config`，再传给 `MemoryEngine(config=...)`。完整字段和精确默认值在 [config.py](../../mnemotrace/config.py) 维护。

`Config.from_env()` 返回应用默认值。数据库、模型和服务的环境变量由各自部署入口读取。

创建 `Config` 时请使用关键字参数，例如 `Config(top_k=5)`；字段顺序可能随配置分组调整。

## Postgres 数据库基础

安装 `python -m pip install ".[postgres]"`；已发布安装使用 `mnemotrace-memory[postgres]`。
`serve` 和 `all` 包含此 extra。产品 HTTP 服务、可信直连 MCP 和 CLI 使用运行角色连接数据库。
纯算法核心可单独安装。

支持 Postgres 18 与 pgvector 0.8.7，测试镜像及摘要在 [CI](../../.github/workflows/ci.yml) 固定。
数据库使用 `mnemotrace` schema；不读取、迁入、删除或覆盖旧记忆目录。

`DatabaseConfig` 的参数与默认值以[定义](../../mnemotrace/store/postgres/database.py)为准，
它独立于算法 `Config`，不从已有文件后端配置推导连接。

| 环境变量 | 用途 |
| --- | --- |
| MNEMOTRACE_DATABASE_URL | 普通运行连接，不含账号管理或 DDL 权限 |
| MNEMOTRACE_MIGRATION_DATABASE_URL | 显式初始化/升级；运行时不自动迁移 |
| MNEMOTRACE_DISCOVERY_DATABASE_URL | 独立 worker 发现连接，领取后的业务处理仍使用运行连接 |
| MNEMOTRACE_ADMIN_DATABASE_URL | 账号与 key 管理，不能作为普通数据连接 |
| MNEMOTRACE_BOOTSTRAP_DATABASE_URL | 仅首次角色/扩展配置使用的 DBA 连接 |
| MNEMOTRACE_DB_ROLE_PREFIX | 角色名称前缀；初始化后不可直接更换 |
| MNEMOTRACE_DB_POOL_MIN / MNEMOTRACE_DB_POOL_MAX | 每个池的最小/最大连接数 |
| MNEMOTRACE_DB_CONNECT_TIMEOUT / MNEMOTRACE_DB_POOL_TIMEOUT | 建连和取连接超时，单位秒 |
| MNEMOTRACE_DB_OPEN_TIMEOUT | 显式打开连接池的最大等待秒数 |

凭据存放在受保护的环境或本地配置中，不作为管理命令参数，也不把真实连接串提交到仓库。
首次配置分两步进行：DBA 先创建无登录权限的角色与扩展，再配置四种独立登录账号：

```python
from mnemotrace.store.postgres import DatabaseConfig
from mnemotrace.store.postgres.migrate import bootstrap_roles

bootstrap_roles(DatabaseConfig.from_env("bootstrap"))
```

DBA 在目标库内执行以下 SQL，使用 `psql` 的 `\password` 分别交互设置密码；普通登录账号不授予
SUPERUSER、BYPASSRLS、CREATEROLE 或 CREATEDB。以下采用默认角色前缀：

```sql
CREATE ROLE app_runtime LOGIN IN ROLE mnemotrace_runtime;
CREATE ROLE app_migration LOGIN IN ROLE mnemotrace_migration;
CREATE ROLE app_account_admin LOGIN IN ROLE mnemotrace_account_admin;
CREATE ROLE app_worker_discovery LOGIN IN ROLE mnemotrace_worker_discovery;
```

将四种登录凭据分别配置到对应环境变量，再显式初始化和创建账号：

```sh
python -m mnemotrace.admin init-db
python -m mnemotrace.admin create-user
python -m mnemotrace.admin issue-key --user-id USER_ID --name first
python -m mnemotrace.admin list-keys --user-id USER_ID
python -m mnemotrace.admin rotate-key --user-id USER_ID --key-id KEY_ID
python -m mnemotrace.admin revoke-key --user-id USER_ID --key-id KEY_ID
```

`init-db --bootstrap` 可在已配置 DBA 与迁移凭据时依次重复验证角色/扩展并初始化。
重复初始化不重建表；升级只执行待应用迁移。结构损坏、脚本校验和不一致和未知版本会拒绝运行，
不能通过重新初始化静默修复。数据库表、约束与策略以[迁移资源](../../mnemotrace/store/postgres/sql/)为准。

签发和轮换成功时 JSON 输出包含一次性的原始 key；列表只返回标识和时间。
不要把这两个命令的输出写入日志。撤销后下一次认证失效；轮换撤销旧 key 与签发新 key 同事务完成。
`delete-user --user-id USER_ID` 锁定并删除账号及全部关联数据，失败回滚，重复执行安全；此命令具有真实删除副作用。

受信任 Python 调用方使用运行连接认证并创建、选择空间：

```python
import os
from mnemotrace.store.postgres import Database, DatabaseConfig, Spaces

with Database(DatabaseConfig.from_env()) as database:
    spaces = Spaces(database)
    identity = spaces.authenticate(os.environ["MNEMOTRACE_USER_API_KEY"])
    space = spaces.create(identity.user_id, "Research")
    context = spaces.authorize(identity.user_id, space.space_id)
    page = spaces.list(identity.user_id, limit=100)
    # 后续页传 after_id=page[-1].space_id；列表按稳定 ID 排序。
```

用户身份属于可信输入，直接构造 `SpaceContext` 不等于授权。
每次空间事务重新验证账号和空间状态，写入按用户→空间顺序加锁；只读事务使用一致数据库快照。
认证通过受限 SECURITY DEFINER 函数查询摘要，运行角色不能读取 key 表。
用户级 RLS 同时约束读写且始终开启；同用户空间范围仍需 DAO 限制，不能用用户级 RLS 代替空间授权。
原始数据库凭据只交给受信任服务端，不交给终端用户。

数据库回归使用可创建临时库/角色的专用 DBA 连接：

```sh
python -m pytest tests/postgres tests/test_postgres_configuration.py
```

通过 `MNEMOTRACE_TEST_POSTGRES_DSN` 指向测试服务器；fixture 每次创建独立数据库与角色，清理前验证归属标记。
未配置时本地显式跳过数据库测试；`MNEMOTRACE_REQUIRE_POSTGRES=1` 时缺少连接或发生跳过均失败。
CI 的 Linux 使用固定镜像，Windows 启动专用原生 Postgres 并编译相同 pgvector 版本，均使用实际运行凭据验证隔离。

[进程恢复与并发回归](../../tests/postgres/test_process_recovery.py)也由上述入口收集。
测试使用 `spawn` 启动独立进程，各进程自行建立连接；业务处理使用 runtime 角色，后台发现和账号操作分别使用 discovery 与 admin 角色。
DBA 连接用于临时库/角色初始化、清理及锁等待观察，业务读写通过实际运行角色验证。
测试仅使用合成输入、固定模型响应和本地 embedder，在提交边界同步并强制终止测试进程，结束时回收子进程。
回归覆盖范围见[存储契约中的行为对照](../architecture/storage-contracts.md#行为对照与验证输入)。

## 窗口抽取预算

对话联合输出使用 `joint_max_output_tokens`、`joint_core_output_tokens`、`joint_digest_output_tokens`，分别对应 `MNEMOTRACE_JOINT_MAX_OUTPUT_TOKENS`、`MNEMOTRACE_JOINT_CORE_OUTPUT_TOKENS`、`MNEMOTRACE_JOINT_DIGEST_OUTPUT_TOKENS`。
默认值维护在 [Config](../../mnemotrace/config.py)，只是工程起点。规划同时预留核心结果、逐条长消息摘要及格式开销，输出不足时缩小窗口，不复用独立摘要的输出上限。
可用 `joint_context_tokens` / `MNEMOTRACE_JOINT_CONTEXT_TOKENS` 声明提供方总上下文限制；规划使用输入 UTF-8 字节作为保守 token 上界，同时预留完整联合输出上限。未声明时不声称已验证提供方上下文容量。截断响应整体失败。

缓冲请求将原文和任务提交到 Postgres。后台处理由独立 worker 执行，部署方式见[部署指南](../../deploy/README.md)。对话窗口在最后活动后空闲 60 秒、达到预算或显式刷新时可处理，原文和文档的抽取任务提交后即可领取。显式刷新也可主动推进本次等待范围内的任务。各来源共用以下输入配置。

| Config 字段 | 服务环境变量 | 含义 |
| --- | --- | --- |
| extraction_max_input_bytes | MNEMOTRACE_EXTRACTION_MAX_INPUT_BYTES | 系统与用户提示词的 UTF-8 字节总预算，包含来源元数据 |
| extraction_context_bytes | MNEMOTRACE_EXTRACTION_CONTEXT_BYTES | 每窗前文正文最多占用的 UTF-8 字节数；空间不足时优先减少前文 |

默认值以 [Config 定义](../../mnemotrace/config.py) 为准。输入使用可离线计算、对中英文都明确的字节预算，
并非 provider 的精确 token 计数。输入和输出预算必须是正整数，前文预算可以为零。
调小输入预算会拆成更多窗口；调大输出预算可能增加调用成本。更改配置会阻止旧结果回写；显式重试从已保存覆盖范围继续规划。
原文需保持不变；修改原文后不能复用已有处理范围。

## 条件来源摘要

| Config 字段 | 服务环境变量 | 含义 |
| --- | --- | --- |
| digest_min_chars | MNEMOTRACE_DIGEST_MIN_CHARS | 原文达到此 Python 字符数才请求摘要 |
| digest_max_chars | MNEMOTRACE_DIGEST_MAX_CHARS | 成功摘要的字符上限，必须小于生成阈值 |
| digest_max_input_bytes | MNEMOTRACE_DIGEST_MAX_INPUT_BYTES | 系统和用户提示词合计 UTF-8 字节预算 |
| digest_max_output_tokens | MNEMOTRACE_DIGEST_MAX_OUTPUT_TOKENS | 每次摘要调用的输出 token 上限 |

四项均为正整数，精确默认值在 [Config](../../mnemotrace/config.py) 中维护。这些是工程初值，未完成真实模型质量或成本验收。
超长事件按实际输入预算分段并保存中间进度，之后合并成一份事件摘要；中间结果不参与检索。
摘要失败、空输出、输出超限或截断不产生摘录替代品。已有摘要不自动重新生成。

## 画像刷新预算

profile_max_input_chars 和 profile_max_output_tokens 仅影响显式画像刷新，默认值分别为 48,000 和 4,096。输入预算包含固定协议、附加指导、旧画像和事实。必要输入无法容纳时明确失败。画像读取和上下文组装不调用模型。

## 其他配置参数

| 参数组 | 主要字段与默认行为 | 影响 |
| --- | --- | --- |
| 融合 | w_sem=1.0、w_lex=0.6、w_graph=0.8、w_rec=0.3、w_sal=0.25 | 不同检索信号的配置权重 |
| 候选与输出 | top_k=5、rrf_k=60；candidate_k=24 已弃用，仅供旧评测配置 | 检索返回规模与排序融合 |
| 扩展候选 | ann_candidates=False、ann_pool=200 | 内存引擎可选的有界向量候选路径。BM25 仅重排向量与图候选 |
| 时间与图 | recency_tau_days=45、max_hops=2 | 时近性先验和图遍历 |
| 热层与拒答 | max_hot_facts=10000、abstain_threshold=0.45 | 工作集容量和低证据回退 |
| 整合 | salience_decay_per_day=0.02、access_boost=0.5 | 显著度更新 |
| 冲突 | conflict_sim_threshold=0.80 | 使用语义向量召回同主体的跨槽位候选 |


## 服务环境变量

HTTP、直连 MCP 和普通 CLI 使用[数据库运行连接](#postgres-数据库基础)。
远程 MCP stdio、SDK 和导入器使用 HTTP 地址、账号 key 和显式空间。

| 变量 | 未设置时的行为或默认值 |
| --- | --- |
| MNEMOTRACE_DATABASE_URL | 运行角色连接配置，启动数据库服务时必填 |
| MNEMOTRACE_DISCOVERY_DATABASE_URL | 独立 worker 的任务发现连接 |
| MNEMOTRACE_USER_ID | 可信直连的账号 ID，也可传 `--user-id` |
| MNEMOTRACE_SPACE_ID | MCP stdio 和普通 CLI 的空间 ID，也可传 `--space-id` |
| MNEMOTRACE_API_URL | MCP stdio 使用 HTTP 代理时的服务地址 |
| MNEMOTRACE_API_KEY | HTTP 客户端凭据，也可使用受保护的 key 文件 |
| MNEMOTRACE_EMBEDDER | Python 服务默认 lexical。Docker 和 Quickstart 默认 fastembed |
| FASTEMBED_CACHE_PATH | 默认用户目录下的 .cache/mnemotrace/fastembed，Compose 使用持久缓存卷 |
| MNEMOTRACE_EMBED_OFFLINE | 设为 1 时只读取已经预热的 FastEmbed 缓存 |
| MNEMOTRACE_LLM | 空，自动抽取请求在持久化前返回配置错误 |
| MNEMOTRACE_ANSWERER | 复用主 LLM |
| MNEMOTRACE_VISION_LLM | 复用主 LLM，未配置时图片不生成语义描述 |
| MNEMOTRACE_MAX_REQUEST_BYTES | 2097152，HTTP 请求体字节上限 |

服务与 worker 使用相同的嵌入模型和抽取配置。服务会加载当前目录的 `.env`，
已有进程环境变量优先。真实凭据保存在本机配置或环境中。

## 账号与空间

用管理 CLI 创建用户及 key，再通过 `POST /v1/spaces` 为该账号显式创建空间。
同一账号的有效 key 可访问该账号拥有的全部空间。远程客户端提交 `space_id`，
后端根据 key 查得所有者并验证空间归属。详见 [HTTP 参考](http-api.md)。

## 模型与可选依赖

| 能力 | 安装入口与选择 |
| --- | --- |
| HTTP 服务 | `.[server,postgres]` |
| MCP | HTTP 代理用 `.[mcp]`，可信直连用 `.[mcp,postgres]` |
| 本地语义嵌入 | `.[fastembed]` 提供固定的多语 MiniLM；`.[embeddings]` 提供 SentenceTransformer 模型 |
| LLM / API 嵌入 | `.[llm]`；模型名和凭据规则由 [providers.py](../../mnemotrace/llm/providers.py) 解析 |
| PDF/DOCX 解析 | `.[documents]` |
| Postgres 数据库基础 | `.[postgres]`；包含数据库初始化、身份和业务接口 |
| 完整服务常用组合 | `.[serve]`；具体包含项以 pyproject 为准 |

上表的 `.` 表示从源码安装，已发布分发包使用对应包名及 extra。真实模型可能下载文件、联网或产生费用，应按语料语言与任务评测选择。别名对应的具体模型见 factory 源码。模型名中同维度也不代表向量兼容。

FastEmbed 每条原文保留一份向量，超过模型输入窗口的内容仍可通过全文 BM25 匹配。模型缓存或推理失败会报告嵌入不可用。

## 命令入口

| 命令 | 主要用途 |
| --- | --- |
| mnemotrace-quickstart | 使用已配置 LLM 的示例 |
| mnemotrace-mcp / python -m mnemotrace.mcp | 本地工具服务或远程 HTTP 代理 |
| mnemotrace | 授权空间内摄入、操作查询、重试、检索和事实擦除 |
| mnemotrace-import / python -m mnemotrace.connectors | 解析与导入，先使用 --dry-run |
| mnemotrace-agent-setup | 输出或安装客户端配置与受管接入说明 |
| mnemotrace-agent-bootstrap | 组合安装入口，有实际配置写入副作用 |
| mnemotrace-agent-doctor | 检查所选运行环境与接线 |
| mnemotrace-watch | 发现并导入客户端会话，另有调度器及 hook 安装选项 |
| python -m mnemotrace.admin | 数据库初始化和受限账号/key 管理，使用独立凭据 |

完整参数见各命令的 `--help`。MCP stdio 代理使用 `--api-url`、`--api-key-file` 和 `--space-id`，
本地直连使用 `--user-id` 与 `--space-id`。watcher 使用 `--url`、`--key-file` 和 `--space-id`，
默认受理历史后交给 worker，`--immediate` 等待核心处理。
若原文已保存而核心处理失败或等待超时，watcher 保留已确认的原文游标，报告非零退出状态及可查询、重试的操作 ID。
