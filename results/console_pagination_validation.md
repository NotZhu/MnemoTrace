# 控制台分页工程验收

当前交付结果以文末“首屏写入刷新修订验收”为准。前面的首次实现与迁移记录保留为历史证据，不能替代本轮重载竞态、固定时刻及 SDK 契约验收。

## 基线、环境与范围

2026-10-22；基线 `bc163abd640cb62e7a84702fee1e87f02e35948a`，分支 `fix/console-pagination`。
最初由 Codex 创建独立受管 worktree；用户随后将其移至 `E:/MnemoTrace-worktrees/console-pagination`。实现与验收均在该独立 worktree 执行，原 main 工作区核验为干净。
本次代码未提交，证据与本次工作区变更共同使用；未推送、合并或部署。
Windows，Python 3.12.6，pytest 8.4.2，Node 24.19.0，pnpm 10.30.3；使用锁定依赖，React Query 5.101.0。
无新增依赖。`pnpm install --offline --frozen-lockfile` 因缓存缺包失败，随后 `pnpm install --frozen-lockfile` 成功，锁文件未改。

只验收控制台浏览 API、分页和缓存生命周期。数据全部合成，服务为 hashing embedder / 无 LLM；不运行模型 benchmark，不更改公开成绩。
规格及实施前门禁见 `specs/008-console-pagination/`，一致性检查 PASS。

## 首次实现的历史回归与命令

