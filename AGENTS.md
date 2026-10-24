# MnemoTrace — 项目章程与工作规则

产品：忆痕 / MnemoTrace；导入包：`mnemotrace`；Python/npm 发行包：`mnemotrace-memory`。

本文件是项目使命、设计原则和贡献规则的唯一维护来源，适用于人类与智能体。开始工作前完整阅读，再按[任务入口](#reading)加载所需材料。`CLAUDE.md` 只导入本文件；贡献指南、Spec Kit 宪章和论文检查清单不另行定义规则。

## 使命与核心不变量

**为 LLM 智能体构建一流的长期记忆系统，完全开源，并取得任何人都能复现的有力结果。**

每项设计至少服务于准确率与成本、多跳推理、双时间与冲突、可复现评测、规模扩展或异步整合之一。战略下注、里程碑与性能目标见[架构总览](docs/architecture/README.md#目标与战略方向)；目标不作为当前性能声明。

<a id="principle-i"></a>

### I. 可复现性不可妥协

- 使用仓库唯一的 `eval/` 框架，保持相同摄入、answerer、judge 与骨干设置；采用官方 judge 提示和强 judge，声明相对基准设置的偏离。不同框架的数字不能作同条件比较。
- 同时报告准确率、总 token、p50/p95 延迟及同次运行、相同 answerer + judge 的 full-context baseline；未超越时明确说明。区分平均上下文与总 token、检索与端到端延迟。
- 公开质量数字使用完整集合及多个骨干模型（一个小型开放模型与一个前沿模型）。切片只作为探索，不能替代全集结论。
- 每项公开数字关联已提交的原始 JSONL、精确命令和代码 commit SHA，并可由 `python eval/report.py <file>` 重推。运行有未提交改动时保存完整 diff；无法恢复的历史条件如实标明，不补造证据。
- 标为 DONE 或用于公开/论文结果前运行 `python eval/validate_results.py --expected-rows <full item count> --require-complete <file>`；LongMemEval_S 为 500。混合日志用可重复的 `--system <name>` 选择被引用系统，不称整份日志为干净。
- 修改头条数字时同步两份 README、`RESULTS.md` 和两份生成网页；保留页面有意差异。模板与差异配置生成后运行 `python scripts/build_landing_pages.py --check`。操作见[评测说明](eval/README.md)。

<a id="principle-ii"></a>

### II. 零配置不变量

`python examples/quickstart.py`、已安装的 `mnemotrace-quickstart` 和 `pytest` 必须在无 API key、无第三方服务时运行，使用哈希 embedder、规则提取器和内存存储。破坏零配置示例或离线套件的变更直接拒绝，没有例外。

<a id="principle-iii"></a>

### III. 接口优先

Python ≥ 3.10；核心仅用标准库，零硬依赖。LLM、embedder、向量/图/文档存储及词法索引均置于接口后，提供零依赖确定性回退。真实后端通过可选 extras、在对应实现中延迟加载，核心顶层不导入重型依赖。

<a id="principle-iv"></a>

### IV. 不允许静默损坏记忆

- Fact 保留有效时间、事务时间、`provenance` 和 `supersedes`；矛盾采用非破坏性失效，保留来源与历史，不能覆盖后遗忘或因反驳而硬删除。
- 冲突先用槽位、嵌入相似度和内容包含关系区分更新、矛盾与补充；相同 `(subject, predicate)` 的不同 object 只是候选冲突。明显矛盾且时间明确时设 `old.invalid_at = new.valid_at`、`new.supersedes = old.id`，仅歧义可升级 LLM。
- 显式单条擦除可删除指定历史事实；`clear_slot` 批量擦除只选 live 非手写事实，保护替代链。工作记忆不静默变成长久画像。
- 存储路径不直接拼接原始 tenant/user 文本，删除目标必须是根目录内已验证子项。生产 HTTP 不默认匿名开放，liveness 不能代替 readiness。

<a id="principle-v"></a>

### V. 先测量，再优化

准确率与性能声明来自 harness。先测瓶颈再引入 Rust 或缓存；融合权重配置化并由评测调优。测量运行间波动与噪声下限，落在波动范围内的净收益不算胜利，answerer 波动可能超过检索收益。

<a id="principle-vi"></a>

### VI. 组合，而非单选

- 组合类型化记忆、双时间图谱、分层分页、多信号融合、显著度衰减与异步整合。读取必须结合整合后的事实与检索到的原始会话片段，禁止仅靠事实交付问答。
- 时近性与显著度只作先验；没有词法、语义或图谱证据的项不能仅靠先验变相关。规划依据证据形态，不读取数据集标签做 benchmark 投机分支。
- 模块小且可组合，按下一里程碑构建。复杂度须由测量和明确职责论证，不为想象中的需求增加抽象。代码导航见[架构总览](docs/architecture/README.md)。

<a id="principle-vii"></a>

### VII. 诚实、开放的对外措辞

- 描述自身能力，对外不点名或做竞争对手 benchmark 比较，不包装为“借鉴自某大公司”。未有已提交证据时不声称“1M”“SOTA”“#1”或成本恒定，目标与实测明确区分。
- 不提交真实个人数据、姓名或密钥；示例用 `user123`、`my-app` 等合成值，保持 `.env` 被忽略。发现历史 PII/凭据立即标记，一次删除提交不等于修复历史泄露。
- 提交信息平实且具描述性，不添加 AI 署名 trailer；已有此类 trailer 合入前移除。
- 开放、可自托管、无厂商锁定；采用 AGPL-3.0 与商业双许可，商业使用不遵守 AGPL 时须获授权。具体条款以 [LICENSE](LICENSE) 和 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md) 为准。

<a id="reading"></a>

## 按任务读取

| 任务 | 先读 |
| --- | --- |
| 使用或理解产品 | [文档入口](docs/README.md)、[快速开始](docs/quickstart.md) |
| 算法、数据模型、生命周期 | [架构总览](docs/architecture/README.md)、对应专题、[设计取舍](docs/decisions.md)、[未完成事项](docs/issues.md) |
| 接口或接入 | [Python](docs/reference/python-api.md)、[HTTP](docs/reference/http-api.md)、[MCP](docs/reference/mcp.md)、[Agent 指南](docs/guides/agents.md) |
| 运行评测或引用成绩 | [RESULTS](RESULTS.md)、[评测操作](eval/README.md)、[证据目录](results/README.md) |
| 开发、验证、部署 | [贡献指南](CONTRIBUTING.md)、[部署说明](deploy/README.md) |
| 重大变更设计 | [Spec Kit 原则索引](.specify/memory/constitution.md)、当前 `specs/` 套件 |

<a id="workflow"></a>

## 工作流程

1. 明确范围，核对实现、测试和相关文档；只读任务保持只读。
2. 普通缺陷、文案和局部重构直接实施并做必要验证，目标与结果在对话或 PR 交付。
3. 重大算法、接口、数据模型、安全边界或跨模块取舍走 `specify → clarify（按需）→ plan → tasks → analyze → implement`；实施前一致性检查必须通过。违反 II/IV 的规格直接拒绝，先重新设计。
4. 重大任务保留 spec/plan/tasks/analysis；仅有独立需要时增加 research、data-model、contracts 或 quickstart，不生成空占位文件。完成后提炼知识和证据，移除过程套件并清除本任务活动指针。
5. 规格用时间戳命名，规则文件不保存当前计划指针。内部正文优先中文，API、标识符、命令和文件名保留原文；注释解释非显然原因。

当前为私密仓库，尚无外部旧版用户。默认直接修改当前版本，不新增历史兼容分支、别名或自动迁移；任务明确要求兼容时才增加。发布或使用范围变化后重新评估。

<a id="documentation"></a>

## 文档与产物生命周期

- 先更新现有权威页面；新增长期文档须有独立读者和职责，并接入导航。纯调研默认在对话交付，用户要求文件时再创建。
- 用法/契约变化更新对应指南或参考；机制/生命周期变化更新对应架构；模块关系变化才更新总览；形成长期取舍才更新决策。不要求每次修改同步多份报告。
- 缺陷、待办、风险和待验证行为的状态只在 `docs/issues.md` 维护。当前文档解释行为和理由，不记录对话过程、调试轮次或历次通过数量。
- 参数默认值以代码为准，参考页说明含义与影响面；算法开关须能在 Config 和 harness 中消融。被替代或未证实的方案保留必要反证与适用范围。
- 临时脚本、截图和终端输出写系统临时目录或 `.artifacts/<task>/`。任务结束仅清理自己创建且无需交付的内容，不清理用户原有文件、运行环境或记忆库。

<a id="evidence-artifacts"></a>

- 每次算法实验本地留存命令、数据范围、系统、代码状态和结果，包括失败尝试；明确 failed/partial/exploratory，不默默丢弃或作为正式质量证据。
- 仅被公开结果、采纳/否决取舍、必要发布验收或回归 fixture 消费的材料进入受管 `results/`。普通测试结果留在对话、PR 或 CI，不新增独立验收 Markdown。
- 原始日志、补丁和必要源码快照按证据原样保留；历史记录中的路径不重写。缺失来源条件如实标明。目录、schema 和校验操作见[证据说明](results/README.md)。
- 移动或删除页面时更新链接、锚点、代码和构建消费者；生成文件改来源后重新生成，不直接手改。论文中的历史检查清单不覆盖本文件。

## 验证与完成

为新行为添加有效回归；已安装测试依赖时推送前运行 `pytest`。验证范围依变化选择：

| 变化 | 必要验证 |
| --- | --- |
| 纯文档 | `git diff --check`、仓库链接/导航检查；迁移另查路径消费者 |
| 抽取/过滤局部增强 | 目标单测、离线消融、真实相关切片 context JSONL |
| read path/planner/fusion | 单测、离线消融、真实 miss/类别切片、tokens 与延迟 |
| answerer/judge/harness | 小样本 QA、validator、同 rig full-context 对照 |
| 个人导入/会话收尾 | 合成回归、导入/收尾/重启、隔离及脱敏前后对比 |
| 服务/存储/安全 | 路径、租户、链接、鉴权、并发、zero-setup、发布及相关构建 |
| 控制台/缓存 | 组件与状态回归、类型检查、生产构建，必要时实际页面验证 |
| 公开数字 | 完整 validator、原始日志、精确复现命令、统一 baseline、全部公开入口 |

算法读写 PR 同时有本地正确性与真实数据验收；预计影响基准行为时新增一次 LongMemEval、LOCOMO 或 PersonaMem 的 `eval/bench.py` 运行。每步算法可开关、可消融，harness 开关在 `mnemotrace_config` 可关闭。PR 报告命令、行数、系统、准确率、token、p50/p95 延迟、产物及回归/中性/改进结论。

局部规则用相关小切片，planner/fusion 从 20–50 条相关类别起，read path 大改用 100 条或全量 500 题。影响全局排序、预算或公开质量主张时运行完整集合；多项局部增强合包或可能影响 headline 也触发完整评测。`session_outcomes` 只由会话/持久化回归验收，未走收尾入口的 benchmark 不能替代。

发布 benchmark 或声称干净检出可运行前执行 `python scripts/check_zero_setup.py`。个人语料、离线测试、探索切片和完整 benchmark 的范围互不替代；工程发布成功不代表算法提升。

完成时检查新增文件归属、未跟踪文件、临时输出、证据消费者与最终 diff；在对话/PR 报告实际命令、结果、跳过项和限制。当前文档不把某次测试数量当作长期保证。

## 规则治理

规则只在本文件修订；其他入口仅同步链接/索引。MAJOR：移除原则或不兼容重定义；MINOR：新增章节或实质扩展；PATCH：不改变含义的澄清。PR 用一句话说明修订原因。

规则版本：**1.2.1**；首次批准：2026-06-25；最近修订：2026-10-23。1.2.0 引入文档与产物生命周期及按风险选择流程；本次仅修正路径标记格式，规则含义不变。
