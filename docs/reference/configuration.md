# 配置参考

配置分为 Python 的 Config、服务/MCP 环境变量和命令参数。它们不是一套自动互相覆盖的全局配置；以各入口的构造与解析逻辑为准。

## Python Config

`from mnemotrace import Config`，再传给 `Memory(config=...)`。完整字段和精确默认值在 [config.py](../../mnemotrace/config.py) 维护。

创建 `Config` 时请使用关键字参数，例如 `Config(top_k=5)`；字段顺序可能随配置分组调整。

| 参数组 | 主要字段与默认行为 | 影响 |
| --- | --- | --- |
| 存储 | storage=memory、data_path=None | 内置 LanceDB 需要非空数据路径；不代替显式 open/save |
| 嵌入 | embed_dim=256 | 默认 HashingEmbedder 维度；显式 embedder 有自己的身份与维度 |
| 融合 | w_sem=1.0、w_lex=0.6、w_graph=0.8、w_rec=0.3、w_sal=0.25 | 不同检索信号的配置权重 |
| 类型先验 | w_type_preference=1.25、w_type_identity=1.15 | 在相关候选中调整优先级 |
| 候选与输出 | top_k=5、rrf_k=60；candidate_k=24 当前未使用 | 检索返回规模与排序融合 |
| 扩展候选 | ann_candidates=False、ann_pool=200 | 可选有界向量候选路径，可能改变召回覆盖 |
| 时间与图 | recency_tau_days=45、max_hops=2 | 时近性先验和图遍历 |
| 热层与拒答 | max_hot_facts=10000、abstain_threshold=0.45 | 工作集容量和低证据回退 |
| 整合 | salience_decay_per_day=0.02、access_boost=0.5 | 显著度更新 |
| 冲突 | conflict_sim_threshold=0.80；conflict_llm_escalation=False、conflict_detection=False | 规则与可选模型冲突处理 |

使用内置 LanceDB 向量后端时，直接构造 `Memory` 必须提供非空 `data_path`；缺失或空字符串会在后端初始化前抛出 `ValueError`，不会自动创建临时数据库。该路径是向量数据库位置，不代替保存完整记忆状态所需的 `save()`：

```python
from mnemotrace import Config, Memory

memory = Memory(config=Config(storage="lancedb", data_path="./vectors"))
memory.save("./snapshot")
```

`Memory.open()` 已接收快照根目录，在 `data_path=None` 时会使用其下的 `lancedb` 子目录，不修改传入的 Config。显式提供的非空 `data_path` 优先；空字符串仍报错，不触发路径推导：

```python
memory = Memory.open("./snapshot", config=Config(storage="lancedb"))
# 向量数据库位置为 ./snapshot/lancedb；加载完整记忆快照。
```

