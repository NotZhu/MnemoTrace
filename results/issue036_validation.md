# ISSUE-036：筛选范围与旧响应复用验收

日期：2026-10-21。工程验收环境：Windows x64、Node.js v24.19.0、Vite v5.4.21、Ego Lite Chromium。
实现基于 `e9e52bdda90f944eff9c29871498e463c9fb0e82` 的未提交工作树。本文与原始输出供
[问题台账](../docs/known-issues.zh-CN.md#issue-036)及架构文档引用，不是 benchmark 证据。

## 根因与改动

查询标识已经包含 include_sensitive，API 请求也传递 false；漏洞在 placeholderData 仅比较账号和 kind，
允许新查询借用不符合范围的完整旧响应。useMemories 现在只允许 q、facts_limit、episodes_limit 变化，
其余字段均参与范围比较；敏感默认值 false 与空分类按服务端语义归一化。使用既有 hashKey 稳定比较，
不改变完整缓存标识，不清空其他查询，也不在页面复制服务端过滤。

Facts 将加载、失败和重试移至列表区域；工具栏保持挂载，无数据或占位时计数显示等待，分类候选不跨范围沿用，
旧空列表显示更新而非新搜索无结果，占位期间禁用加载更多。错误在列表内显示并提供当前查询 refetch，
全局错误条保留其既有告警与重试能力。Layout 的 Facts 分支在全局查询错误时保持挂载，否则本地状态分支无法工作。
其他页面的错误边界仍由 [ISSUE-016](../docs/known-issues.zh-CN.md#issue-016)跟踪。

## 失败回归与最终验证

全部原始输出见 [issue036_validation.jsonl](issue036_validation.jsonl)。

| 日志 label | 范围与结果 |
| --- | --- |
| red_scope_and_page | 20 项，6 通过、14 失败；实际 hook + QueryObserver 及 Facts 渲染复现跨范围旧数据和整页加载等问题 |
| green_scope_and_page | 19 通过、1 失败；测试树遍历未包含 ErrorBanner.action，无法找到重试按钮，属于测试工具缺口 |
| green_scope_and_page_corrected_harness | 补齐 action 遍历后 20 项通过 |
| red_layout | 8 项中 1 失败；全局错误卸载 Facts，验证组件测试以外的实际父层边界 |
| query_isolation | 94 项通过，含新增范围测试及既有登录周期、账号隔离 |
| search | 36 项通过，含页面、布局、搜索与筛选 |
| frontend_all_final | 208 项通过，0 失败、0 跳过 |
| frontend_build_final | TypeScript 检查和 Vite 生产构建退出码 0 |
| browser_production | 生产构建、真实浏览器 DOM，11 个观察点通过 |
| browser_category_search_sensitive | 分类候选没有 private；搜索 + 分类 + 隐藏组合等待自己的响应，通过 |
| http_memories_native | 3 项通过、47 项未选中；默认敏感过滤、组合分页、分类可见范围及命名空间保持有效 |

最终前端命令（frontend 目录）：

```powershell
npm run test:query-isolation
npm run test:search
node --test tests/*.test.cjs
npm run build
```

仓库根目录的 HTTP 补充命令（Python 3.12.6、pytest 8.4.2）：

```powershell
python -B -m pytest tests/test_server.py -k memories -o addopts= -vv --basetemp=.pytest_cache/issue036-http-native
```

普通执行环境下 3 项通过、47 项未选中；两个既有 asyncio 配置警告。首次沙箱运行长期未完成，
已停止并单列为 interrupted，没有可用的子进程输出，不计作通过。未修改后端后，在原生环境限时 60 秒运行上面的命令完成验证。

范围测试覆盖双向敏感切换、缺省值、分类、状态、kind、分页偏移、未来筛选字段、账号切换、已有当前条件缓存，
以及受控 Promise 的失败、重试、连续切换与宽范围响应晚到。Journal 搜索仍复用，历史范围改变则等待。
页面测试检查占位空结果、总数、分类、分页禁用、重试和父布局；既有测试覆盖组合参数传递。

## 浏览器复现

在 frontend 运行 `node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 4177 --strictPort`。
按 ego-browser 技能在一个 TaskSpace 内加载生产页面；在首次导航前用
`Page.addScriptToEvaluateOnNewDocument` 安装 [合成 fetch fixture](issue036_browser_fixture.mjs) 的
`installFixture` 函数（source 为函数转字符串后立即调用）。导航到 `/ui/facts`，用新的 synthetic Key 登录，
确认初始包含两条合成事实，再执行 [checkFacts(page)](issue036_browser_checks.mjs)。
结束时关闭本次 TaskSpace；不操作其他用户标签页或数据。

fixture 仅替换 fetch 响应，不替换 React、查询库、登录与偏好存储、路由或 DOM。返回敏感/非敏感两条合成记录，
以手动释放 Promise 模拟慢响应和 HTTP 503；验证隐藏等待、失败、重试等待/成功、连续切换、旧宽范围响应晚到、
成功空结果、占位空结果和搜索恢复。额外打开分类下拉，确认只有 public，再选 public，验证组合筛选先加载后显示。
浏览器记录保存于 JSONL，全部正文与账号为合成数据。

最初沙箱监听的 4176 端口无法访问，浏览器报告 ERR_CONNECTION_TIMED_OUT，PowerShell 回环请求报告 socket 权限错误；
改在批准的本机回环预览进程运行 4177 后完成验收。一次脚本相对导入多写一层导致 ERR_MODULE_NOT_FOUND，
修正脚本路径后继续同一 TaskSpace；未将这两次工具失败算作产品通过。初次测试日志已写入，随后终端 GBK 输出失败，
改用 UTF-8 查看；原始测试结果没有丢失。

验收后关闭本次浏览器 TaskSpace 和两个本地预览进程。最终 `git diff --check` 与文档链接、台账索引检查见
JSONL 的 diff_check / documentation_check；既有 LF/CRLF 提示不属于空白错误。

## 验证边界

浏览器使用合成响应，不代表真实后端部署或跨浏览器兼容验收。后端默认过滤与组合分页另以既有 HTTP 测试检查，
结果见 JSONL 的 http_memories 系列记录。实现阶段未运行完整 pytest，推送前补充结果见下节；未运行模型 benchmark、zero-setup 或发布检查。
本次无后端、存储、算法及公开质量数字变更；服务部署不在本次交付范围内。
隐藏范围仅针对事实查询响应，不改变独立冲突卡片、来源接口或画像，也不把 ISSUE-016、038 标为解决。

## 推送前完整回归

用户确认提交信息并授权推送后，在普通 Windows 执行环境运行：

```powershell
$env:PYTHONUTF8 = '1'
python -B -m pytest -o addopts= -q --basetemp=.pytest_cache/sensitive-query-prepush-native
```

872 passed、14 skipped、4 warnings，退出码 0，pytest 报告耗时 133.40 秒。警告为两个既有 asyncio 配置项未识别及两个 websockets 弃用提示。
本轮没有安装或更新依赖；完整脱敏输出保存于 JSONL 的 prepush_pytest_native 记录。
