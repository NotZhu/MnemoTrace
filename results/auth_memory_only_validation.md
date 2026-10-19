# 控制台凭证仅保留在页面内存验收

日期：2026-10-18。范围：浏览器登录凭证、错误重试记录的生命周期与旧缓存清理。

## 需求与改动

按所有者要求取消 key 持久化，刷新后重新输入。`frontend/src/store/auth.ts` 移除 Zustand
persist 及其 version、merge、migrate，初始 `apiKey` 为 null。登录保持原有 ASCII 校验和 trim，
退出清除内存凭证；模块加载时尝试删除旧 `localStorage['mnemotrace.auth']`，不读取或恢复旧 key。

检查同时发现，查询错误的 `queryHash` 包含原始 key，原先会经 `store/queryErrors.ts` 写入
sessionStorage。该 store 同步改为纯内存，并在加载时尝试删除旧
`sessionStorage['mnemotrace.query-errors']`，避免凭证通过错误重试缓存间接落盘。
当前页面仍显示错误并支持重试，账号切换和退出仍清理私有查询及错误状态；刷新后不恢复旧重试入口。

主题和按 API 地址与凭证指纹隔离的空间偏好继续持久化，没有清空整份 localStorage。
偏好持久化内容不含明文 key。存储访问被浏览器限制时，旧条目可能无法删除，但代码不读取其内容，
也不再写入新凭证，当前页面仍可登录和退出。

## 回归

先修改认证和查询隔离回归，再运行旧实现：

```powershell
Set-Location frontend
node --test tests/demoAuthMigration.test.cjs tests/apiKeyValidation.test.cjs tests/queryIsolation.test.cjs
```

得到 **7 failed, 11 passed**，失败覆盖旧 key 被恢复、登录/退出仍写入存储，以及刷新恢复旧查询错误。
修复后这组回归通过，并进一步检查所有模拟 localStorage/sessionStorage 值中都没有当前合成 key。

在 `frontend` 目录运行完整测试与生产构建：

```powershell
$testFiles = Get-ChildItem -Path tests -Filter '*.test.cjs' | ForEach-Object { $_.FullName }
node --test $testFiles
npm run build
```

- **137 passed, 0 failed**。覆盖版本 0/1 旧缓存不恢复、清除旧条目、合法普通/演示 key 原样在内存
  登录、重新加载模块后未登录、无效 key 拦截、退出和受限存储环境。
- 真实前端入口、API 客户端与 QueryClient 的合成集成回归覆盖：失败查询仅在内存保留、清除旧
  错误缓存、账号切换清私有缓存、旧请求迟到隔离、当前账号重试和公共健康查询保留。
- 原有主题与空间偏好回归通过，验证它们仍持久化并可恢复。
- `npm run build` 的 TypeScript 检查与 Vite 生产构建通过。

输出见 [完整前端测试日志](auth_memory_only_frontend_tests.txt) 与
[构建日志](auth_memory_only_frontend_build.txt)。日志统一为 UTF-8，个人用户目录前缀如有出现则
替换为 `<USER_HOME>`。固定测试凭证均为合成值，没有读取实际浏览器凭证。

## 边界

加载新版页面才执行旧缓存清理；不远程操作用户已有浏览器标签页。随机生成的空间 key 需自行
保存，刷新或重新打开后需要重新输入同一 key 才能回到原空间。

本次不改变后端 Bearer 鉴权、租户映射、记忆数据、接口或 CSP。取消持久化减少凭证落盘，不能
阻止当前登录页面中的 XSS 利用权限；`unsafe-inline` 及回退控制台脚本仍作为后续独立加固事项。
没有执行真实浏览器视觉验收、完整后端 pytest 或在线模型 benchmark；137 项结果属于 Node
中的前端回归，不作为真实浏览器安全事件或完整安全审计结论。
