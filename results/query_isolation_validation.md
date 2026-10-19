# 控制台跨账号查询隔离验收（2026-10-18）

## 问题与修复

全局错误条原先重试所有失败查询，而 API 客户端在执行时读取当前登录 Key。
旧账号 A 的查询失败后，切换到 B 并重试，会把 B 的数据缓存到 A 的查询键下。

- 重试入口只执行当前 Key 对应的失败查询。
- `main.tsx` 订阅账号变化，同步取消、移除私有查询并清空错误提示；公共健康查询保留。
- 全局成功/失败回调只处理仍在缓存中、属于当前账号的查询，防止迟到回调污染当前错误条。
- 原文溯源查询由公共键改为 `['source-episodes', Key]`，退出后禁用。
- 普通 API 请求在响应到达和正文读取完成后核对凭证。空间切换时以客户端 `ApiError(409)` 拒绝旧响应，旧 401 不退出新账号。

只调整浏览器查询和请求生命周期。取消查询不保证撤销已发送的 HTTP 请求或服务端写入。
没有修改后端鉴权、记忆算法、持久化数据，也没有运行外部模型或更改 benchmark 数字。

## 回归证据

新增 `frontend/tests/queryIsolation.test.cjs`，使用真实入口代码、Zustand 鉴权 store、API 客户端、
TanStack QueryClient 及错误条重试处理，仅替换浏览器挂载、React hook 执行环境和网络响应。
测试内容、账号及错误均为合成数据，无 API key 或外部服务。

修复前运行 `node --test tests/queryIsolation.test.cjs`，最初 5 个场景中有 4 个失败、1 个通过：
旧账号查询没有移除，重试发起 2 次请求而非 1 次，退出后仍有进行中的查询，旧 401 返回鉴权失败并退出新账号。
修复后原有失败场景通过，再补充正文读取中切换账号、原文溯源隔离和公共健康请求回归，共 8 个新增场景。

仓库根目录可复现命令：

```powershell
npm --prefix frontend run test:query-isolation
npm --prefix frontend run build
```

- 专项脚本覆盖新增 8 项、原有搜索查询 6 项及 Key 校验 4 项，18/18 通过。
  [专项原始输出](query_isolation_regression_tests.txt)。CI web job 在构建前执行此脚本。
- 前端完整测试 `cd frontend; node --test tests/*.test.cjs` 在新增前 7 项回归时执行，127/127 通过，
  包含原有 120 项测试。[完整测试原始输出](query_isolation_frontend_tests.txt)。之后仅增加第 8 项公共健康请求测试，已由专项脚本验证。
- TypeScript/Vite 生产构建成功。

本次没有重跑 Python/HTTP 测试或真实模型 benchmark；验收结论限于上述浏览器代码路径及构建。
