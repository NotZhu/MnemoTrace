# ISSUE-016 / 037：错误生命周期与内部导航验收

日期：2026-10-22。状态由[问题台账](../../docs/known-issues.zh-CN.md#issue-016)维护。
本记录是控制台工程证据，不是记忆准确率、benchmark 或远端部署证据。

## 根因、实现与取舍

QueryCache 的失败原本驱动 Layout 移除非 Facts 路由；页面内的 useState 草稿随卸载消失。
部分主查询刷新失败又提前返回错误界面，销毁行编辑子组件；共享 ErrorState 遇到全局错误时返回 null，
会隐藏区域错误，来源失败甚至被表现为“来源不在库中”。

Layout 现统一保留 Outlet，移除 Facts 特例。全局条只负责提示和当前账号查询重试；局部 ErrorState
始终呈现，并支持自己的重新加载。设置、关注点、结论和体检保留已有数据及编辑组件，明确提示更新失败，
dirty 草稿不随读取恢复覆盖。会话输入不依赖历史请求，状态/工作记忆/报告失败单独显示。
来源分开处理加载、失败、成功空结果和原文；关闭默认自动重试，使用明确的局部或全局重试。
错误未知的接入记录数不显示为零。代价是局部与全局可以同时提示错误。

设置的普通 a href 改为现有 Router Link，basename 生成 /ui/，不再拼接 origin 和路径。
凭据、草稿均不新增持久化；App 的登录 epoch key、私有查询清理及当前 401 退出策略保留。
离开路由和主动刷新不提供草稿恢复，不把本次改动包装成跨路由草稿功能。

## 命令与自动回归

Windows x64，Node.js v24.19.0，Vite v5.4.21，Python 3.12，Ego Lite Chromium 与 Codex 内置浏览器。
依赖已存在，没有安装/更新依赖或锁文件。命令通过 [run-command.cjs](run-command.cjs) 保存未截断、路径脱敏输出至
[validation.jsonl](validation.jsonl)。仓库根目录执行：

```powershell
node results/issue016-037/run-command.cjs query_isolation frontend node --test tests/queryIsolation.test.cjs tests/searchQueries.test.cjs tests/memoryScope.test.cjs tests/apiKeyValidation.test.cjs tests/sessionLifecycle.test.cjs tests/privacySession.test.cjs tests/preferences.test.cjs tests/consoleErrors.test.cjs tests/factsFilters.test.cjs
node results/issue016-037/run-command.cjs frontend_all_native frontend node --test tests/*.test.cjs
node results/issue016-037/run-command.cjs frontend_typecheck frontend node node_modules/typescript/bin/tsc -b
node results/issue016-037/run-command.cjs production_build frontend node node_modules/vite/bin/vite.js build
```

| 日志 label | 结果与边界 |
| --- | --- |
| red_layout | 8 项中 1 失败：全局错误仍卸载 /profile |
| red_pages / red_pages_corrected_fixture | 设置、会话、结论、关注点、来源、导航及局部错误捕捉旧行为；Health 的初始失败是测试使用了错误的按钮文案，不能计作该行为的 red 证明 |
| green_pages_first | 8 通过、1 个 Health 按钮定位失败；修正测试文案后 green_pages 的 9 项通过 |
| frontend_build | TypeScript 首次发现 resetPrompt 闭包中 data 可能为空；补充守卫后 frontend_typecheck 退出 0 |
| page_cases | 增补场景后 13 通过、1 个空结果文案断言不匹配；按实际文案修正，未改产品空结果行为 |
| query_isolation | 116 项通过，包含 14 项新页面行为及已有账号/登录周期/401/旧响应范围回归 |
| frontend_all | 沙箱 223 通过、1 失败；既有 Bash 子进程以 0xC0000142 启动失败 |
| frontend_all_native | 普通本机环境同一套 224 项全部通过，无跳过 |
| production_build | Vite 生产构建退出 0；配合 frontend_typecheck 完成 package build 的两个阶段 |

新回归位于 [consoleErrors.test.cjs](../../frontend/tests/consoleErrors.test.cjs)，并纳入既有
`test:query-isolation` CI 入口；原 Facts 特例断言改为全部路由保持挂载。
组件回归使用真实页面函数及状态/效果生命周期执行器，控制查询结果；它不冒充 React DOM renderer。
真实 QueryClient/API/认证 store 和旧响应隔离继续由已有测试覆盖，真实 DOM 节点保留另由以下浏览器验收验证。

初次验收没有提交或推送，没有运行完整 pytest、模型 benchmark、zero-setup 或发布检查；后续完整测试复测见下节。

## 2026-10-22 提交前复测

本轮仅重跑测试并补充证据，没有修改产品代码、安装依赖、提交或推送。
Windows x64、Node.js v24.19.0、项目 `.venv` Python 3.12.6 / pytest 9.1.1。

| 检查 | 命令及工作目录 | 结果 |
| --- | --- | --- |
| 完整前端 | `node --test --test-reporter=dot tests/*.test.cjs`，`frontend/`，普通本机环境 | 224 通过，无失败、跳过 |
| Bash/PowerShell 安装命令 | `node --test tests/codexSetupCommand.test.cjs`，`frontend/`，普通本机环境 | 5 通过，包含真实 Bash 执行 |
| 类型检查 | `node node_modules/typescript/bin/tsc -b`，`frontend/` | 退出 0 |
| 生产构建 | `node node_modules/vite/bin/vite.js build`，`frontend/` | 退出 0 |
| 完整 Python | `.venv/Scripts/python.exe -m pytest -ra`，仓库根目录，普通本机环境 | 1106 通过、13 跳过，无失败；101.33 秒 |
| diff 格式 | `git diff --check`，仓库根目录 | 退出 0；仅有既有 LF/CRLF 提示 |

前端及构建原始工具输出见 [recheck.jsonl](recheck.jsonl)。完整 Python 命令通过既有
`run-command.cjs recheck_pytest_venv_native . .venv/Scripts/python.exe -m pytest -ra`
保存于 [validation.jsonl](validation.jsonl) 的 `recheck_pytest_venv_native`；临时目录用户名另做脱敏。
Python 跳过项包括 Windows 链接权限、POSIX 权限位、macOS 专属 plutil，以及已安装 pypdf 时的缺失依赖分支。

本轮沙箱完整前端仍为 223 通过、1 失败：Bash 启动返回 `0xC0000142`；相同测试在普通本机环境通过。
因此已确认失败与执行环境有关，没有据此修改产品或放宽测试，具体初始化失败的 DLL 未定位。
首次 Python 命令误用系统 Python，沙箱临时目录创建失败；`python -m pytest -x -vv --tb=short`
确认首个错误为 pytest 无法创建临时目录。停止该轮后使用项目已有虚拟环境重跑，以上本机完整结果为最终验收。
失败及中止的环境尝试不计为通过；本次没有重跑浏览器、模型 benchmark、zero-setup 或发布检查。
metadata/implementation.patch 对应当前实现快照，原始运行输出及 recheck.jsonl 保留各阶段的复测证据。

## 补充回归：结论重分组与完整 diff 校验

结论恢复结果在前面插入新分组时，原分组的下标 key 会变化，已有编辑行仍可能重建。
Journal 现按事实 ID 在同一父级协调编辑行，连续会话分组只控制标题、边框和间距；
不按会话 ID 合并不连续区段，也不将草稿持久化。补充回归覆盖失败、重试等待，以及恢复后新增、拆分和合并分组，
断言原编辑组件和输入均保留。补充使用已有页面生命周期测试边界，未重跑此前三组真实服务截图。
另在同一隔离服务的生产 `/ui/journal` 页面，用 ego-browser 专用空间 6 实际验证新增分组和同会话区段拆分：
失败后恢复真实服务的新数据，原 textarea 节点仍是同一个，合成草稿不变。
合并区段由自动回归覆盖，不扩大为浏览器验收声明。
成功步骤现已并入[修复后验收脚本](browser-verify.mjs)的结论重分组场景。
准备阶段通过 `/v1/facts` 添加 `subject=synthetic-regroup-original, predicate=decision, object=Synthetic regroup original`，
登录合成账号后进入结论页；历史验收使用空间 6，整理后的脚本自行创建专用空间并生成本轮合成标识。
首轮在第二次重连等待时中断，恢复轮次分开派发 offline/online，未重放已通过的第一轮。
完成后关闭空间并停止本轮 FastAPI；未操作用户已有服务或页面。

证据生成改为零上下文补丁，保留基准 SHA，并排除 patch、metadata、日志及截图自身，避免暂存后递归嵌入。
恢复命令为 `git apply --unidiff-zero results/issue016-037/implementation.patch`，在 metadata 的基准提交上执行。
检查器逐项验证未跟踪文件，普通 `git diff --check` 不再是新增证据的唯一检查。
原失败的 81 行空白上下文及修复后的结果均保留在 validation.jsonl；历史通过数量不改写。

| 日志 label | 结果与边界 |
| --- | --- |
| red_journal_regroup | 新分组恢复后找不到未保存编辑，1 项失败 |
| green_journal_regroup_final | 页面生命周期与结论搜索回归共 19 项通过 |
| red_evidence_whitespace | 新增补丁文件格式检查失败，退出 1 |
| evidence_untracked_guard | 临时未跟踪文件中的行末空白被拦截，探针文件随后删除 |
| final_frontend_regroup | 完整前端 225 项通过，无失败或跳过 |
| final_query_isolation_regroup | CI 查询隔离入口 117 项通过 |
| final_build_regroup | `npm run build` 的类型检查与生产构建通过 |
| browser_journal_new_group_* / browser_journal_split_group_* | 真实服务错误/恢复期间，同一输入 DOM 节点及草稿保留 |

补丁恢复检查及完整待提交 diff 校验的最终结果见 validation.jsonl 对应补充记录。

## 真实服务与浏览器过程

```powershell
python -u results/issue016-037/serve-validation.py
# 第二个终端；用于截图的 GET 故障代理
node results/issue016-037/fault-proxy.cjs
# 第三个终端；开发导航验收（frontend 目录）
node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5174 --strictPort
```

[serve-validation.py](serve-validation.py) 只绑定 127.0.0.1:8000，数据根为仓库忽略目录
`.mnemotrace/issue016-037-validation`，两个合成账号映射 synthetic-a、synthetic-b。
显式 hashing，LLM/answerer/vision 为空，session_outcomes 和冲突 LLM 关闭，覆盖 dotenv 同名设置；
未使用真实个人记忆、模型或供应商请求。/health 和 /ready 均返回 200，实际鉴权 remember 写入
`I live in Testville.` 与 `I live in OtherTestville.`；事实、来源和会话均由真实服务生成。

先构建旧版本，在 ego-browser 的同一 TaskSpace（本轮 id=5）复现：输入设置草稿后，GET /v1/sessions
返回合成 503，页面只剩错误条；重试真实服务后草稿为空。label=browser_baseline。
实现后重新构建生产资源，通过 FastAPI 的 /ui/ 页面操作。网络 fixture 只包装指定 GET，正常请求透传真实 fetch，
不替换 React、QueryClient、Router、DOM 或表单状态。

| 浏览器日志 label | 实际验证 |
| --- | --- |
| browser_settings_failure / retry_waiting / recovered | 设置草稿值不变，保存的原输入 DOM 节点持续连接 |
| browser_policy_refresh_failure / recovered | 通过 offline→online 事件触发真实 QueryClient 重取陈旧 policy；失败和恢复保留 dirty 草稿和节点 |
| browser_conversation_failure / retry_waiting / recovered | 工作记忆网络失败、重试、真实恢复不移除输入节点，不改变 default 会话和自动判断范围 |
| browser_fact_failure / retry_waiting / recovered | 冲突查询失败、重试、恢复期间事实弹窗的原输入节点及内容保留；这些断言包含 portal 内输入，不依赖列表文本 |
| browser_source_failure / recovered | 503 明确显示来源读取失败，不显示来源缺失；重试从服务恢复真实合成原文 |
| browser_account_switch_old_failure_ignored | A 草稿及请求等待时退出并登录 B；B 草稿为空，A 的迟到 503 不进入 B 错误条 |
| browser_current_account_retry | B 自己的失败显示错误，全局重试恢复当前账号 |
| browser_same_account_new_epoch | A→退出→A 后草稿为空，释放旧成功响应不恢复旧草稿或错误 |
| browser_router_navigation / browser_history | 生产设置内部链接及前进/后退保留 documentId，不重新加载应用 |
| browser_explicit_reload_requires_login | 主动刷新显示登录输入框 |
| browser_development_router | Vite 5174 的 /ui/ 内部导航同样保留 documentId，真实 API 经开发代理访问 |

原先按工具轮次保存的脚本已合并为两个入口，移除失败截图探测和中断恢复文件；历史失败及原始记录仍保留，
日志中的旧脚本名是历史上下文，不代表当前可执行入口。重复的 cleanup.json 已移除，原内容完整保留在
validation.jsonl 的 service_cleanup 记录中。八张已验收截图不变。

- [修复前复现](browser-baseline.mjs)：对旧版生产 UI 验证设置页卸载及草稿丢失；必须使用基准版本的前端构建，不能在修复后的 UI 上当作通过测试执行。
- [修复后验证](browser-verify.mjs)：覆盖草稿失败/重试/恢复、来源、登录隔离、结论重分组、生产导航和刷新。`?dev=1` 额外验证已启动的 Vite 5174 代理。

运行前使用上述隔离服务；脚本只操作其中两个合成账号。通过 ego-browser 执行：

```powershell
# 基准版本前端由隔离服务托管时运行
ego-browser nodejs -e "await import('file:///E:/MnemoTrace/results/issue016-037/browser-baseline.mjs')"
# 当前前端构建后；开发代理需另行在 5174 启动
ego-browser nodejs -e "await import('file:///E:/MnemoTrace/results/issue016-037/browser-verify.mjs?dev=1')"
```

两份脚本自行创建专用 TaskSpace，并在成功后关闭；失败时保留同一空间以便诊断，不另建空间绕过失败。
网络 fixture 位于 [browser-network.mjs](browser-network.mjs)，输入/断言/日志帮助函数位于
[browser-common.mjs](browser-common.mjs)。截图仍通过原 GET 故障代理单独验收，不混同为新脚本的截图产物。

合并后在专用空间 7 实跑生产及开发导航：首轮策略重取等待发生在 10 秒缓存新鲜期内，
记为失败尝试；补充真实时间等待后，从同一空间继续完成其余场景和清理。相关输出使用 consolidated_* 标签，
没有覆盖原始验收记录。修复前脚本本次只做语法检查，沿用此前已保存的修复前复现证据，未在当前修复版上伪造旧行为。
文件整理未修改产品、测试或规格；八张截图保持原摘要，历史 JSONL 原文保留，新记录仅追加。

## 截图：实际查看后的八张交付

ego-browser 的 Page.captureScreenshot 多次超时，备用 fromSurface=false 得到错误画面，已删除，未作为证据。
随后用 Codex 内置浏览器访问 127.0.0.1:8016，由 [fault-proxy.cjs](fault-proxy.cjs) 仅拦截目标 GET；
其他请求转发 8000。通过 `node results/issue016-037/set-faults.cjs hold /v1/sessions` 开始等待，
`503` 释放失败，`hold` 后重试等待，`clear` 放行真实服务。工作记忆与冲突场景分别替换为 /v1/working、/v1/conflicts。
不增加生产故障接口、不修改产品 DOM 或添加假错误提示。

截图浏览器独立重做了三组失败、重试等待、恢复，并断言同一合成草稿值；label 为 iab_*。
八张原始 JPEG 已逐张查看。默认窄视口覆盖移动布局；事实另补桌面断点，便于同时观察背景错误条与编辑弹窗，
随后恢复默认视口。弹窗的正常遮罩会模糊背景，故障/恢复由 DOM 日志与图中错误条存在与否共同证明。

| 场景 | 失败时 | 恢复后 |
| --- | --- | --- |
| 设置草稿 | [截图](screenshots/settings-failure.jpg) | [截图](screenshots/settings-recovered.jpg) |
| 会话输入 | [截图](screenshots/conversation-failure.jpg) | [截图](screenshots/conversation-recovered.jpg) |
| 事实编辑（窄视口） | [截图](screenshots/fact-failure.jpg) | [截图](screenshots/fact-recovered.jpg) |
| 事实编辑（桌面） | [截图](screenshots/fact-failure-desktop.jpg) | [截图](screenshots/fact-recovered-desktop.jpg) |

## 环境失败、可恢复代码及清理

- 沙箱本机回环请求 WinError 10013、浏览器连接拒绝；改用普通本机执行环境启动本轮服务。
- 首次截图和后续目标激活截图超时；fromSurface=false 画面不符已删除，未算通过。CDP 中 page.targetId
  未暴露、一次缺失脚本 identifier、旧 placeholder 定位错误、Input.dispatchMouseEvent 超时均保留在工具失败摘要。
- ego-browser 的 requestAnimationFrame 等待没有产出帧，改用普通事件循环计时器恢复；未把工具超时记为产品失败或成功。
- 5173 已占用，未停止原进程，改用 5174。本轮正常完成后关闭自行启动的 8000、8016、5174 和两个浏览器验收表面。

代码基准、完整实现 patch、文件摘要和额外工具失败记录见 [metadata.json](metadata.json)、
[implementation.patch](implementation.patch)、[tool-failures.jsonl](tool-failures.jsonl)。patch 包含相对基准的所有被修改受管文件
及本轮新增文本源码/规格/脚本；日志和截图作为独立证据保留，不重复嵌入 patch。
文档本地链接、台账数量/锚点和 git diff --check 的最终结果写入 validation.jsonl。

架构与规格引用采用[按功能命名的验收索引](../console-validation-index.md)。原始编号目录保留，
当前改动在 results/ 和问题台账之外不再包含问题编号，避免依赖具体台账条目。
该引用整理只修改文档与证据检查，不改变已验收的运行代码，无需重复前端测试或浏览器操作。
