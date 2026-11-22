# MnemoTrace TypeScript SDK

`mnemotrace-memory` 调用 MnemoTrace HTTP v1。使用 Node.js 18 或更新版本，支持 ESM 和 CommonJS。

## 接入

按[部署指南](../../deploy/next/README.md)启动 API 和写入 worker。从[凭据签发命令](../../backend/docs/http-api.md#管理凭据和启动)的输出获取 Bearer token，使用有权访问的[空间](../../backend/docs/http-api.md#空间和会话) ID。默认本机地址为 `http://127.0.0.1:8100`。将 token 放在服务端的环境或凭据存储中。

在仓库根目录安装并构建，再从你的项目安装已构建的本地包。

```sh
npm --prefix clients/typescript ci
npm --prefix clients/typescript run build
npm install /absolute/path/to/MnemoTrace/clients/typescript
```

```ts
import { randomUUID } from 'node:crypto'
import { MnemoTraceClient } from 'mnemotrace-memory'

const client = new MnemoTraceClient({
  baseUrl: 'http://127.0.0.1:8100',
  apiKey: process.env.MNEMOTRACE_NEXT_API_KEY!,
})
const spaceId = process.env.MNEMOTRACE_NEXT_SPACE_ID!
const { session_id } = await client.createSession(spaceId)
const completed = await client.rememberAndWait(
  spaceId,
  session_id,
  '忆痕项目默认用 CSV 导出报表。',
  { idempotencyKey: randomUUID() },
)
if (completed.status === 'succeeded') {
  const { evidence } = await client.recall(spaceId, { query: '忆痕项目的默认导出格式' })
  const messageId = evidence[0]?.source_message_ids[0]
  if (messageId) {
    const original = await client.getMessage(spaceId, messageId)
    console.log(original.content)
  }
}
```

## 写入和恢复

- `remember(spaceId, sessionId, messages, options)` 返回 `{ handle, operation }`。要求 `options.idempotencyKey`，支持字符串或含 `role`、`content`、可选 `event_time` 的消息数组。字符串按一条 `user` 消息提交。传输失败、提交结果不明或队列满时会在 `submitTimeoutMs` 预算内用原键原参重发，最多提交三次，默认总预算为 15000 毫秒。
- `getOperation(handle)` 查询当前状态，`waitOperation(handle, { timeoutMs })` 轮询直到 `succeeded` 或 `failed`。等待默认最多 240000 毫秒，超时抛出 `OperationWaitTimeout`。可以保存异常的 `handle`，稍后重新查询或等待。
- `rememberAndWait` 接受同一组写入参数和等待选项。收到 `submit_outcome_unknown` 时，使用原幂等键和原输入恢复提交，最近一次错误可从 `cause` 查看。收到 `failed` 终态时查看 `operation.error`。HTTP 失败会抛出 `MnemoTraceError`，包含 `status`、`code` 和服务端提供的 `requestId`。
- `recall(spaceId, request)` 采用[后端召回契约](../../backend/docs/http-api.md#召回)。例如传 `rerank: false` 跳过重排，从 `rerank_applied` 查看是否实际重排。使用证据中的 `source_message_ids` 调用 `getMessage(spaceId, messageId)`。接口接收服务端返回的路径标识。

## 查询任务与证据

- `listOperations(spaceId, options)` 查找写入任务，支持 `status` 和 `session_id`。
- `listOperationFactChanges({ spaceId, operationId }, options)` 返回任务状态和可见变更。任务状态为 `succeeded` 时读取关联审计，其余状态返回空列表。
- `listFactChanges(spaceId, factId, options)` 读取事实有效期的变更和截止触发依据。两种变更查询都支持 `include_sensitive`。
- `listSessionMessages(spaceId, sessionId, options)` 按追加顺序读取会话原文。也可将变更中的来源 ID 传给 `getMessage`。

方法各返回一页。用 `limit` 设置页大小，续页在相同查询选项中传入上页返回的 `next_cursor` 作为 `cursor`。 [HTTP 查询契约](../../backend/docs/http-api.md#任务变更和会话原文)
