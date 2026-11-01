# 配置参考

配置分为 Python 的 Config、服务/MCP 环境变量和命令参数。它们不是一套自动互相覆盖的全局配置；以各入口的构造与解析逻辑为准。

## Python Config

`from mnemotrace import Config`，再传给 `Memory(config=...)`。完整字段和精确默认值在 [config.py](../../mnemotrace/config.py) 维护。

创建 `Config` 时请使用关键字参数，例如 `Config(top_k=5)`；字段顺序可能随配置分组调整。

## 窗口抽取预算

对话联合输出使用 `joint_max_output_tokens`、`joint_core_output_tokens`、`joint_summary_output_tokens`，分别对应 `MNEMOTRACE_JOINT_MAX_OUTPUT_TOKENS`、`MNEMOTRACE_JOINT_CORE_OUTPUT_TOKENS`、`MNEMOTRACE_JOINT_SUMMARY_OUTPUT_TOKENS`。
默认值维护在 [Config](../../mnemotrace/config.py)，只是工程起点。规划同时预留核心结果、逐条长消息摘要及格式开销，输出不足时缩小窗口，不复用独立摘要的输出上限。
可用 `joint_context_tokens` / `MNEMOTRACE_JOINT_CONTEXT_TOKENS` 声明提供方总上下文限制；规划使用输入 UTF-8 字节作为保守 token 上界，同时预留完整联合输出上限。未声明时不声称已验证提供方上下文容量。截断响应整体失败。

后台对话处理默认开启，`MNEMOTRACE_BUFFERED_EXTRACTION=0` 显式关闭。对话仅由最后活动后空闲 60 秒、预算、flush/close 触发，没有最早等待兜底；原文入口的旧抽取配置见下表。

| Config 字段 | 服务环境变量 | 含义 |
| --- | --- | --- |
| extraction_max_input_bytes | MNEMOTRACE_EXTRACTION_MAX_INPUT_BYTES | 系统与用户提示词的 UTF-8 字节总预算，包含来源元数据 |
| extraction_context_bytes | MNEMOTRACE_EXTRACTION_CONTEXT_BYTES | 每窗前文正文最多占用的 UTF-8 字节数；空间不足时优先减少前文 |
| extraction_max_output_tokens | MNEMOTRACE_EXTRACTION_MAX_OUTPUT_TOKENS | 模型抽取输出 token 上限；结束状态为截断时不提交结果 |

默认值以 [Config 定义](../../mnemotrace/config.py) 为准。输入使用可离线计算、对中英文都明确的字节预算，
并非 provider 的精确 token 计数。输入和输出预算必须是正整数，前文预算可以为零。
调小输入预算会拆成更多窗口；调大输出预算可能增加调用成本。更改预算后重试从已完成的字符位置继续。
原文需保持不变；修改原文后不能复用已有处理范围。

## 条件事件摘要

| Config 字段 | 服务环境变量 | 含义 |
| --- | --- | --- |
| summary_min_chars | MNEMOTRACE_SUMMARY_MIN_CHARS | 原文达到此 Python 字符数才请求摘要 |
| summary_max_chars | MNEMOTRACE_SUMMARY_MAX_CHARS | 成功摘要的字符上限，必须小于生成阈值 |
| summary_max_input_bytes | MNEMOTRACE_SUMMARY_MAX_INPUT_BYTES | 系统和用户提示词合计 UTF-8 字节预算 |
| summary_max_output_tokens | MNEMOTRACE_SUMMARY_MAX_OUTPUT_TOKENS | 每次摘要调用的输出 token 上限 |

四项均为正整数，精确默认值在 [Config](../../mnemotrace/config.py) 中维护。这些是工程初值，未完成真实模型质量或成本验收。
超长事件按实际输入预算分段并保存中间进度，之后合并成一份事件摘要；中间结果不参与检索。
摘要失败、空输出、输出超限或截断不产生摘录替代品。已有摘要不自动重新生成。

## 其他配置参数

| 参数组 | 主要字段与默认行为 | 影响 |
| --- | --- | --- |
| 存储 | storage=memory、data_path=None | 可选 LanceDB / Qdrant；向量数据库不代替完整快照的 open/save |
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

### 可选 Qdrant

