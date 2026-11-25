# MnemoTrace · 忆痕

[English](README.md) · [快速开始](#快速开始) · [文档](docs/README.md) · [工作原理](#工作原理) · [评测](#评测)

MnemoTrace（忆痕）是面向 LLM 智能体的开源、自托管长期记忆引擎。智能体可以在新会话里沿用项目约定和用户偏好。约定发生变化后，它还能查询当前和历史版本，并找到支撑这些事实的原始消息。

应用通过 `remember` 提交会话消息，由后台进程提取结构化事实。回答前，应用用 `recall` 获取带有效期和原文引用的证据，按需放入模型上下文。服务由 HTTP API、后台写入进程和 PostgreSQL/pgvector 组成。

## 快速开始

需要 Docker Compose 2.30.0 或更高版本，以及 `uv`。首次部署使用空数据库。以下命令从仓库根目录运行。

1. 将[数据库模板](deploy/.env.example)复制为根目录的 `.env`，为 `MNEMOTRACE_DB_PASSWORD` 填写随机十六进制密码。将[模型模板](deploy/.env.models.example)复制为 `deploy/.env.models`，填写补全和嵌入模型的配置。
2. 启动服务并为本地示例账号签发 API 密钥。

   ```sh
   docker compose --env-file .env -f deploy/compose.yaml up --build --wait
   docker compose --env-file .env -f deploy/compose.yaml run --rm admin issue --owner-id local-demo
   ```

3. 将返回的 `token` 设为环境变量 `MNEMOTRACE_SMOKE_TOKEN`，运行验收脚本。

   ```sh
   uv run --locked python scripts/http_smoke.py
   ```

   脚本写入合成消息、验证召回证据可以追溯到原文，并清理本次创建的验收空间。成功时输出「HTTP 闭环通过，验收空间已删除。」

验收通过后，可以按 [HTTP API v1 接入指南](docs/reference/http-api.md)直接调用服务，或在 Node.js 中使用 [TypeScript SDK](clients/typescript/README.md)。自管 PostgreSQL、密钥轮换和故障排查见[自托管部署与运维](deploy/README.md)。

## 工作原理

### 持久写入与原子发布

API 把写入请求保存为 PostgreSQL 任务。对同一会话中的同一批消息，用原幂等键重试会返回原任务。会话内按接收顺序处理写入，不同会话可以并行。事实提取和模型裁决在空间写锁之外执行，提交前再持锁复核候选。原文、事实变更和任务成功状态在同一事务中提交。 [写入实现](mnemotrace/application/remember.py)

### 事实裁决与双时间轴

系统结合主体、谓词、有效期重叠以及词法和向量检索，为新事实选出一组待比较的旧事实。模型判断事实对是重复、可并存、冲突还是不确定，后端再依据有效期和证据决定是否补充来源、截止旧事实或暂缓发布。 [裁决实现](mnemotrace/application/adjudication.py) · [事实更新规则](mnemotrace/application/fact_lifecycle.py)

事实的**有效时间**与系统的**记录时间**分别保存。假设用户在 5 月 1 日说“从今天起，我工作日都坐地铁上班”。6 月 3 日，用户在另一场会话中说“从 6 月 1 日起，我工作日改骑车上班了”。服务在 6 月 3 日受理这次补记，骑车事实从 6 月 1 日生效。写入提交后，可以用 `as_of` 查询不同业务时点的通勤习惯。

| 查询时点 | 召回事实 | 来源 |
| --- | --- | --- |
| 5 月 31 日 | 工作日坐地铁上班 | 5 月 1 日的消息 |
| 6 月 1 日生效时 | 工作日骑车上班 | 6 月 3 日的消息 |

### 混合召回与上下文预算

`recall` 按空间和事实有效期筛选。倒数排名融合（RRF）合并词法、向量和可选的多跳图检索结果，再按需重排。`top_k` 和 `max_evidence_chars` 分别限制证据条数和正文总字符数，预算内返回完整条目。嵌入或重排超时时保留可用的检索结果，并通过 `degraded_reason` 标明原因。 [召回实现](mnemotrace/application/recall.py)

### 证据追溯与会话擦除

召回证据通过 `source_message_ids` 指向原文。变更审计可以查看事实有效期的前后值，并追查触发截止的事实和依据。擦除会话时，系统同时清理依赖该会话消息的证据组，撤下失去全部完整证据的抽取事实。有独立证据支持的事实仍可保留。 [变更审计](mnemotrace/server/v1/audit_schemas.py) · [擦除验证](tests/postgres/test_erasure.py)

数据模型、运行流程和一致性边界见[记忆引擎架构](docs/architecture/memory.md)。

## 评测

评测套件覆盖 LongMemEval-S、LoCoMo 和 PersonaMem v2，对照同次运行的全文输入和原文 BM25 基线。数据版本、运行方式和发布校验见[评测指南](eval/README.md)。

## 开发验证

在仓库根目录运行。

```sh
uv sync --locked
uv run --locked python scripts/check.py
```

运行真库回归时，将 `MNEMOTRACE_TEST_POSTGRES_DSN` 指向独立的 PostgreSQL/pgvector 测试实例。

## 许可证

[AGPL-3.0-only](LICENSE)
