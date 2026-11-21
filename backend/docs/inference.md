# 推理适配器

部署方通过环境变量选择 LLM 和嵌入端点。服务进程用 `open_inference` 装配一次，在退出时关闭 HTTP 客户端。两项能力可以独立启用，同一 API 前缀和凭据的请求共用连接池和并发上限。启用自动抽取的入口须先装配补全端口，再写入原文。

```python
import os

from mnemotrace_next.adapters.inference import open_inference


async def run() -> None:
    async with open_inference(os.environ, llm=True, embed=True) as inference:
        # 在此上下文内把 inference.llm 和 inference.embedder 交给应用层。
        ...
```

## 环境变量

以下名称均加 `MNEMOTRACE_NEXT_` 前缀。启用某个角色时，设置该角色的必填项。非本机端点使用 HTTPS 和对应密钥，明文 HTTP 仅用于无密钥的 `localhost`、`127.0.0.1` 或 `::1`。API 前缀包括版本路径，例如 `https://api.openai.com/v1`。客户端保留该路径并追加 `chat/completions` 或 `embeddings`。

| 变量后缀 | 含义 | 默认值 |
| --- | --- | --- |
| `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY` | 补全 API 前缀、模型和密钥 | 显式配置 |
| `LLM_JSON_MODE` | `json_schema`、`json_object` 或 `prompt` | 显式配置 |
| `LLM_TEMPERATURE` | 可选采样温度，范围为 0 到 2。按部署模型支持的值配置 | 省略参数 |
| `LLM_MAX_OUTPUT_TOKENS` | 输出 token 上限 | 2048 |
| `LLM_TOKEN_LIMIT_FIELD` | `max_completion_tokens` 或 `max_tokens` | `max_completion_tokens` |
| `LLM_DISABLE_THINKING` | `deepseek` 发送 `thinking.type=disabled`，`dashscope` 发送 `enable_thinking=false` | 省略思考参数 |
| `LLM_TIMEOUT_SECONDS` | 单次补全的总时限（秒） | 45 |
| `EMBED_BASE_URL`、`EMBED_MODEL`、`EMBED_API_KEY` | 嵌入 API 前缀、模型和密钥 | 显式配置 |
| `EMBED_REVISION` | 权重或部署修订 | 显式配置 |
| `EMBED_DIM` | 预期向量维度 | 显式配置 |
| `EMBED_BATCH_SIZE` | 单个嵌入请求最多包含的文本数 | 10 |
| `EMBED_SEND_DIMENSIONS` | 设为 `1` 时发送 `dimensions=EMBED_DIM`，仅用于支持该参数的模型 | 0 |
| `EMBED_TIMEOUT_SECONDS` | 整批嵌入的总时限（秒） | 30 |
| `INFERENCE_CONNECT_TIMEOUT_SECONDS` | 建连、写入和取连接的超时（秒） | 5 |
| `INFERENCE_MAX_IN_FLIGHT` | 单进程同一端点和凭据的并发请求数 | 4 |
| `INFERENCE_MAX_RESPONSE_BYTES` | 单次补全或嵌入子批的响应体上限，传输与解压后的正文各按字节计 | 8388608（8 MiB） |

## 响应与向量身份

`json_schema` 启用上游严格约束。`json_object` 和 `prompt` 通过系统消息给模型提供 schema，后者适用于需要省略 `response_format` 的端点。三种模式均在本地严格解析并校验 JSON。降级模式允许一次带校验反馈的重问，与网络重试共用一次额外请求。拒答、截断和无效结构分别由端口异常报告。

请求总时限包含排队和退避。临时建连故障、限流及部分上游故障可补试一次，读超时直接结束。嵌入按配置顺序发送子批，每个子批有独立的补试次数，共用一次嵌入的总时限。子批均通过索引、维度和分量校验后才返回整批向量。

HTTP 响应支持原样传输和 gzip，成功和失败响应共用同一字节上限。正文超限时立即关闭响应，返回类别为 `response_limit` 的 `InferenceRequestError`，本次请求不补试。HTTP 分段读取仍使用模型的普通 JSON 响应。

从端口调用时用 `InferenceError` 捕获请求失败或响应协议异常，用 `TimeoutError` 捕获超时。HTTP 状态码和请求标识可用于适配器诊断。对比抽取评测时记录模型版本、提示和采样温度。低温可以减少采样波动，离线回归使用固定模型响应。

用 `embedder.embedding_space` 同时标记索引写入和查询。标识由 API 前缀、模型、修订、预期和实际请求的维度、输入规则版本生成。轮换密钥可以保留原标识。更换端点、权重、模型或维度时，先用新配置补齐已有事实的向量，再切换读写。托管浮动别名对应的权重或路由变化时，部署方需要更新 `EMBED_REVISION`。即使维度相同，不同模型的向量也应分别存放。

## 写入抽取

`MessageInput.role` 接收 `"user"` 或 `"assistant"`。`MessageInput.event_time` 是以 UTC epoch 秒表示的发言时刻，省略时使用本次固定的接收时刻。入口拒绝超出配置时区所支持日历范围的新时间。写入用例在同一授权读快照里固定本会话最近十条已提交消息和擦除标记，本批消息保留输入顺序和原文。

`extract_facts` 接收前文和本批消息，在读事务结束后调用一次结构化补全。只有助手发言的批次直接返回空事实。只抽取用户明确陈述、纠正或采纳的信息。每条候选只表达一项可独立裁决的断言，并引用一组充分的原文证据，至少包含一条本批用户消息。采纳助手提议时，提议和确认组成同一组。各自充分的证据组分别产出候选。合法的空事实数组正常返回。模型响应中的无效引用或时间使整批抽取失败。

