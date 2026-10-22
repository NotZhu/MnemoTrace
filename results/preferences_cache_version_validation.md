# 空间偏好缓存版本重置验收

日期：2026-10-22。只调整浏览器偏好缓存的恢复策略和两条无调用文案，不修改服务端记忆数据。

## 改动

原 `store/preferences.ts` 在恢复缓存时保留两条旧版本兼容：把早期的 `facts.showOld` 布尔开关
转换成三态 `facts.status`，并让版本 0、1 的缓存继续按当前格式校验后采用。

现缓存版本从 1 升到 2。`migrate` 不再读取旧内容，任何非当前版本统一回落到默认值并由 zustand
写回，磁盘上的旧格式随之被替换；`migrate` 函数本身保留，因为缺少它时 persist 只跳过恢复、
不覆盖旧数据。`facts.status` 只接受 `live` / `superseded` / `all`，其余值（包括 `showOld`）
不再被翻译。当前格式的类型、范围、白名单字段校验和账号隔离均未改变，问答片段数 1 → 2 的
取值钳制也继续生效。

`lib/copy.ts` 删除 `profile.evidenceMentions` 与 `profile.evidenceOnce`，两者已无调用；
画像证据仍显示 `x{source_count}` 计数，`evidenceTitle`、`evidenceUser` 与强化提示保留。

升级后的可见影响：浏览器中已有的空间偏好（图谱参数、事实筛选、问答片段数）一次性回到默认值。
这是本次预期的兼容策略，不是缺陷。指纹算法、存储键 `mnemotrace.preferences` 和空间编号不变。

## 回归

先修改偏好回归，在旧实现上运行：

```powershell
Set-Location frontend
node --test --test-reporter=tap tests/preferences.test.cjs
```

得到 **6 failed, 11 passed**，失败覆盖当前格式校验、旧版本整份重置、`showOld` 不再转换、
旧版本重置后仍可写入，以及 chunks 钳制与三态筛选用例。
输出见[修复前回归](preferences_cache_version_red.txt)。

修复后运行：

```powershell
Set-Location frontend
node --test --test-reporter=tap tests/preferences.test.cjs
$testFiles = Get-ChildItem -Path tests -Filter '*.test.cjs' | ForEach-Object { $_.FullName }
node --test --test-reporter=tap $testFiles
npm run build
Set-Location ..
git diff --check
```

- 偏好回归 **17 passed**，新增旧版本重置、重置后可写入与刷新恢复、重置不破坏账号隔离，
  以及未声明字段不写回缓存。
- 完整前端 **228 passed, 0 failed**。
- TypeScript 检查与 Vite 生产构建通过；`git diff --check` 无输出。

日志：[偏好测试](preferences_cache_version_preferences.txt)、
[完整前端测试](preferences_cache_version_frontend_tests.txt)。构建输出未单独留存，
通过结论见上方命令与结果说明；如需复核，在 `frontend` 目录重新执行 `npm run build`。

## 2026-10-23 提交前复核

复核代码为 `07b6a14a96f9984d99f5e8edffd937c9b5b97c18`；本轮仅清理修复前日志的
8 处行尾空白并补充验收记录，未修改运行时代码。环境为 Windows、Node 24.19.0、
仓库 `.venv` 的 Python 3.12.6 / pytest 9.1.1 / pytest-asyncio 1.4.0。

- 偏好回归再次运行，17 passed；反转状态校验条件时回归失败，恢复原文件后全部通过。
- TypeScript 检查与 Vite 生产构建通过。
- 沙箱内完整前端测试为 227 passed、1 failed：Git Bash 进程返回 `3221225794`
  （有符号值 `-1073741502`）。最小命令 `bash --noprofile --norc -c 'exit 0'`
  同样失败；该测试与调用代码相对审查基线均无变化。
- 在沙箱外执行同一接入命令测试，5 passed；随后完整前端测试为 **228 passed、0 failed、
  0 skipped**，退出码 0，确认失败来自执行环境，无需修改测试或产品代码。
- 清理日志空白后，对审查基线执行 `git diff --check` 通过。

复核命令（仓库根目录启动，前端完整测试在沙箱外运行）：

```powershell
Set-Location frontend
node --test tests/preferences.test.cjs
npm run build
node --test tests/codexSetupCommand.test.cjs
node --test --test-reporter=tap tests/*.test.cjs *> ../results/preferences_cache_version_frontend_review.log
Set-Location ..
git diff --check bc163abd640cb62e7a84702fee1e87f02e35948a
```

后端首次以系统 Python 在沙箱内运行 `python -m pytest -q`，因临时目录无法创建而出现
fixture setup errors，且该解释器未安装 pytest-asyncio；`python -m pytest -x --tb=short`
确认首个错误发生在 tmp_path 初始化，随后停止完整运行。改用 `.venv` 与工作区 basetemp
在沙箱内尝试的运行未完成，也已停止。这两次中断不计为通过。

最终使用仓库 `.venv` 在沙箱外执行（默认项目测试配置保持启用）：

```powershell
.venv/Scripts/python.exe -m pytest -v -o faulthandler_timeout=60 *> results/preferences_cache_version_pytest_final_review.log
```

结果为 **1106 passed、13 skipped、0 failed**，耗时 181.37 秒，退出码 0。
跳过项分布：`test_credentials.py` 2 项、`test_documents.py` 1 项、
`test_forget_links.py` 3 项、`test_session_hook.py` 3 项、`test_ui_static.py` 3 项、
`test_watch_install.py` 1 项；涉及 Windows 的 POSIX 权限/符号链接能力限制、macOS 专用
工具，以及已安装 pypdf 时不运行的缺失依赖分支。跳过范围不能视为已通过验证。

本轮终端原始日志仅保留本地 `.log`，不作为 benchmark 证据。

## 边界

本机 Windows，Node 24。回归在 Node VM 中注入合成 localStorage 与 Web Crypto，未在真实浏览器
打开生产构建验证重置后的界面表现。2026-10-22 的原验收只验证前端；2026-10-23
复核另外执行后端 pytest。两轮均未运行模型 benchmark，不涉及记忆质量结论。

不同来源、不同设备不共享偏好；localStorage 被浏览器禁用时仍只能使用内存设置。
本次不提供旧偏好到新版本的字段迁移，也不做跨设备同步。
