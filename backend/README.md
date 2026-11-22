# MnemoTrace 新后端

`mnemotrace-next` 是独立的 Python 后端项目，导入名为 `mnemotrace_next`。部署 HTTP `/v1` 服务和写入 worker 见 [Compose 部署](../deploy/next/README.md)。

本地与 CI 都用 `.python-version` 选择 Python。uv 会为后端创建虚拟环境，并按 `uv.lock` 安装项目及开发依赖。默认 `dev` 组包含测试所需的 `inference` 和 `server` extras。先安装 uv，再从仓库根目录运行下面的命令。 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```sh
cd backend
uv run --locked python scripts/check.py
```

同一条命令在 CI 中依次执行 Ruff lint、Ruff 格式检查、mypy 和 pytest。可以用 `uv run --locked ruff format .` 应用格式化。

运行 `uv run --locked python -m mnemotrace_next` 进行冒烟检查，预期输出 `ok`。

HTTP 服务和写入 worker 的启动方式，以及凭据、任务和召回契约，见 [HTTP API](docs/http-api.md)。

## Python 写入

在服务端用已授权的 `space_id`、绑定认证账号的 `storage`、已装配的 `llm` 和 `embedder`、`clock` 与 `ids` 创建 `Memory`。缺少任一推理端口时，构造函数抛出 `ValueError`。日历日期默认按 UTC 解释，可通过 `timezone` 指定时区。

```python
from mnemotrace_next import Memory, MessageInput

memory = Memory(
    space_id=space_id,
    storage=storage,
    llm=llm,
    embedder=embedder,
    clock=clock,
    ids=ids,
)
session_id = await memory.create_session()
result = await memory.remember(
    (MessageInput(role="user", content="以后导出报表默认用 CSV。"),),
    session_id=session_id,
)
```

`remember` 也接受单条用户文本。`RememberResult.message_ids` 按输入顺序返回已提交原文的标识，`fact_changes` 按审计游标升序返回实际发生的变更。合法零事实或全部暂缓时仍保存原文，事实变更为空。

依赖前一次对话的调用应等待前一次 `remember` 完成。`SessionUnavailable` 表示会话不可用，`RememberInvalidated` 表示空间擦除使本次请求失效，`RememberRetryRequired` 表示并发导致本次尚未写入、可由调用方重新发起。`CommitOutcomeUnknown` 表示写事务进入提交阶段后失去确认，原文和事实可能已经提交。调用方重送会按新写入处理，可能产生新的原文记录。

## Python 召回

已装配写入的 `Memory` 可以直接调用 `recall`。只读部署通过 `Memory.for_recall` 装配已授权的存储和查询 Embedder。

```python
from mnemotrace_next import Memory

reader = Memory.for_recall(
    space_id=space_id,
    storage=storage,
    embedder=embedder,
    clock=clock,
)
result = await reader.recall("报表导出格式", top_k=5)
```

`recall` 默认在同一读快照内融合词法和向量候选。`result.evidence` 按相关性排列，包含事实 ID、正文、有效区间和关联原文 ID。实体关系事实还带有 `relation`，包含 `subject_id`、`predicate` 和 `object_entity_id`。默认查当前有效事实。显式传入 `as_of` 时按 UTC epoch 秒查询，事实须有已知起点且有效区间覆盖该时点。

查询近期变化时可调用 `reader.recall("发布规则有哪些变化", prefer_recent=True)`，在相关性接近的候选中优先返回近期生效的事实。`prefer_recent` 默认为 `False`。

空间启用 `memory_decay` 后，当前召回会提高显著度较高的相关候选的排名。新建空间默认为关闭，可通过空间 PATCH 修改。新近度和显著度共用加成上限，传入 `as_of` 时按相关性排列历史候选。Python 召回保持只读。 [空间设置及计分](docs/http-api.md#召回)

使用 `graph` 参数开启图召回。

```python
from mnemotrace_next import GraphRecallOptions

result = await reader.recall("发布负责人属于哪个团队", graph=GraphRecallOptions())
```

省略 `origin_entity_ids` 时，从词法和向量命中的关系事实中交替选取尚未使用的主体，直到两路候选用完。显式提供空序列或同空间内无可用起点时，只返回普通召回结果。`predicates` 按规范谓词约束图路的每一跳，可直接使用证据里的 `relation.predicate`。图路在同一读快照内逐跳筛选并扩展有向关系，受 [`GraphRecallOptions`](src/mnemotrace_next/application/graph_recall.py) 的跳数上限和请求的全局边预算约束。三路候选按事实 ID 融合后返回最多 `top_k` 条平铺证据。

有关系证据时，可以从它的客体继续查询。

```python
relation = next((item.relation for item in result.evidence if item.relation), None)
if relation is not None:
    related = await reader.recall(
        "这个实体负责哪些服务？",
        graph=GraphRecallOptions(origin_entity_ids=(relation.object_entity_id,)),
    )
```

读写和只读装配都可传入部署方授权的 `reranker`，调用时用 `recall(..., rerank=True)` 启用。召回按 RRF 选出候选池。候选至少有两条时，退出读事务后把查询和已授权的候选 ID、正文交给重排器。新近度和显著度加到重排分数上，再截取最终结果。 [候选池与排序规则](docs/http-api.md#召回)

已有自定义重排器需将 `rerank(query, documents, limit)` 迁移为 `rerank(query, documents)`。为每条候选返回一个 `(FactId, float)`，顺序任意，分数须为 `[0, 1]` 内的有限数，缺失、重复和候选外 ID 会触发 `InferenceError`。 [重排端口](src/mnemotrace_next/ports/reranker.py)

`result.degraded_reason` 默认是 `None`。嵌入超时时按剩余词法和图候选加权并标记 `embedding_timeout`。重排超时时按已读取候选的 RRF 分加权并标记 `reranker_timeout`。在途召回使用原读快照的证据，后续请求使用新的读快照。

受限读取由服务端在装配时设置 `exclude_sensitive=True`。来源标识随可见事实返回，读取原文正文使用原文入口的授权规则。

PostgreSQL 的迁移、角色配置、表结构及真库验证见 [PostgreSQL 存储适配器](docs/postgres.md)。

普通召回的固定样例、真实嵌入对照与复现命令见[普通召回质量验收](docs/postgres.md#普通召回质量验收)。

图路补全、种子边计分和高出度计划见[图召回质量验收](docs/postgres.md#图召回质量验收)。

补全和嵌入的端点配置、向量身份及真实端点验收见 [推理适配器](docs/inference.md)。

发行包随附仓库的[开源许可](LICENSE)和[商业授权条款](COMMERCIAL-LICENSE.md)。
