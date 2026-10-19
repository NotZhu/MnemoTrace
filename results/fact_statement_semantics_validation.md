# 属性事实整句编辑的三元组语义修复

日期：2026-10-18。范围仅限审查的第三项：避免把完整陈述写入属性事实的 object、同时保留旧主语和关系。

## 行为与边界

- `Memory.update_fact(statement=...)` 仅接受已有谓词为 `decision`、`finding`、`lesson`、`open_question` 的会话结论。
- 属性事实的 statement 请求（包括原文回传）在改变正文、来源、敏感标记、分类或图谱之前抛出 `InvalidFactEditError`；HTTP 编辑入口映射为 422。不存在的事实仍返回 404。
- Health 属性编辑展示“人物或对象 / 关系类型 / 具体内容”，提交 subject/predicate/object，显式保存；字段切换不保存。结论保留整句编辑、依据和既有持久化路径。
- 更新 API 与两份架构说明。不新增模型调用、不变更存储格式、不迁移或重处理真实记忆；其余审查问题不在本次修改范围。

## 失败复现

先更新回归断言，再修改实现：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_fact_statement_updates.py -q --basetemp=E:\MnemoTrace\.pytest_cache\issue3-red-20261018 --tb=short
```

修复前 3 项失败：两种属性 statement 请求未抛 ValueError，HTTP 编辑函数也未拒绝该请求。

```powershell
cd frontend
node --test tests/healthEditing.test.cjs
```

前端修复前，新增的属性编辑用例失败：仍渲染 StatementEditor；无法提交明确的三字段草稿。

## 修复后验证

在仓库根目录执行：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_fact_statement_updates.py tests/test_fact_metadata_updates.py tests/test_outcomes.py tests/test_profile_evidence.py tests/test_conflict.py tests/test_smoke.py -q --basetemp=E:\MnemoTrace\.pytest_cache\issue3-regression-20261018 --tb=short
```

112 项通过。覆盖属性拒绝且不改变事实或图谱；结构化修正后的来源、画像取值、实体与持久化；四类结论整句编辑、依据、来源与保存重开；元数据和历史回归。
HTTP 状态测试直接调用真实路由函数、请求模型与 MemoryService，不经过 TestClient 线程。
当前 Python 环境提示两项已有 asyncio pytest 配置未识别，不影响本组同步测试。
`tests/test_server.py` 中相应 HTTP 往返用例已更新，但未在本次运行 TestClient 集成测试。

```powershell
cd frontend
node --test tests/healthEditing.test.cjs tests/healthRecheck.test.cjs tests/factsFilters.test.cjs tests/profileEvidence.test.cjs
npm run build
```

21 项前端测试通过；TypeScript 与 Vite 生产构建通过。覆盖三字段绑定、请求载荷、空值/原样/取消不提交、失败保留草稿、防重复保存、结论兼容、体检刷新、事实过滤和画像来源展示。

所有输入均为合成数据；未调用外部模型或真实记忆服务。此记录是缺陷回归证据，不是 benchmark 结果。
