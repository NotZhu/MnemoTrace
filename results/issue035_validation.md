# ISSUE-035：登录周期与导出请求验收

日期：2026-10-20。状态：已修复；本地 Windows 合成回归、TypeScript 检查及 Vite 生产构建通过。
运行环境：Windows x64，Node.js v24.19.0。真实浏览器下载、远端 CI 与线上部署尚未核验。

## 根因与实现

原导出直接 fetch，没有普通请求的凭据检查；普通请求也只比较 Key，无法区分同一账号的不同登录周期。
A→退出→A 会重新接受旧响应，旧 401 还可能退出新登录。直接同 Key 登录跳过旧查询清理，
异步偏好回写也会在同 Key 重登后继续生效。页面卸载不能停止异步函数或写入全局 toast。

认证 store 现在每次 login／logout 原子更新 key、错误及 epoch，包括无效 Key 登录和重复退出。
currentSession() 一次读取只读快照，isCurrentSession() 同时比较 key 和 epoch；状态只存内存。
普通 JSON 与导出共用 requestSession 检查，成功、网络失败和正文失败均先判断归属，过期以
StaleSessionError extends ApiError（status=409）拒绝，不用正常 resolve 表示取消。
旧 401 不退出新登录，当前 JSON 401 保持退出与 401 错误语义。公共 /health 豁免过期检查，
但旧 health 401 同样不能退出新登录。

每次 epoch 变化同步取消并移除私有查询，保留 health；WeakMap 绑定查询对象与周期，
全局回调检查周期、账号及缓存身份，并静默忽略取消类型。偏好激活和延迟修改检查完整快照，
持久化指纹算法和格式保持不变。App 按 epoch 重建已登录页面树，避免同 Key 新登录沿用旧页面状态。

隐私页在导出完成后、成功或失败提示前检查归属；过期任务不提示、不更新旧周期状态。
下载在响应、Blob 读取及点击前检查，创建 URL 后用 finally 清理元素和 URL，正常路径保持点击后立即释放。

## 先失败再修复

按 API／查询与偏好／页面三个边界分别先运行失败回归，再实施对应修改：

| 原始日志 label | 当时的检查与结果 |
| --- | --- |
| red_export | 导出等待响应和读取正文期间的 5 种登录变化，共 10 项全部失败：旧任务正常 resolve 并继续下载 |
| green_export | 加入周期与共享请求检查后，同一组 10 项通过 |
| red_query_preferences | 原有与新增共 27 项，5 项失败：同 Key 私有缓存未移除、旧查询未取消、取消进入横幅、两种重登保留旧偏好修改 |
| green_query_preferences | 订阅与异步回写改用周期后，27 项通过 |
| red_privacy_behavior | 8 项中 5 项失败：三类旧结果出现提示、API 已完成后换登录仍提示成功、同 Key 页面树没有新 key |
| green_privacy | 页面提示与树生命周期修复后，8 项通过 |

日志保留增量测试阶段的原始输出，不表示所有当前用例都在初次 red 时已存在。
red_privacy 是 React hook 测试环境最初缺少 useDebugValue 的工具性失败；修正测试环境后，
red_privacy_behavior 才是业务行为失败证据。首次 frontend_build 暴露 Query 错误泛型约束过窄，
随后仅将查询身份类型收窄为实际使用字段，frontend_build_fixed_types 构建通过；最终查询入口再次通过。

## 最终验证命令与结果

在仓库 frontend 目录执行：

```powershell
npm run test:query-isolation
npm run test:demo-auth
npm run test:preferences
node --test tests/*.test.cjs
npm run build
```

| 检查 | 结果 | 原始日志 label |
| --- | --- | --- |
| 查询隔离入口（含新生命周期、页面与偏好回归） | 81 passed | query_isolation_final |
| 演示登录与凭据校验 | 12 passed | demo_auth |
| 偏好与延迟回写 | 14 passed | preferences |
| 完整前端测试 | 190 passed，0 failed | frontend_all |
| TypeScript + Vite 生产构建 | exit 0 | frontend_build_fixed_types |
| git diff --check（仓库根目录） | exit 0；仅既有换行转换提示 | diff_check |
| 推送前完整 pytest（Windows 普通执行环境） | 872 passed，14 skipped，4 warnings | prepush_pytest_native |

