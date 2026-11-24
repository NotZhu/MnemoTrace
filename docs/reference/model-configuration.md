# 模型配置参考

本文说明 MnemoTrace 在线服务如何选择补全、嵌入和重排端点，以及配置变更对写入和召回的影响。使用 Compose 时，`api` 和 `worker` 从 `deploy/.env.models` 读取模型配置。字段入口见[模型配置模板](../../deploy/.env.models.example)，部署步骤见[自托管部署与运维](../../deploy/README.md)。

## 角色与端点

| 角色 | 配置前缀 | 协议与用途 |
| --- | --- | --- |
| 补全 | `MNEMOTRACE_LLM_` | OpenAI 兼容 Chat Completions。用于事实提取、事实裁决、画像和会话摘要。 |
| 嵌入 | `MNEMOTRACE_EMBED_` | OpenAI 兼容 Embeddings。为事实正文和召回查询生成向量。 |
| 重排 | `MNEMOTRACE_RERANK_` | 可选的 Cohere v2 兼容 Rerank。HTTP 召回将查询和候选事实正文交给重排服务。 |

`BASE_URL` 填不含凭据、查询参数和片段的 API 路径前缀。例如，服务接受 `/v1/chat/completions` 时，补全地址填到 `/v1`。适配器在相应前缀后追加 `chat/completions`、`embeddings` 或 `rerank`。HTTPS 端点使用各角色的 `API_KEY`，模型密钥保存在配置或凭据存储中。

HTTP 端点限无密钥的本机回环地址。使用容器部署时，回环地址指向发起请求的容器。独立模型服务可配置成 `api` 和 `worker` 都能访问的 HTTPS 端点。 [端点校验](../../mnemotrace/adapters/inference/config.py) · [Compose 网络](https://docs.docker.com/compose/how-tos/networking/)

写入要求补全与嵌入模型都已配置。HTTP 召回需要嵌入模型。API 检测到任意嵌入或重排前缀的变量后，就会校验对应角色的配置，包括仅设置超时时间的情况。启用时填齐必填字段，停用可选重排时移除整组重排变量。 [API 装配逻辑](../../mnemotrace/server/app.py) · [写入进程](../../mnemotrace/worker.py)

## 选择补全的 JSON 输出模式

按上游对结构化输出的支持填写 `MNEMOTRACE_LLM_JSON_MODE`。

| 模式 | 如何请求 | 何时使用 |
| --- | --- | --- |
| `json_schema` | 在 `response_format` 中发送 Schema，并设置 `strict: true`。 | 上游支持严格的 JSON Schema 输出，并接受请求中的 Schema。 |
| `json_object` | 请求 JSON 对象，并在系统提示中给出 Schema。 | 上游支持 JSON 对象模式。 |
| `prompt` | 在系统提示中给出 Schema。 | 上游依靠提示词返回 JSON。 |

三种模式均在 MnemoTrace 内验证 JSON 与 Schema，调用方提供领域解析器时还会验证应用约束。适配器使用配置的模式发请求，符合条件的输出错误可在同一请求预算内纠错重试。

生成长度字段通过 `MNEMOTRACE_LLM_TOKEN_LIMIT_FIELD` 选择，按上游契约使用 `max_completion_tokens` 或 `max_tokens`。 [补全适配器](../../mnemotrace/adapters/inference/openai_chat.py)

启用补全时必须设置 `MNEMOTRACE_LLM_CONTEXT_WINDOW_TOKENS`，填写供应商对该模型和端点公布的总上下文 token 容量。事实抽取按组装后的请求字节保守估算输入，预留 `MNEMOTRACE_LLM_MAX_OUTPUT_TOKENS`（默认 2048）、256 个封装 token 和 512 个纠错 token。优先保留本批完整原文，再加入预算内的最近原文和会话摘要。兼容端点的实际 token 数可能与估算不同。

## 嵌入维度与版本切换

`MNEMOTRACE_EMBED_DIM` 必须匹配服务实际返回的向量长度。上游接受 `dimensions` 请求字段且需要显式指定长度时，可设置 `MNEMOTRACE_EMBED_SEND_DIMENSIONS=1`。`MNEMOTRACE_EMBED_REVISION` 用来标识本地向量所属的模型版本。服务在同一端点、同一模型名下升级模型时，使用不同修订标识区分向量。上游模型由端点与发出的 `model` 字段确定。

MnemoTrace 用规范化后的端点前缀、模型名、修订标识、维度、是否发送 `dimensions` 以及输入规则生成嵌入空间标识。轮换密钥或调整批次大小、超时会保留同一标识。写入和召回使用相同标识的向量。 [向量标识实现](../../mnemotrace/adapters/inference/openai_embeddings.py) · [向量查询](../../mnemotrace/adapters/postgres/search.py)

**切换前核对历史事实的语义召回覆盖。** 修改模型、修订标识、API 前缀、维度或是否发送 `dimensions` 会切换嵌入空间标识。原有向量仍绑定旧标识，当前向量查询只读取绑定新标识的向量。API 的业务空间 ID 保持不变，历史事实仍可由符合条件的词法或图检索命中。需要延续历史语义召回时，将向量重建作为独立的迁移步骤验收。

在独立测试实例对比新旧配置时，保留相同的合成输入和查询，分别验收新写入与已有事实。验收已有事实时，可使用词面不同的同义查询，减少关键词命中掩盖向量覆盖缺口的可能。

## 启用可选重排

配置 Cohere v2 兼容端点后，`recall` 省略 `rerank` 时默认使用已配置的重排器。设为 `false` 可跳过重排。显式设为 `true` 而服务端缺少重排配置时，返回 `503 reranker_not_configured`。

验证时用同一查询分别传 `rerank: false` 和 `rerank: true`，选择能产生多条候选的合成数据。`rerank_applied` 为真表示本次确实执行了重排。重排超时时，响应会给出 `degraded_reason: "reranker_timeout"` 并返回降级结果。 [召回选项](http-api.md#召回选项) · [重排适配器](../../mnemotrace/adapters/inference/cohere_reranker.py)

## 应用配置并验收

已部署的 Compose 实例修改模型配置后，从仓库根目录重新创建 API 和写入容器，使两者读取新环境。下方命令用于数据库与迁移已就绪的实例。 [Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)

```sh
docker compose --env-file .env -f deploy/compose.yaml up --no-deps --force-recreate --wait api worker
```

按[部署验收](../../deploy/README.md#部署验收)检查服务状态并运行 HTTP smoke test。正常写入、召回和追溯共同验证所配置的模型链路。切换嵌入模型或重排设置时，再执行上述对应场景的验证。

| 现象 | 优先检查 |
| --- | --- |
| API 或 Worker 启动时提示缺少配置 | 核对已启用角色的全组变量，以及端点形态与密钥要求。 |
| 写入返回 `write_models_unavailable` | 确认 API 同时加载了补全与嵌入配置。 |
| 召回返回 `embedding_not_configured` | 确认 API 已加载嵌入配置。 |
| 模型调用失败或返回结构无效 | 核对实际路径、补全输出模式和向量长度，再结合对应的请求 ID 与服务日志排查。 |