命令均在 worktree 根目录执行；pytest 最终运行前将 TEMP/TMP 指向 `data/console-pagination/tmp`。
普通完整终端日志为本地 `.log`（gitignore），原始 JUnit XML 移至本地 `data/console-pagination/pytest.xml`，避免记录机器名和绝对个人路径。
可入库摘要见[检查结果 JSON](console_pagination_checks.json)。

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/test_memory_pagination.py tests/test_server.py -k 'complete_collections or outcome_filter_precedes or revision_change_between or empty_revision_restart or memories_outcome_kind' -q`（实施前） | 8 个失败：缺失筛选/版本契约，HTTP 所选类型 total 错误为 62；修复后同命令全部通过 |
| `node --test tests/factsFilters.test.cjs tests/journalSearch.test.cjs`（实施前，cwd 为 frontend） | 捕获 limit 80 而非固定 40，以及 outcome_kind 缺失；修复后通过 |
| `python -m pytest tests/test_memory_pagination.py tests/test_server.py tests/test_service_transactions.py -q` | 102 通过（添加混合过滤回归前），服务/API/锁/持久化覆盖 |
| `python -m pytest --basetemp=data/console-pagination/final-pytest --junitxml=results/console_pagination_pytest.xml -rs` | 1073 passed，20 skipped，4 warnings，72.44 秒 |
| `pnpm --dir frontend exec node --test` | 236 passed，0 failed，0 skipped |
| `pnpm --dir frontend build` | TypeScript 与 Vite 生产构建通过；最终主包 `index-Di6uQ3J2.js` |
| `python scripts/check_zero_setup.py` | PASS：离线 quickstart、harness、既有证据及标准库编译；其中统计输出仅复核既有日志，不是本次新 benchmark |
| `git diff --check`；新增/修改行及新文件编号扫描 | PASS，编号只允许出现在 results 和问题台账 |

20 个跳过：缺 lancedb 2、缺 mcp 5、Windows 文件链接权限 8、POSIX 权限语义 3、已安装 pypdf 故不适用其缺包路径 1、macOS plutil 1。
4 个警告：本机未安装 pytest-asyncio，已有两个配置项未识别；uvicorn/websockets 的两个弃用警告。
跳过项未视作通过；本次没有声明 Linux、MCP 或 LanceDB 的完整运行保证。

## 服务与查询层覆盖

- 601 条事实与 241 条原文，固定页大小和大于单页上限的请求，返回全部预期 ID、无重复、同时间 ID 顺序确定，重启后可继续读取。
- 后页才出现的类型、交错类型、搜索/分类/敏感/当前/历史组合、真正无匹配；空间总数不被筛选总数替代。
- 两个真实服务实例的新增、修改、删除、清空使旧版本后续页失败；空库固定版本，其他空间写入不误伤。
- 真实 InfiniteQueryObserver/QueryClient 验证固定页大小、位置与版本传递、失败页重试、重复点击、切换范围、迟到响应、占位标识、写后整批重建、异常游标拒绝。
- 显式重载成功前不清空缓存，失败保留旧数据；同 Key 新登录后的旧重载不得恢复已移除私有缓存。版本变化消除同查询先前网络错误的全局重试入口。

## 首次实现的历史生产浏览器验收

在该 worktree 启动 `python -m uvicorn mnemotrace.server.app:app --host 127.0.0.1 --port 8769`，私有合成空间，数据根位于 `data/console-pagination/store`。
初始种子为 601 条属性事实、61 条 decision、1 条较早 lesson、1 条较早 open_question、241 条 Episode；后续显式增加两条合成属性触发版本变化。
使用 ego-browser 同一任务空间操作真实生产页面，初次事实翻页使用原生点击，其余批量遍历触发实际 DOM 按钮并等待真实响应。

| 场景 | 实际结果 |
| --- | --- |
| 事实 | 16 页取完 601 条，601 个唯一标记，最早记录存在；请求 offset 从 40 到 600，limit 始终为 40 |
| 时间线 | 12 页取完 664 条，664 个唯一标记，较早 lesson 存在 |
| 原始对话 | 13 页取完 241 条，241 个唯一标记，最早原文存在 |
| 结论 | 全部类型取完 63 条；直接选择经验只返回较早的 1 条，待解决卡片仍存在 |
| 外部写入后继续分页 | 409 局部提示，加载按钮禁用，原 textarea DOM 节点及未保存正文保持 |
| 显式重新加载 | 成功后仅提交新第一页 60 条，原 textarea 节点与草稿保持 |
| 下一页失败及重试 | 仅对目标 GET 注入一次 503，其余真实请求透传；保留编辑器，重试相同 offset=60 和相同版本，取到第 61 条后加载按钮消失 |
| 真正无匹配搜索 | 正确显示空结果与重置筛选，不冒充整个空间为空 |
| 最终生产包复核 | 刷新并重新登录后验证 `index-Di6uQ3J2.js`：版本变化、重载、DOM 身份和草稿通过，全局无旧重试横幅 |

原始断言见[浏览器 JSON](console_pagination_browser.json)，最终页面文本见[空结果 DOM](console_pagination_empty_dom.txt)与[版本变化 DOM](console_pagination_revision_dom.txt)。
浏览器脚本保留在本地 `data/console-pagination/round01.mjs` 至 `round11.mjs`。中途截图接口 Page.captureScreenshot 超时，未生成截图；保留 DOM 与请求证据，不声称截图验收。
验收脚本曾因时间线首屏全为较新 decision、原文按钮文案差异和文本定位歧义中断，均先检查实际页面后修正脚本并完成；另有初期命令相对路径错误，未产生跨 worktree 修改。

## 迁移后的核验

用户将目录迁至 `E:/MnemoTrace-worktrees/console-pagination` 后，核对 Git 回指、分支与冻结基线均正常；原 main 工作区仍干净。
pnpm 的依赖 Junction 指向新目录，重新构建的主包仍为 `index-Di6uQ3J2.js`。本地历史验收脚本的绝对路径已更新到新位置。
迁移遗留的 157 个生成字节码文件仍嵌入旧路径，已在确认文件属于当前 worktree 后重建；服务源码与代码位置均指向新目录。

| 新路径下的命令／核验 | 结果 |
| --- | --- |
| `python -m pytest --basetemp=data/console-pagination/relocation-pytest --junitxml=data/console-pagination/relocation-pytest.xml -rs` | 1073 passed，20 skipped，4 warnings，143.70 秒；跳过范围与上文相同 |
| 重建字节码后运行分页与 HTTP 目标切片（完整命令见检查结果 JSON） | 9 passed |
| `pnpm --dir frontend exec node --test` | 236 passed，0 failed |
| `pnpm --dir frontend build` | PASS，主包内容未变 |
| `python scripts/check_zero_setup.py` | PASS |
| 编号边界、476 个本地文档链接、已修复台账唯一锚点、包含新文件的空白扫描 | PASS |

临时浏览器空间已不存在，8769 端口无监听，无需再次停止服务。
Codex 应用层附件仍指向旧目录；尝试重新附加返回“不是受管 worktree”。这是应用附件识别限制，Git 与新目录下的测试和构建正常；本次没有修改应用内部数据。
迁移后的机器可读摘要同样保存在[检查结果 JSON](console_pagination_checks.json)。

## 边界

版本变化检测复用已加载对象的提交标识，不保存历史快照、不自动追赶持续写入；用户需重新浏览。
事实/原文显式单页上限 500/200，未传 limit 的单次全文接口保留。原始对话来源弹窗的独立读取范围不在本次扩展。
时间线人物/日期筛选、时间含义和替换链仍由问题台账跟踪。所有能力声明限上述本地工程验收。


## 分页一致性与接口契约修订验收

2026-10-23；仍在相同基线、分支和 E 盘 worktree，未提交、推送、合并或部署。原 8769 手工服务 PID 56340 保持运行；本轮使用 8770、独立数据根 `data/console-pagination/consistency/store`。
规格先修订并通过一致性门禁，再修改实现。测试接缝为真实 QueryClient/InfiniteQueryObserver、MemoryService、HTTP、SDK 和真实生产 DOM。

### 根因、失败证据与修复

- 旧重载通过独立 mutation 发请求并直接 setQueryData，能在新版本成功后用迟到响应把缓存从 v3 覆盖回 v2。新生命周期回归在旧代码下 15 项中失败 11 项、通过 4 项。修复为 refetch(cancelRefetch=true)，由同一 Infinite Query 从第一页重建已加载链，整批提交；没有 pages=1、新鲜度门槛或第二提交者。
- 旧版本令牌只固定存储提交，没有固定墙钟判定。服务/HTTP 的 15 个时间和参数回归全部失败；现在 evaluated_at 贯穿 live/历史过滤、行状态及三个事实统计，后续请求与 expected_revision 成对，非法时间返回 422。
- 旧 SDK 类型检查出现 4 个契约错误，运行测试漏传 outcome_kind。现在参数、响应和四类枚举同步，409 结构保留在 MnemoTraceError.detail，文档包含遍历例子和版本边界。
- 仅在内存副本移除时间一致性校验，新回归明确失败 1 项；将重载取消关闭，连续重载的 2 项测试在 1500ms 边界内检测到挂起。实际源码从未被这些变异覆盖，正常生命周期 15 项全部通过。

失败日志位于本地忽略目录：`frontend-lifecycle-red.log`、`time-red.log`、`sdk-red-types.log`、`sdk-red-tests.log`；它们均在 `data/console-pagination/` 下。内存变异探针为 `data/console-pagination/consistency/mutation-probe.cjs`，命令为 `node <该路径> time` 和 `node <该路径> cancellation`；故意变异应非零退出。

### 命令与最终结果

以下命令 cwd 均为 worktree 根目录；完整 Python 检查的 TEMP/TMP 指向 `data/console-pagination/tmp`。机器可读摘要见[本轮检查 JSON](console_pagination_consistency_checks.json)。

| 命令 | 结果 |
| --- | --- |
| `python -m pytest tests/test_memory_pagination.py tests/test_server.py tests/test_service_transactions.py -q --basetemp=data/console-pagination/time-green` | 目标服务、HTTP、事务回归通过 |
| `pnpm --dir frontend exec node --test tests/memoryPages.test.cjs tests/sessionLifecycle.test.cjs` | 52 passed，包含真实 fetch 的 AbortSignal 透传 |
| `python -m pytest --basetemp=data/console-pagination/consistency-pytest --junitxml=data/console-pagination/consistency-pytest.xml -rs` | 1088 passed，20 skipped，4 warnings，117.51 秒 |
| `pnpm --dir frontend exec node --test` | 243 passed，0 failed，0 skipped；测试适配清理后全量再次通过（6.33 秒），目标 15 项也再次通过 |
| `pnpm --dir frontend build` | TypeScript / Vite PASS；主包 `index-D-G4-0LY.js` |
| `npm --prefix clients/typescript run typecheck` | PASS，包括公共类型正反例 |
| `npm --prefix clients/typescript run build` | PASS，ESM/CJS/声明文件 |
| `npm --prefix clients/typescript test` | 11 passed，0 failed，包含参数与结构化 409 |
| `python scripts/check_zero_setup.py` | PASS；输出中的成绩只是复核既有日志，并非新 benchmark |
| `git diff --check`、新文件/新增行命名扫描、本地文档链接检查 | PASS，命名仅允许 results 与问题台账包含编号 |

20 个跳过与首次实现相同：lancedb 2、mcp 5、Windows 链接权限 8、POSIX 语义 3、已安装 pypdf 导致缺包分支不适用 1、macOS plutil 1。4 个警告是既有 pytest-asyncio 配置和 uvicorn/websockets 弃用警告。未将跳过视作通过。
SDK 沿用独立 package-lock：先 `npm ci --ignore-scripts --offline` 因 ufo 缓存缺失失败，再 `npm ci --ignore-scripts` 成功；不执行安装脚本，不增改依赖或锁文件。普通终端日志和含机器名的 JUnit XML 留在本地忽略目录，不入库。

### 最终生产页面证据

使用 ego-browser 任务空间 11 操作真实生产页面；结束后已关闭任务空间。最终证据见[浏览器断言 JSON](console_pagination_consistency_browser.json)、[版本冲突 DOM](console_pagination_consistency_revision_dom.txt)与[展开状态 DOM](console_pagination_consistency_expanded_dom.txt)。本轮未做截图，不声明截图验收。

| 场景 | 断言 |
| --- | --- |
| 事实 | 16 页、601 条 DOM 记录及 601 个唯一 ID，固定 limit=40，同一版本/评估时刻 |
| 时间线 | 12 页、664 条 DOM 记录及 664 个唯一 ID，固定 limit=60 |
| 原始对话 | 13 页、241 条 DOM 记录及 241 个唯一 ID，固定 limit=20 |
| 会话结论 | 2 页、63 条及 63 个唯一 ID，固定 limit=60；较早 lesson 单独筛选可达，独立待解决卡片保留 |
| 同页写入，刷新第二页注入一次 503 | 第一次页成功但不发布部分新链；63 条旧记录、同一 textarea DOM 和未保存草稿均保留 |
| 显式重试 | 实际请求 offset=0/60，提交同一新版本/新时刻的 63 条；原 textarea 与草稿保持，全局旧错误清除 |
| 外部标准写入后继续翻页 | 真实 409，60 条旧记录和草稿保留，禁止追加，无全局旧版本重试 |
| 版本冲突后重载并翻页 | 第一页不带旧上下文，之后沿用新上下文到 61 条 decision 末页，草稿保持 |
| 原文展开后通过页面追加对话 | 整批重建 13 页，共 243 个唯一 Episode ID；长原文节点身份与展开状态保持 |

本地浏览器脚本位于 `data/console-pagination/consistency/browser-01.mjs` 至 `browser-14.mjs`；精确调用形如 `ego-browser nodejs -e "await import('file:///E:/MnemoTrace-worktrees/console-pagination/data/console-pagination/consistency/browser-12.mjs')"`。成功断言来自 09、10、12、13、14；08 核对最终包和首屏上下文。

验收过程的边界与修正：初次导航早于服务就绪；随后发现从数据子目录启动的脚本导入了主工作区旧安装，导致旧递增 limit 页面卡在 500。将 Python sys.path 显式绑定 worktree 后，仅重启本轮 8770 实例，并核对服务 __file__ 与静态目录均在 worktree，重新登录最终包完成全部遍历。旧安装页面结果没有计为通过。草稿验收首次跨路由操作会卸载编辑器，因此改为同一页面编辑另一行触发真实写后失效；不把跨路由草稿保留纳入本次能力声明。取消变异首次探针挂起，只停止该探针自己的 Node 进程后加超时重跑，未停止用户服务。

保证边界：data_revision 跟踪已提交服务写入，裸 Memory 未保存修改不受保护；evaluated_at 仅固定事实浏览判定，不改变原始时间戳，不扩展画像生成或记忆检索为独立双时间查询。重载成本随已加载深度增加，任意后续页失败仍保留旧链；无历史快照或持续写入自动追赶。未运行准确率 benchmark，未更新公开成绩。

最终交付核对：485 个本地文档链接均有效；新增文件空白和新增行编号扫描通过，两份锁文件未改，台账独立的暂缓调整保留；原 E:/MnemoTrace 工作区只读核验为干净。原 8769 手工进程未重启，修复后的 8770 合成验收实例仍运行，可用于继续人工测试；两者的后端进程版本不同。


## 首屏写入刷新修订验收

2026-10-23；同一分支、基线和 worktree。本轮仅修改共享前端写后刷新入口、回归和相关规格/文档，HTTP/SDK/存储及算法不变；无提交、推送、合并或部署。

根因：React Query 的 Query.fetch 仅在已有 data 时使用 cancelRefetch 取消旧请求；新筛选的首屏没有 data，标准 invalidateQueries 会复用旧 pending 请求。旧响应成功后把写入前的记录和计数发布，并清除失效标记。当前新增表单在首屏加载期间可用，因此实际可达。

先补规格及一致性门禁，再通过真实 useAddFact、MutationObserver、InfiniteQueryObserver 和 QueryClient 固化失败：`pnpm --dir frontend exec node --test --test-name-pattern='write during' tests/memoryPages.test.cjs` 在旧实现下失败，写后请求数为 1 而非预期 2。日志 `data/console-pagination/first-page-write-red.log` 留在本地忽略目录。

修复统一放在 queries.ts::refreshAfterWrite：捕获登录周期，先取消全部受影响查询根，等待取消完成并核对登录周期后才失效。useInvalidateMemory 以及冲突/策略/working 旁路共用该入口；保持原写入成功回调时机，不让后台刷新阻塞已成功的写入。仍由 Query 提交结果，没有手动写缓存或第二请求状态机。取消不依赖传输层遵守 AbortSignal。

| 命令 | 本轮结果 |
| --- | --- |
| `pnpm --dir frontend exec node --test tests/memoryPages.test.cjs` | 32 passed；覆盖无缓存首屏、搜索/筛选首屏、旧成功/失败、替换读取失败、连续写入、完整已加载深度、单次概览、另空间及登录周期隔离、全部相关写入入口 |
| `pnpm --dir frontend exec node --test` | 260 passed，0 failed，0 skipped；11.94 秒 |
| `pnpm --dir frontend build` | TypeScript / Vite PASS；最终主包 index-CHwWPvc8.js |
| `python -m pytest --basetemp=data/console-pagination/write-barrier-pytest --junitxml=data/console-pagination/write-barrier-pytest.xml -rs`（TEMP/TMP 仍指向 worktree 忽略目录） | 1088 passed，20 skipped，4 warnings；130.83 秒 |
| `python scripts/check_zero_setup.py` | PASS；只复核既有日志，不是新 benchmark |
| `git diff --check`、编号/新增文件空白及文档链接检查 | PASS |

Python 跳过与警告范围同前一节，未视作通过。SDK 未发生本轮修改，沿用前一节已完成的类型、构建及 11 项契约检查，不把它们记作本轮重跑。

生产验收使用独立 8771 实例、独立合成库 `data/console-pagination/first-page-write/store`，服务源文件和静态目录均显式绑定该 worktree；原 8769/8770 服务不动。种子为三个同槽合成事实，其中一个 live，页面首次 total=1；随后通过新增表单写入两个不同合成主体，live total 依次变为 2 和 3。未使用真实个人数据。

浏览器使用 ego-browser 任务空间 12 和真实生产包 index-CHwWPvc8.js。网络包装只延迟服务真实返回的首屏响应，不改响应数据；旧响应延迟期间使用真实新增表单提交，确认新的 GET 完成并展示新记录，再释放已经取消的旧响应：

- 无缓存首屏：两次实际读取，提交版本变化，旧信号已取消，旧响应仍挂起时 UI 已显示新增事实及 total=2；释放后无倒退、无错误。
- 搜索首屏：两次实际读取，提交版本变化，新记录和 total=3 在旧响应释放前已显示；释放后仍保留三条，搜索输入 DOM 身份保持，无旧错误提示。

断言见[写后取消浏览器 JSON](console_pagination_write_barrier_browser.json)、[最终页面 DOM](console_pagination_write_barrier_dom.txt)、[本轮检查摘要](console_pagination_write_barrier_checks.json)。准确脚本为本地 `data/console-pagination/first-page-write/browser-01.mjs` 至 `browser-04.mjs`；调用形如 `ego-browser nodejs -e "await import('file:///E:/MnemoTrace-worktrees/console-pagination/data/console-pagination/first-page-write/browser-03.mjs')"`。结束后已关闭浏览器任务空间，未作截图声明。

原独立审查子代理再次只读复核：确认原 P2 修复，无新增阻塞项；检查了取消/登录周期屏障、旁路入口、真实写入 hook 回归及生产证据，没有重复宣称独立运行完整套件。
