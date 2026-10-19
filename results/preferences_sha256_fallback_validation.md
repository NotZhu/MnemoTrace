# 空间偏好 SHA-256 兼容回退验收

日期：2026-10-18。对应审查项 R-03。

## 改动

原 `preferenceScopeFor` 在 `crypto.subtle` 不可用或 digest 失败时生成 `volatile-N`，这类空间
被持久化白名单过滤，导致筛选等偏好无法跨刷新恢复。

现优先使用原生 SHA-256，失败时使用固定版本 `@noble/hashes@2.4.0` 的 `sha256`。保留完整
输入 `JSON.stringify([server, credential])`、UTF-8 编码和 `space-` 加 64 位小写十六进制的
格式，未改缓存版本、隔离边界或存储键。依赖和完整性摘要由 pnpm 10.30.3 写入锁文件，
安装时禁用脚本。该版本的构建环境要求 Node >=20.19，项目 CI 和 Docker 使用 Node 22。

原生与回退可互读既有偏好；保存的仍是指纹、白名单偏好和版本，不保存明文 key。登录凭证
继续只在页面内存中，刷新后重新输入同一 key。两种计算都失败时保留原临时空间回退，存储
不可用时继续使用内存设置。没有修改服务/API、记忆模型或服务器数据。

依赖说明及 API 来源见 [noble-hashes 官方文档](https://github.com/paulmillr/noble-hashes)。

## 回归

先修改偏好回归，在旧实现上运行：

```powershell
Set-Location frontend
node --test tests/preferences.test.cjs
```

得到 **4 failed, 8 passed**，失败覆盖 crypto 缺失、subtle 缺失、原生 digest 拒绝，以及
回退与原生的 SHA-256 编号一致性。输出见 [修复前回归](preferences_sha256_fallback_red.txt)。

修复后运行：

```powershell
node --test --test-reporter=tap tests/preferences.test.cjs
$testFiles = Get-ChildItem -Path tests -Filter '*.test.cjs' | ForEach-Object { $_.FullName }
node --test --test-reporter=tap $testFiles
npm run build
```

- 偏好回归 **12 passed**，覆盖三类原生失效、刷新重建恢复、账号和服务地址隔离、原生与回退
  双向读取缓存、UTF-8/长输入/转义字符，以及存储失败仍可操作。结果与 Node `createHash`
  独立计算的 SHA-256 比较；测试使用浏览器目标 bundle，不依赖浏览器中的 Node crypto。
- 完整前端 **141 passed, 0 failed**，包括凭证内存态、账号切换、主题和其他现有回归。
- TypeScript 检查与 Vite 生产构建通过。

日志：[偏好测试](preferences_sha256_fallback_preferences.txt)、
[完整前端测试](preferences_sha256_fallback_frontend_tests.txt)、
[构建](preferences_sha256_fallback_build.txt)。日志统一为 UTF-8；如出现个人用户目录，前缀替换为 `<USER_HOME>`。

## 边界

本机 Windows，Node 测试通过 VM 注入缺失/失败的 Web Crypto；未执行真实浏览器 HTTP 部署或
线上 Demo 视觉验收。只验证本次前端改动，没有运行完整后端 pytest 或模型 benchmark。

localStorage 本身被浏览器禁用时仍无法跨刷新保存。HTTP 与 HTTPS、不同端口属于不同来源，
不共享浏览器存储；相同编号不意味着跨来源迁移数据。此次也不提供跨设备同步或后端空间
更换 key 后自动合并偏好。