安装 `python -m pip install "mnemotrace-memory[qdrant]"`（源码安装用 `".[qdrant]"`）。默认内存路径不导入 Qdrant，LanceDB 仍可独立选择。

本地模式适合单进程开发与测试。四个索引共享一个客户端，退出上下文会释放数据库锁；`close()` 不隐式保存：

```python
from mnemotrace import Config, Memory

with Memory.open("./synthetic-snapshot", config=Config(storage="qdrant")) as memory:
    memory.add("I like Python.", user_id="synthetic-user", consolidate=True)
    memory.save()
# 本地向量库在 ./synthetic-snapshot/qdrant，完整状态仍在 JSONL + manifest 中。
```

直接使用 `Memory(config=Config(storage="qdrant", data_path="./vectors"))` 时必须指定非空本地路径。`Memory.open()` 仅在 `data_path=None` 时推导路径，不修改传入的 Config。重复持有同一路径会报错，重新打开前应关闭原实例。

远程模式用于自建或云端 Qdrant；除回环地址用于本机测试外，连接要求 HTTPS。凭据通过环境或本地配置传入，不写入快照、配置 repr 或响应：

```python
import os
from mnemotrace import Config, Memory

config = Config(
    storage="qdrant",
    qdrant_url=os.environ["MNEMOTRACE_QDRANT_URL"],
    qdrant_api_key=os.environ.get("MNEMOTRACE_QDRANT_API_KEY"),
)
with Memory.open("./synthetic-snapshot", config=config) as memory:
    memory.add_fact("user", "likes", "Python", user_id="synthetic-user")
    memory.save()
```

远程 URL 与 `data_path` 互斥。直接构造远程 `Memory` 时还需要显式 `qdrant_namespace`；`open()` 可从规范化快照绝对路径的 SHA-256 摘要推导。更换快照位置会改变推导出的远程命名空间；需要固定身份时显式配置 namespace。不同 Memory 不应使用同一远程 namespace 并各自管理不同快照。

`qdrant_collection_prefix` 默认 `mnemotrace`，用于区分共享集群中的部署；命名空间摘要和索引名进一步隔离四个集合。`qdrant_timeout` 默认 10 秒。自定义 `vector_store_factory` 仍由调用方管理后端；直接使用 `QdrantVectorStore(client, collection, dimension)` 时，注入客户端由调用方关闭。

Qdrant 承担向量和 payload 存储，图、原始日志及其他状态仍由完整快照恢复。打开快照前先校验全部记录，再重建该命名空间索引；首次打开没有 manifest 的目录视为空快照。未调用 `save()` 的改动不会成为恢复依据。维度不匹配时失败，显式 `open(..., allow_mismatch=True)` 后调用 `reembed()` 和 `save()` 才能迁移；迁移失败可从原有效快照以 `allow_mismatch=True` 重新打开并重试。

带 Python 谓词的查询会分页扫描全部记录、先过滤再排序，以保持现有结果语义。首版没有过滤下推或默认 ANN 加速，不能仅凭切换后端推断检索变快。

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
| MNEMOTRACE_STORAGE | `memory`；可选 `durable`、`lancedb`、`qdrant`，由 HTTP 与本地 MCP 共用 |
| MNEMOTRACE_QDRANT_URL | 未设置时使用每用户快照目录下的本地 Qdrant；设置时连接远程服务 |
| MNEMOTRACE_QDRANT_API_KEY | 未设置；仅通过服务配置传入远程客户端 |
| MNEMOTRACE_QDRANT_COLLECTION_PREFIX | 使用 Config 默认前缀；服务始终另行派生每用户命名空间 |
| MNEMOTRACE_QDRANT_TIMEOUT | 使用 Config 的连接超时默认值，单位秒 |
| MNEMOTRACE_DATA_DIR | `~/.mnemotrace/data`；服务按命名空间管理目录 |
| MNEMOTRACE_API_KEYS | 未配置；保护路由拒绝访问，需显式配置鉴权 |
| MNEMOTRACE_SPACES | 未配置；remember/recall 的扩展 principal/ACL 配置 |
| MNEMOTRACE_OPEN | 关闭；显式开启后把 Bearer 文本作为 namespace |
| MNEMOTRACE_ALLOW_ANONYMOUS | 关闭；仅开放模式下控制共享 anonymous |
| MNEMOTRACE_EMBEDDER | hashing，不下载模型 |
| MNEMOTRACE_LLM | 空；请求抽取时报配置错误 |
| MNEMOTRACE_LLM_TOPICS | 默认开启；设为 0 关闭 LLM 主题增强，保留规则判断；HTTP 服务和本地 MCP 生效，代理 MCP 由目标服务控制 |
| MNEMOTRACE_ANSWERER | 未设置时复用主 LLM |
| MNEMOTRACE_VISION_LLM | 未设置时复用主 LLM；没有模型时不生成图片语义描述 |
| MNEMOTRACE_MAX_REQUEST_BYTES | 2097152，HTTP 请求体字节上限，必须为正整数 |
| MNEMOTRACE_MAX_HOT_USERS | 64，服务的热用户缓存容量 |
| MNEMOTRACE_MAX_HOT_FACTS | 使用 Config 的热层上限 |
| MNEMOTRACE_BUFFERED_EXTRACTION | 默认开启后台处理；设为 0 关闭 worker，显式 flush 仍可执行；对话默认 buffered，原文 remember 默认 immediate；接口见 HTTP 参考 |
| MNEMOTRACE_CONFLICT_DETECTION | 设为 1 且配置 LLM 时启用待确认冲突检测 |
| MNEMOTRACE_SESSION_OUTCOMES | 设为 0 时强制禁止会话结论提取，客户端不能覆盖 |
| MNEMOTRACE_REEMBED_ON_MISMATCH | 默认关闭；设为 1 可触发显式模型迁移路径 |

