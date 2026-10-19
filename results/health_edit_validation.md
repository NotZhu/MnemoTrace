# 记忆体检：展示与整句编辑验证

重新检查按钮的后续加载闪烁修复与时序回归见 `health_recheck_validation.md`。

日期：2026-10-15。使用合成数据，没有修改真实命名空间。

## 变更

- 标签缩为 12px；缺少有效信息沿用红色主题。有问题时结果图标为红色警示图标。
- 编辑/删除和保存/取消与正文同行，按钮间距 12px。清理入口缩小为中性色按钮。
- slot_overflow 的事实样本使用与其他正文相同的 14px 字号、颜色和行高。
- 整句草稿优先读取 display，其次 text；镜像 textarea 原位换行、失焦保存，按钮接管操作。
- HTTP statement 模式保存完整断言，不拼接旧前缀；失败保留草稿，空白/混合字段拒绝。
- 预览补齐 9 类已有发现；新增内容过长、内容不完整、代码/路径、检索模型文字支持不足。

## 验证

`python -m pytest tests/test_fact_statement_updates.py tests/test_fact_metadata_updates.py tests/test_server.py tests/test_profile_evidence.py --basetemp <新建的可写测试目录> -q`

通过。原始输出见 `health_edit_pytest.txt`。覆盖整句无前缀重复、嵌入/图更新、落盘重新读取、来源
与 provenance 保留、原样保存不激活历史、无效请求拒绝、结论依据保留，以及现有接口与画像回归。
本机初次沙箱运行无法创建 pytest 临时目录，TestClient 也未完成；使用独立工作区临时目录在沙箱外重跑通过。
当前 pytest 环境对两项既有 asyncio 配置有 Unknown config option 警告。

`cd frontend; node --test tests/healthEditing.test.cjs`：3 项通过。
覆盖整句草稿与 statement 请求、失焦提交、防重复请求、取消/原样保存不提交、按钮接管失焦与失败重试。

`cd frontend; npm run build`：TypeScript 与 Vite 构建通过。
`git diff --check`：本次相关变更通过（Windows 环境有既有 LF/CRLF 提示）。

独立预览服务 `/ui/health` 返回 HTTP 200；`/v1/audit` 返回 9 项，9 类各 1 项。
浏览器自动观察此前受访问策略限制，本次未绕过该限制；通过页面链接交给用户审阅视觉效果。

## 语义边界

samples[].text 是已保存的事实内容，不能称作来源对话逐字原文。预览文本全部是合成样例。
statement 是完整用户断言，保留原 subject/predicate 槽位，不调用模型猜测或重抽结构。
需要改变结构化主语/关系时，仍使用事实管理的结构化编辑入口；本次不修改抽取或审计规则。
2026-10-15 用户确认替换提示文案后，9 类发现的原因/操作提示按已审阅版本更新；
slot_overflow 的动态提示同步保留属性名称和当前记录数量。该轮仅替换提示文案。
本轮为文案替换，重新执行前端 TypeScript/Vite 构建通过；未重复运行此前已通过的逻辑测试。

## 2026-10-15：提示简化与单条删除弹窗

去掉原因与建议间的箭头、待确认标题的说明与图标。灰色标签采用事实管理的 bg-ghost/10、
text-ghost；蓝色复用 fact-blue-tag，包含浅色主题的蓝色适配。红色与琥珀色类别沿用原分配，
严重程度配色仅向用户提出方案，本轮没有应用。

单条删除改用项目 ConfirmDialog，展示实际记忆正文及“删除后无法恢复，来源对话仍会保留”。
点击行级删除只打开弹窗，确认才提交；取消不请求，防止重复确认导致重复请求，失败保留弹窗可重试。
批量清理确认仍保留数量与样本。没有修改后端删除语义。

`cd frontend; node --test tests/healthEditing.test.cjs`：5 项通过（原 3 项编辑回归 + 2 项删除确认回归）。
`cd frontend; npm run build`：TypeScript/Vite 通过。

## 2026-10-15：批量删除文案、紧凑弹窗与优先级颜色

用户确认应用此前审阅的方案后，标题改为动态“删除这 N 条记忆？”，正文优先使用接口 label，
缺失时回退 predicate。底部说明删除不可恢复、来源对话保留；按钮继续使用取消/删除。
Health 两类确认弹窗启用 compact：底部内边距改为 16px，内容与按钮之间改为 16px；
批量确认正文区间距缩为 8px。Modal/ConfirmDialog 默认间距不变，其他页面未启用 compact。

颜色按默认处理优先级分配：红色为 empty_value/embedder_blind；琥珀色为 machine_token、
slot_overflow、fragment、dangling_subject；蓝色为 unreduced_claim/code_artifact；灰色为 orphan_entity。
蓝色继续复用事实管理的 fact-blue-tag，灰色继续使用 bg-ghost/10 与 text-ghost。
颜色不表示已经证实错误，不新增后端严重程度字段或改变审计判断。

`cd frontend; node --test tests/healthEditing.test.cjs`：5 项通过。
`cd frontend; npm run build`：TypeScript/Vite 通过。

## 2026-10-15：按颜色优先级排序

汇总分类与问题列表统一为红色、琥珀色、蓝色、灰色，同级维持原有顺序。
Health 在同一配置内定义每类问题的颜色及排序等级，排序前复制查询结果，不改写缓存。
MemoryService.audit 在 limit 截取前排序，确保靠后发现的高优先级问题也能进入首批结果；
统计仍覆盖全部发现。分组行 key 加入 predicate，保证排序后各属性组的组件身份独立。

`cd frontend; node --test tests/healthEditing.test.cjs`：6 项通过（新增全 9 类渲染顺序、同级稳定性、缓存不变检查）。
`python -m pytest tests/test_server.py -k 'audit_flags_junk or audit_finding_can_be_fixed or audit_sorts_before_limit' -q`：
3 项通过，覆盖排序后截取、同级顺序、全量计数与原有审计修复流程；原始输出见 `health_audit_order_pytest.txt`。
`cd frontend; npm run build`：TypeScript/Vite 通过。
