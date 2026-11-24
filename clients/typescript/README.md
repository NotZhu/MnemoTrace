# MnemoTrace TypeScript SDK

`mnemotrace-memory` 是 MnemoTrace HTTP v1 的 TypeScript 和 JavaScript 客户端。它支持异步记忆写入、任务查询、按时间召回和原文追溯。运行环境为 Node.js 18 或以上，提供 ESM、CommonJS 和 TypeScript 类型声明。

## 从源码安装

在 MnemoTrace 仓库根目录构建并打包 SDK。

```sh
cd clients/typescript
npm ci
npm run build
npm pack
```

在接入项目的目录中安装刚生成的 `.tgz` 文件，将下面的路径换成实际文件路径。

```sh
npm install "/path/to/mnemotrace-memory.tgz"
```

## 写入、召回与追溯

先启动已配置补全与嵌入模型的服务，为测试账号签发 API 密钥，并为示例创建专用空间。将返回的 `token` 和 `space_id` 分别设为环境变量 `MNEMOTRACE_API_KEY` 和 `MNEMOTRACE_SPACE_ID`。 [部署快速开始](../../README.zh-CN.md#快速开始) · [创建空间](../../docs/reference/http-api.md#创建空间)

将以下代码保存为 `example.mjs`，在接入项目中运行 `node example.mjs`。API 密钥保存在服务端环境或凭据存储中。

```js
import { randomUUID } from 'node:crypto'
import { MnemoTraceClient } from 'mnemotrace-memory'

const { MNEMOTRACE_API_KEY: apiKey, MNEMOTRACE_SPACE_ID: spaceId } = process.env
if (!apiKey || !spaceId) {
  throw new Error('请设置 MNEMOTRACE_API_KEY 和 MNEMOTRACE_SPACE_ID')
}

const client = new MnemoTraceClient({
  baseUrl: process.env.MNEMOTRACE_BASE_URL ?? 'http://127.0.0.1:8100',
  apiKey,
})

const { session_id } = await client.createSession(spaceId)
const operation = await client.rememberAndWait(
  spaceId,
  session_id,
  '我工作日坐地铁上班。',
  { idempotencyKey: randomUUID() },
)
if (operation.status === 'failed') {
  throw new Error(`写入失败：${operation.error?.code ?? operation.operation_id}`)
}

const { evidence, degraded_reason } = await client.recall(spaceId, {
  query: '我工作日怎么上班？',
  top_k: 3,
  max_evidence_chars: 600,
})
console.log({
  evidence: evidence.map(({ text }) => text),
  factChangeCount: operation.fact_change_count,
  degradedReason: degraded_reason,
})

const sourceId = evidence.find((item) => item.source_message_ids.length > 0)?.source_message_ids[0]
if (sourceId) {
  const original = await client.getMessage(spaceId, sourceId)
  console.log(original.content)
}
```

有证据时，示例输出召回正文及首条可追溯的原文。若 `evidence` 为空，可参照 HTTP 示例判断抽取与召回结果。演示结束后，可按数据生命周期指南删除专用空间。 [召回与排查](../../docs/reference/http-api.md#召回并查看原文) · [删除演示空间](../../docs/guides/data-lifecycle.md#选择操作)

## 异步写入与重试

`remember` 接受字符串或由 `role`、`content` 和可选 `event_time` 组成的消息数组，返回 `{ handle, operation }`。字符串作为一条 `user` 消息提交。`getOperation(handle)` 查询当前状态，`waitOperation(handle)` 等待任务进入 `succeeded` 或 `failed`。`rememberAndWait` 将提交与等待合为一步。

| 结果 | 处理方式 |
| --- | --- |
| `succeeded` | 原文和事实变更已提交，可以开始召回。 |
| `failed` | 任务以失败状态返回，从 `operation.error` 查看错误。 |
| `OperationWaitTimeout` | 等待超时。保存异常的 `handle`，用 `waitOperation(handle)` 继续等待同一任务。 |
| `submit_outcome_unknown` | 提交结果不明。使用原空间、会话、消息和 `idempotencyKey` 重试，可通过 `cause` 查看最近一次错误。 |

为每批逻辑写入生成并保留一个幂等键，重试复用同一个键。`submitTimeoutMs` 控制提交及重试的总预算，`timeoutMs` 控制等待任务完成的预算。HTTP 请求失败时，`MnemoTraceError` 提供 `code` 以及可用的 `status`、`requestId`。

## 召回与查询

`recall(spaceId, request)` 使用 SDK 导出的 `RecallRequest` 和 `RecallResponse` 类型。历史时点、过滤、预算和降级字段遵循 HTTP v1 契约。 [召回选项](../../docs/reference/http-api.md#召回选项)

- `listOperations` 查看空间下的写入任务，`listOperationFactChanges` 查看某个任务产生的事实变更。
- `listFacts` 查看空间事实，`listFactChanges` 查看单条事实的有效期变化及其依据。
- `listSessionMessages` 按追加顺序列出会话消息，`getMessage` 使用证据里的 `source_message_ids` 取回原文。

列表方法的 `options` 支持 `limit` 和 `cursor`，结果中的 `items` 和 `next_cursor` 遵循 HTTP 的分页规则。 [分页与审计](../../docs/reference/http-api.md#列表分页和审计)

## 开发验证

在 `clients/typescript` 中运行以下检查。`npm test` 会先构建 SDK。类型由后端 OpenAPI 生成，契约更新时按仓库 CI 的方式导出并校验。 [SDK CI](../../.github/workflows/ci.yml)

```sh
npm run typecheck
npm test
```

## 许可证

[AGPL-3.0-only](../../LICENSE)