例如在 PowerShell 中为 HTTP / 本地 MCP 选择本地 Qdrant：

```powershell
$env:MNEMOTRACE_STORAGE = "qdrant"
$env:MNEMOTRACE_DATA_DIR = "./synthetic-service-data"
# HTTP 还需按下方说明配置鉴权。
python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8000
# 或在另一条独立命令中启动本地 MCP：python -m mnemotrace.mcp
```

远程部署再设置 `MNEMOTRACE_QDRANT_URL`，需要鉴权时提供 `MNEMOTRACE_QDRANT_API_KEY`。本地模式不能让 HTTP 与 MCP 两个进程同时持有同一用户数据库；多进程使用远程 Qdrant，同时共享相同快照目录和文件锁。仅共享 Qdrant、各自持有快照的跨主机部署不在当前事务保证范围内。

服务擦除会先清理该用户的精确四个集合，再清理本地状态；远程失败返回错误，保留快照以便重试。操作中断会留下索引标记，下一次访问从已提交快照恢复。HTTP 存储错误见 [HTTP 参考](http-api.md)；异步整合失败计入 `consolidation_failed` 指标，原始已提交 episode 可再次整合。

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
| 可选 Qdrant | `.[qdrant]` 或对应 backends extra；本地和远程共用官方客户端 |
| 可选 LanceDB | `.[lancedb]` 或对应 backends extra |
| 完整服务常用组合 | `.[serve]`；具体包含项以 pyproject 为准 |

上表的 `.` 表示从源码安装，已发布分发包使用对应包名及 extra。真实模型可能下载文件、联网或产生费用，应按语料语言与任务评测选择。别名对应的具体模型见 factory 源码；哈希嵌入不是语义模型，模型名中同维度也不代表向量兼容。

## 命令入口

| 命令 | 主要用途 |
| --- | --- |
| mnemotrace-quickstart | 使用已配置 LLM 的示例 |
| mnemotrace-mcp / python -m mnemotrace.mcp | 本地工具服务或远程 HTTP 代理 |
| mnemotrace-import / python -m mnemotrace.connectors | 解析与导入，先使用 --dry-run |
| mnemotrace-agent-setup | 输出或安装客户端配置与受管接入说明 |
| mnemotrace-agent-bootstrap | 组合安装入口，有实际配置写入副作用 |
| mnemotrace-agent-doctor | 检查所选运行环境与接线 |
| mnemotrace-watch | 发现并导入客户端会话，另有调度器及 hook 安装选项 |

完整参数用各命令的 `--help` 查询。MCP 的 `--api-url/--api-key-file` 对应远程后端；本地使用 `--namespace` 与数据目录。watcher 的 `--url`、`--key-file`、`--dry-run`、`--no-close`、`--immediate` 及安装选项有各自解析规则，不与其他 CLI 参数名混用。