相对日期以表达该日期的消息为锚，时区默认 UTC。既有原文的发言时刻缺失或超范围时，仍提供原文，参考时刻视为未知。正文里的日期换算保留原有精度，例如可确定年份的相对月份写到月份。明确排定的状态按指定日期生效。已表达的计划在发言时成立，计划执行日期和软截止日期留在正文。`valid_at_evidence` 和 `invalid_at_evidence` 分别保存有效起点和截止的依据。边界无法确定时保留为空。

绝对时间依据须包含完整的日历日期，周、月或年精度的表达保留在正文。仅给出日期时使用配置时区的本地零点。绝对时间未带偏移或按相对日期计算时，本地时刻须在该时区内唯一。明确的 UTC 偏移用于确定实际时点。

明确陈述的当前状态以发言时刻作为证据确认的有效起点，原文明确给出的生效日期优先。有明确时间范围的历史陈述保存能确定的有效边界。明确终止抽取为从停止时刻生效的新断言，例如不再参加某个跑团。

每条新事实比较最多十条同主体、同谓词的旧事实，以及最多十条跨槽旧事实。两路先排除有效期确定错开的记录，未知边界视为开放。应用最多读取十一条同槽记录来判断是否超限，十条以内全部比较。超限时按正文的词法和向量排名做 RRF 融合，取前十条，排名不足时用已读的同槽记录补足。跨槽允许跨主体和跨谓词，用相同的混合排名取前十条。向量相似度下限为 `0.60`，RRF 常量为 `60`，同分按事实标识排序。裁决只覆盖入选记录，榜外的重复或冲突可能漏判。

批内配对在首次候选读取前生成一次，提交复核时复用。跳过有效期已知错开的事实对，剩余同槽事实直接配对，跨槽按余弦相似度筛选，每对归给输入顺序较早的候选。需要模型判定的配对以左侧新事实为单位发送正文、结构和有效区间，每次最多三十对，每次写入最多四个比较调用并发。每个分片的结果须完整覆盖其中的配对，所有判定按原配对顺序汇总后才提交。任一分片失败时取消仍在运行的分片，等待清理后传播原始异常。

回顾某段时间内的状态只提供该时段的信息。重叠事实判为矛盾且生效先后明确时，先生效的事实截止到后生效点。后生效事实有已知终点时，旧事实也只保留其起点至新事实起点的区间。明确的终止断言通过矛盾裁决触发截止。多条已收录的后继事实与同一条补录事实冲突时，按最早生效的后继设置截止。同批新事实实际设置或提前截止时，按应用顺序记录各次触发。

重复裁决沿用代表事实的有效区间。独立证据组统一按提交时的最终区间验收。有已知截止的区间要求新证据有可确定的截止，且覆盖到该截止。同批独立裁决缩短代表事实的区间后，覆盖最终区间的重复证据可以补入，其已取得的冲突判定也参与截止计算。若独立依据因后续裁决暂缓，须从仍有效的独立依据重新判断哪些重复证据可参与截止。新代表事实触发其他事实截止时，引用实际参与比较并留存的证据组。

## 验收

默认 CI 使用固定 HTTP 响应。配置好所选角色后，从新后端目录手动运行真实端点冒烟。两个参数可分别使用，也可同时提供。脚本使用合成输入，成功时输出所选端点、模型和补全模式及温度，或嵌入维度、修订和 `embedding_space`。可将嵌入冒烟结果与部署配置一起保存，按数据库中的标识查找对应端点和模型。模型服务未提供固定修订时，用部署方维护的标签，例如 `r1`。

```sh
uv run --locked --extra inference python scripts/inference_smoke.py --llm
uv run --locked --extra inference python scripts/inference_smoke.py --embed
```

| 端点 | 冒烟配置 | 真实端点状态 |
| --- | --- | --- |
| [OpenAI](https://developers.openai.com/api/docs/models/gpt-4.1-mini) | `gpt-4.1-mini-2025-04-14` 用 `json_schema`。嵌入用 `text-embedding-3-small`，1536 维，`EMBED_REVISION=r1`。 | 待验证 |
| [DeepSeek](https://api-docs.deepseek.com/api/create-chat-completion/) | `deepseek-flash` 用 `json_object`、`max_tokens` 和 `DISABLE_THINKING=deepseek`。嵌入角色另配端点。 | 待验证 |
| [阿里云百炼](https://www.alibabacloud.com/help/zh/model-studio/qwen-api-via-openai-chat-completions) | 选与密钥对应的地域和 `/compatible-mode/v1` 前缀。`qwen-plus` 用 `json_object`、`max_tokens` 和 `DISABLE_THINKING=dashscope`。嵌入用 `text-embedding-v4`，1024 维，`EMBED_REVISION=r1`，每批至多 10 条。 | 待验证 |
| [本地 Ollama](https://docs.ollama.com/api/openai-compatibility) | `http://127.0.0.1:11434/v1`，`bge-m3` 默认 1024 维，显式记录模型修订。 | 待验证 |

Ollama 兼容嵌入路由可能在服务端截断超长正文，原生接口的 `truncate: false` 对该路由不适用。本地冒烟使用已固定服务和模型版本的短文本。其他兼容端点通过同一脚本按实际配置验证。