完整前端测试运行后仅修正上述 TypeScript 类型约束，生产构建与 81 项查询隔离入口均在该修正后通过。
本机规划阶段发现 Corepack 无法下载指定 pnpm；本轮用 npm 调用同一 package.json scripts，
未修改依赖或锁文件。CI 仍使用现有 pnpm run test:query-isolation，新回归已加入该入口。

上述各阶段的命令、退出码及未截断输出保存在 [issue035_validation.jsonl](issue035_validation.jsonl)。
本地路径以 <REPO>／<HOME> 脱敏，测试账号与正文均为 synthetic 合成数据，不包含真实凭据。

## 覆盖与责任边界

- 等待响应、读取 Blob 时退出、A→B、A→退出→A、A→B→A、直接同 Key 登录，均丢弃旧导出。
- 同 Key 新登录下的旧 JSON／导出成功、401、500、网络失败、正文失败均过期；旧 401 不改变当前登录。
- /health 的响应和正文读取可跨登录、退出、重登完成；当前 JSON 401、其他 HTTP 详情、204 和网络失败保持既有行为。
- API 完成与 Privacy await 恢复之间安排登录变化：已发生的下载保留，但成功提示和旧状态更新被抑制。
- 正常完整／精简导出发送原凭据、正确下载并释放 URL；元素创建、插入、点击失败及创建 URL 后失效均清理资源；Blob 失败不创建 URL。
- 同 Key 新登录取消旧查询、移除旧缓存和错误，迟到响应不恢复缓存；新周期可正常请求。
- 指纹尚未完成时，旧周期的偏好修改被丢弃，新周期修改仍生效；既有持久化偏好恢复由原回归继续覆盖。

测试使用真实 API、认证 store、查询入口／QueryClient、Privacy 和 App 的渲染结果；网络、下载 DOM、
React hook 执行环境、指纹延迟和 toast 边界使用桩。没有真实请求或文件下载，不据此宣称跨浏览器下载兼容性。
未引入 AbortController；正确性依靠周期检查，网络仍可能继续传输。
请求已抵达服务端时，操作仍可在原账号空间完成；修复不撤销服务端写入或已经触发的下载，
也不意味着服务端曾允许其他账号绕过鉴权。

本轮没有修改后端、记忆算法或 benchmark 声明，没有运行模型评测。实现阶段未重跑 Python／HTTP；
推送前按 CONTRIBUTING.md 补跑完整 pytest，包含现有 HTTP 回归，结果见下节。
架构优化图、完整架构报告及问题台账同步更新；ISSUE-035 移至已修复区，保留历史复现。
另以只读脚本检查台账 P2／已修复数量为 24／5、035 锚点唯一且位于已修复区、上述文档本地链接可解析，均通过。

## 推送前补充验收

CONTRIBUTING.md 要求已安装测试依赖时在推送前运行 pytest。首次沙箱运行出现临时目录服务锁
PermissionError；用 -x 探针确认首个失败（139 passed、2 skipped 后停止），随后停止受该环境限制影响的完整运行。
未修改后端代码或测试，在普通 Windows 执行环境重新运行：

```powershell
$env:PYTHONUTF8 = '1'
python -B -m pytest -o addopts= -q --basetemp=.pytest_cache/session-isolation-prepush-native
```

结果：872 passed，14 skipped，4 warnings，退出码 0。警告为当前环境未识别的两个 asyncio 配置项及
既有 websockets 弃用提示；未安装或升级依赖以消除警告。完整输出及沙箱探针失败输出分别为
原始日志中的 prepush_pytest_native／prepush_pytest_sandbox_probe，不将环境失败记为通过。