上述要求仅针对内置 LanceDB 后端。使用自定义 `vector_store_factory` 时，存储位置由工厂管理。LanceDB 的安装依赖见下方[模型与可选依赖](#模型与可选依赖)。

开关按职责分组：

- 证据与预算：evidence_planner、evidence_budgeting、chain_evidence、temporal_history_queries、chain_current_first、provenance_evidence、provenance_chunk_promotion。
- 抽取与偏好：explicit_preference_extraction、preference_object_filter、preference_object_normalization、preference_reversal_extraction。
- 自动主题：llm_topics=True，在既有属性抽取和会话结论调用中附加主题协议；关闭时忽略模型主题，仍执行离线规则。
- 数值聚合：numeric_aggregation_candidates、aggregation_recall_expansion、aggregation_constraint_filter、aggregation_pool_boost。
- 派生读取：summary_fallback、summary_fallback_k、procedural_extraction、procedural_memory、procedural_memory_k。
- 图与规划：graph_proximity、graph_relation_awareness、graph_path_reinforcement、graph_self_anchor、graph_entity_alias_anchor、graph_negative_constraints、planner_location_chains、planner_project_chains、planner_llm_decomposition。
- 会话收尾：session_outcomes。

多数增强当前默认开启，但具体是否执行还取决于调用路径和模型是否可用。不要从字段默认值推断已经发生外部模型调用。算法消融、数据范围与成本记录见[评测说明](../../eval/README.md)。

```python
from mnemotrace import Config, Memory

memory = Memory(config=Config(llm_topics=False))
fact = memory.add_fact("user", "uses", "storage engine")
assert (fact.topic, fact.topic_source) == ("technology", "rule")
```

有 LLM 时该开关只控制主题协议，不关闭原有事实或结论提取。开启后不增加模型调用次数或 token 上限，但会增加输入和输出 token；有效提示词改变也会改变提示词缓存键。关闭时保留原有请求形状与提示词缓存语义。主题判定与重置规则见[主题与来源](../architecture/memory.md#主题与来源)。

## 服务环境变量

| 变量 | 未设置时的行为或默认值 |
| --- | --- |
| MNEMOTRACE_DATA_DIR | `~/.mnemotrace/data`；服务按命名空间管理目录 |
| MNEMOTRACE_API_KEYS | 未配置；保护路由拒绝访问，需显式配置鉴权 |
| MNEMOTRACE_SPACES | 未配置；remember/recall 的扩展 principal/ACL 配置 |
| MNEMOTRACE_OPEN | 关闭；显式开启后把 Bearer 文本作为 namespace |
| MNEMOTRACE_ALLOW_ANONYMOUS | 关闭；仅开放模式下控制共享 anonymous |
| MNEMOTRACE_EMBEDDER | hashing，不下载模型 |
| MNEMOTRACE_LLM | 空，规则抽取 |
| MNEMOTRACE_LLM_TOPICS | 默认开启；设为 0 关闭 LLM 主题增强，保留规则判断；HTTP 服务和本地 MCP 生效，代理 MCP 由目标服务控制 |
| MNEMOTRACE_ANSWERER | 未设置时复用主 LLM |
| MNEMOTRACE_VISION_LLM | 未设置时复用主 LLM；没有模型时不生成图片语义描述 |
| MNEMOTRACE_MAX_REQUEST_BYTES | 2097152，HTTP 请求体字节上限，必须为正整数 |
| MNEMOTRACE_MAX_HOT_USERS | 64，服务的热用户缓存容量 |
| MNEMOTRACE_MAX_HOT_FACTS | 使用 Config 的热层上限 |
| MNEMOTRACE_ASYNC_CONSOLIDATION | 仅设为 1 时开启后台整合；返回与可见性见 HTTP 参考 |
| MNEMOTRACE_CONFLICT_DETECTION | 设为 1 且配置 LLM 时启用待确认冲突检测 |
| MNEMOTRACE_SESSION_OUTCOMES | 设为 0 时强制禁止会话结论提取，客户端不能覆盖 |
| MNEMOTRACE_REEMBED_ON_MISMATCH | 默认关闭；设为 1 可触发显式模型迁移路径 |

生产 Compose 模板只传入其中一部分变量；需要其他设置时应明确修改自己的部署环境，不能假定本机全部变量会自动传进容器。systemd 读取模板指定的 EnvironmentFile。

服务与部分工具会加载当前工作目录的 `.env`，不覆盖已有进程环境变量。MCP 的 namespace/API URL 等是在相应入口解析的，配置在进程启动前提供最可靠。真实凭据只保存在本机配置或环境中，见[部署说明](../../deploy/README.md)。

## 命名空间与 ACL

普通鉴权格式为 `tenant:key,tenant:key2`。多个 key 映射同一 tenant 可用于轮换。

扩展 space 配置是 JSON：每个 key 对应 home、read 和 write。home 始终属于该 principal 的读写范围。当前 remember/recall 使用扩展 ACL，其他管理路由仍走普通鉴权；需要管理访问时同时配置相应普通映射。只配置 SPACES 也不能代替 /ready 所需的正常服务鉴权条件。

```json
{
  "YOUR_API_KEY": {
    "home": "user123",
    "read": ["user123", "my-app"],
    "write": ["user123"]
  }
}
```

这是结构示例，不是可用于生产的凭据。权限在服务端校验，客户端传 space/spaces 不会自行获得权限。

## 模型与可选依赖

| 能力 | 安装入口与选择 |
| --- | --- |
| HTTP 服务 | `.[server]` |
| MCP、本地或 HTTP 代理 | `.[mcp]` |
| 本地语义嵌入 | `.[embeddings]`；multilingual、bge-small-zh、bge-small、bge-m3、bge-large 或 st:模型名 |
| LLM / API 嵌入 | `.[llm]`；模型名和凭据规则由 [providers.py](../../mnemotrace/llm/providers.py) 解析 |
| PDF/DOCX 解析 | `.[documents]` |
| 可选 LanceDB | `.[lancedb]` 或对应 backends extra |
| 完整服务常用组合 | `.[serve]`；具体包含项以 pyproject 为准 |

上表的 `.` 表示从源码安装，已发布分发包使用对应包名及 extra。真实模型可能下载文件、联网或产生费用，应按语料语言与任务评测选择。别名对应的具体模型见 factory 源码；哈希嵌入不是语义模型，模型名中同维度也不代表向量兼容。

## 命令入口

| 命令 | 主要用途 |
| --- | --- |
| mnemotrace-quickstart | 离线示例 |
| mnemotrace-mcp / python -m mnemotrace.mcp | 本地工具服务或远程 HTTP 代理 |
| mnemotrace-import / python -m mnemotrace.connectors | 解析与导入，先使用 --dry-run |
| mnemotrace-agent-setup | 输出或安装客户端配置与受管接入说明 |
| mnemotrace-agent-bootstrap | 组合安装入口，有实际配置写入副作用 |
| mnemotrace-agent-doctor | 检查所选运行环境与接线 |
| mnemotrace-watch | 发现并导入客户端会话，另有调度器及 hook 安装选项 |

完整参数用各命令的 `--help` 查询。MCP 的 `--api-url/--api-key-file` 对应远程后端；本地使用 `--namespace` 与数据目录。watcher 的 `--url`、`--key-file`、`--dry-run`、`--no-outcomes` 及安装选项有各自解析规则，不与其他 CLI 参数名混用。
