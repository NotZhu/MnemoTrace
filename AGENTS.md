# MnemoTrace — 项目章程与工作规则

> 产品名称：忆痕（中文）/ MnemoTrace（英文）。
>
> 导入包名：`mnemotrace`；Python/npm 发行包名：`mnemotrace-memory`。

本文件是项目使命、设计原则和贡献规则的唯一维护来源，适用于人类贡献者与智能体。编写代码前必须完整阅读，再按[文档入口](#reading)读取当前任务所需资料。

`CLAUDE.md` 仅负责导入本文件；贡献指南和 Spec Kit 宪章仅提供操作入口与原则索引。当前实现由代码和架构报告说明，实验成绩由 `RESULTS.md` 及其原始日志记录，阶段进度由对应规格与计划记录；这些材料不另行定义项目规则。

## 1. 使命与战略方向

**为 LLM 智能体构建一流的长期记忆系统，完全开源，并取得任何人都能复现的有力结果。**

跨会话遗忘迫使智能体反复读取完整历史，增加成本、延迟与信息干扰。项目要验证：精确检索能否在降低成本的同时超越全上下文准确率，以及多跳规划、双时间记忆能否改善跨会话和时间推理。开放、中立的评测框架与可复核日志是产品交付的一部分。

每项设计决策必须至少服务于以下一项战略方向：

| 标识 | 方向 | 验证重点 |
| --- | --- | --- |
| Bet A | 在准确率上超越全上下文，同时降低成本 | LongMemEval / BEAM 上的准确率、token 与延迟 |
| Bet B | 用多跳查询规划器攻克难题类别 | 子查询分解、图谱遍历与跨会话证据汇总 |
| Bet C | 双时间模型与低成本冲突检测 | 知识更新、历史查询与完整来源链 |
| Bet D | 以可复现评测框架建立可信成绩记录 | 统一流水线、强 judge、公开原始日志 |
| Bet E | 线性扩展、分层分页与显著度衰减 | 在 10M+ tokens 下保持能力 |
| Bet F | 异步 / 休眠期整合 | 将图谱构建、去重和冲突处理移出关键路径 |

**以下均为设计与验收目标，不是当前版本的性能声明：**

- System-1 热写入目标 <50ms；读取路径目标 <100ms；System-2 面向秒级异步整合。
- M0：零依赖 `add → consolidate → search` 闭环，含双时间、冲突失效、多跳规划器桩、as-of 与微型评测。
- M1：真实后端与真实评测，在相同题目上运行全上下文基线；LongMemEval_S 全集目标超过 85%。
- M2：攻克多跳与时间推理，LongMemEval_S 目标 90+，准确率超越全上下文。
- M3：在 1M+ tokens 的 LongMemEval_M、BEAM-1M/10M 上验证扩展能力，避免规模增长时性能骤降。
- M4：公开框架和原始日志，通过 MCP、SDK 和更多基准扩大复现与采用。

历史成绩、版本限制和复现入口统一见 [RESULTS.md](RESULTS.md)，阶段状态见对应计划。

## 2. 核心不变量

原则 I–VII 保留 Spec Kit 的既有标识；I 和 VII 的完整条款位于[评测与贡献规则](#evaluation)。

<a id="principle-ii"></a>

### II. 零配置不变量

`python examples/quickstart.py`、已安装的 `mnemotrace-quickstart` 和 `pytest` 必须在无 API key、无第三方服务时运行，使用哈希 embedder、规则提取器和内存存储。破坏零配置示例或离线测试套件的变更一律拒绝，没有例外。

<a id="principle-iii"></a>

### III. 接口优先

- Python ≥ 3.10；核心仅使用标准库，保持零硬依赖。
- 每个外部依赖（LLM、embedder、向量 / 图 / 文档存储、词法索引）都置于抽象接口之后，并提供零依赖、确定性的离线回退。
- 真实后端通过可选 extras 接入；核心模块顶层不得导入重型依赖，只在需要它的后端中延迟加载。

<a id="principle-iv"></a>

### IV. 不允许静默损坏记忆

- 从数据模型开始保留有效时间与事务时间。每条事实都能回答“来自哪里”（`provenance`）和“替代了什么”（`supersedes`）。
- 矛盾通过非破坏性失效处理，保留历史和来源；禁止覆盖后遗忘，禁止因事实被反驳而硬删除它。
- 冲突检测先使用槽位匹配、嵌入相似度和内容包含关系，区分更新、矛盾与补充。相同 `(subject, predicate)`、不同 object 只是候选冲突。
- 对明显矛盾且时间顺序明确的更新，设置 `old.invalid_at = new.valid_at` 和 `new.supersedes = old.id`，无需调用 LLM；仅有歧义时可选升级至 LLM 裁决器，不为每条事实调用 LLM。
- 显式单条擦除可以删除指定历史事实；`clear_slot` 的批量擦除只选 live 非手写事实，不能顺带抹掉整条替代链。工作记忆不静默写成长期画像。
- 存储路径不直接拼接原始 tenant/user 文本；删除目标必须是数据根目录内已验证的子项。生产 HTTP 服务不默认开放匿名访问，存活检查不能替代就绪检查。

<a id="principle-v"></a>

### V. 先测量，再优化

- 性能与准确率声明来自评测框架。不提前引入 Rust 或缓存；只有实测证明检索 / 索引热路径是瓶颈，才考虑下沉至 Rust。
- 检索融合权重由配置驱动，通过评测调优，不能凭空设定。
- 测量运行间波动与噪声下限；回答模型的波动可能超过检索收益。净效果落在波动范围内的改动不算胜利，必须如实报告。

<a id="principle-vi"></a>

### VI. 组合，而非单选

- 组合类型化记忆、双时间图谱、按热度分层的分页、多信号检索融合（RRF、可选重排）、显著度衰减、休眠期整合与线性扩展索引；这些机制的衔接是持续建设方向。
- 读取路径必须结合**整合后的事实 + 检索到的原始会话片段**。事实提供冲突已处理的双时间信号，片段补回细节；禁止交付仅靠事实的问答。
- 模块小而可组合，遵循[代码导航](#architecture)的职责划分；审阅者应能把每个文件对应到架构中的一部分。
- 面向下一个里程碑构建，不为想象中的 M4 需求增加抽象。复杂度须依据原则 V、VI 论证，移除无合理依据的复杂度。
- 时近性和显著度只能作为先验辅助排序，没有词法、语义或图谱证据的项不能仅靠先验变成相关事实。查询规划依据证据形态，不读取数据集标签作 benchmark 专用投机分支。

<a id="workflow"></a>

## 3. 工作流程与完成条件

### 开始与实现

1. 阅读本文件，再按[任务入口](#reading)核对相关实现、计划、风险与证据。
2. 功能按规格流程推进：宪章 → `/speckit-specify` → `/speckit-clarify`（按需）→ `/speckit-plan` → `/speckit-tasks` → `/speckit-analyze` → `/speckit-implement`。`/speckit-analyze` 是实施前的一致性门禁。
3. 所有规格、计划和 PR 均须验证合规性。违反原则 II 或 IV 的规格必须在重新设计前拒绝接受，不能仅标为“需要修订”。
4. 注释解释为什么；双时间不变量、冲突规则等非显然逻辑值得注释，getter 不需要。
5. 用户未指定其他语言时，内部文档优先中文；标识符、API 名称、命令、文件名、benchmark 名称和面向英文受众的公开材料保留适当原文或双语。

### 开发阶段与兼容策略

当前仓库为私密仓库，历史版本仅项目所有者本人使用，尚无外部旧版用户。

默认面向当前版本直接修改和简化实现，不为历史版本保留兼容分支、旧接口、参数别名或自动迁移逻辑。只有当前任务明确要求兼容时，才增加相关支持。

对外发布或使用范围变化后，重新评估此策略。

### 验证与完成

为新行为添加测试；已安装测试依赖时，推送前运行 `pytest`。按改动类型执行以下既有验收要求：

| 改动类型 | 必跑验收 | 完整 benchmark 的触发条件 |
| --- | --- | --- |
| 仅文档 | `git diff --check` | 不需要 benchmark |
| 抽取、过滤规则或候选表局部增强 | 目标单测、离线特性消融、真实相关切片 context JSONL | 多项合包或可能影响 headline |
| read path、evidence planning、fusion 权重 | 目标单测、离线 ablation、真实 miss / 类别切片、tokens 与延迟 | 影响全局排序、context budget 或公开数字 |
| answerer / judge 提示词或 harness | 小样本 QA、validator、同 rig 的 full-context 对照 | 质量主张需要完整集合；切片仅标为探索 |
| 个人会话导入与收尾 | 合成回归、导入 / 收尾 / 重启、目标隔离、脱敏前后对比 | 不走该路径的 benchmark 不能替代验收 |
| 服务、存储与安全边界 | 路径 / 租户 / 链接 / 鉴权 / 并发回归、zero-setup、发布检查及相关构建 | 改算法另加质量评测；线上生效须部署核验 |
| 控制台展示与缓存 | 组件 / 状态回归、类型检查、生产构建，必要时实际页面验证 | 不凭展示测试发布记忆准确率结论 |
| 公开 benchmark 数字 | validator、已提交原始 JSONL、复现命令与统一 baseline | 必须完整可验证，并同步全部公开入口 |

每个算法读 / 写路径 PR 都包含本地正确性检查和真实数据验收。预计影响基准行为时，新增一次 LongMemEval、LOCOMO 或 PersonaMem 的 `eval/bench.py` 运行；PR 门禁允许固定切片，公开声明仍遵循原则 I。PR 描述报告命令、行数、比较系统、准确率、token、p50/p95 延迟、制品路径及回归 / 中性 / 改进结论。

算法验证还有以下边界：

- 新增算法路径必须有目标单测或离线 ablation，禁止新增不可消融的算法路径。算法候选方向的每步实现都在 `Config` 中可开关、在 harness 中可 ablate，并留下实验日志；影响 harness 的开关在 `eval/bench.py::mnemotrace_config` 中可关闭。
- `session_outcomes` 属于会话关闭生命周期，标准 benchmark 不调用该入口，使用会话与持久化回归独立验证。
- 局部规则建议使用相关小切片，planner / fusion 建议从相关类别 20–50 条起，read path 大改使用 100 条或完整 500 题作对照；这些规模建议不替代完整 benchmark 的触发条件。

发布 benchmark 或声称仓库可从干净检出运行前，运行 `python scripts/check_zero_setup.py`。该脚本在不安装可选包时检查 quickstart、离线 harness、证据日志、论文统计与标准库编译。

个人语料、离线单测、探索切片和完整 benchmark 的验证范围不能互相替代。命令、环境、测试数量、跳过项、输出、操作过程与测量限制放在 `results/` 验收记录；架构文档保留证据链接和验证边界，不把某次通过数量当成当前全仓保证。

<a id="documentation"></a>

### 文档更新

- 智能体生成的算法或架构变更必须更新[架构优化地图](docs/architecture-optimization-map.zh-CN.md)：修改了哪一层记忆、原因、方案、取舍与证明效果的日志。
- 影响数据流、对象生命周期、存储形态、服务 / API 接口、评测流程或模块边界时，同时更新[全链路架构报告](docs/mnemotrace-full-architecture-report.zh-CN.md)、总览图和代码位置；交付范围变化同时更新交付说明。
- 两份架构文档按模块维护，不在页首或页尾追加日期变更块。合并重复内容，删除失效的当前实现说明；重要历史取舍放在优化地图并链接原始证据，不混写互相替代的方案。
- 新增 Config 开关或 ablation 系统时，更新算法参数和对应优化点，区分默认值、内部常量与实验配置。
- 被替代或收益未证实的方案归入优化地图的“未采用／回滚原因”，保留状态与可复核的失败证据。纯 UI 调整归入控制台，不包装为记忆算法收益。
- 缺陷、待办、风险、待验证行为与验收条件的状态只在[问题台账](docs/known-issues.zh-CN.md)维护，其他文档链接引用。用户对话、截图调整顺序和各轮通过数量不写成架构章节。
- 移动章节后检查章节引用、内部链接、外部深链接和代码位置。公开数字的同步要求见[原则 I](#principle-i)。

### 规则治理

项目规则只在本文件修订，其他入口仅在链接或索引受影响时同步。规则修订提升下方规则版本，并在 PR 描述中用一句话说明理由：MAJOR 表示移除原则或不兼容重定义；MINOR 表示新增原则 / 章节或实质扩展；PATCH 表示不改变含义的澄清、组织与措辞调整。

<a id="architecture"></a>

## 4. 架构概览与代码导航

System-1 / System-2 是职责上的双过程架构，不要求两个操作系统进程。下图是设计概览；当前对象生命周期、功能边界和开关以[全链路架构报告](docs/mnemotrace-full-architecture-report.zh-CN.md)及代码为准，图中结构不代表全部能力已通过验收。

```text
add(messages)
  → System-1：追加无损 Episode、归属标记、轻量嵌入、入整合队列
              关键路径不调用 LLM
  → System-2：抽取 Fact、构建图谱、冲突失效、显著度 / 衰减、分层摘要
  → 类型化记忆：情景 / 语义 / 画像与身份 / 程序性 / 工作记忆
                VectorStore + GraphStore + DocStore；hot / warm / cold 分层方向

search(query)
  → 查询理解与多跳规划 → 多信号检索与时间过滤 → RRF 融合与可选重排
  → 弃答门控 → 事实与原始片段去重、来源标记、token 预算组装
```

### 对象与时间语义

| 对象 | 含义与关键字段 |
| --- | --- |
| Episode | 原始、无损的对话轮次 / 事件；`event_time` 为事件时间，`ingested_at` 为摄入时间 |
| Fact | 原子 `(subject, predicate, object)` 声明；含文本、嵌入、`salience`、`confidence`、来源 episode id 与替代链 |
| Entity / Relation | 图谱节点与边；关系携带双时间戳 |
| Fact 的有效时间 | `valid_at` / `invalid_at`：现实世界中何时为真 |
| Fact 的事务时间 | `created_at` / `expired_at`：系统何时获知 / 撤回 |

双时间概念区分“在现实时间 T 有效”与“在事务时间 K 已知”。不要由这一概念推定 API 已支持独立的 T、K 参数：当前 `Memory.as_of(query, when, ...)` 将单个 `when` 转交给 `search(as_of=when)`，实际过滤语义见 [memory.py](mnemotrace/memory.py)、[types.py](mnemotrace/types.py) 与 [temporal.py](mnemotrace/retrieve/temporal.py)。

### 检索与模块

事实混合检索分别计算语义、BM25、图谱邻近度、时近性和显著度的排名，再通过加权 RRF 融合；它不是直接相加不同量纲的原始分数。实现见 [hybrid.py](mnemotrace/retrieve/hybrid.py) 与 [fusion.py](mnemotrace/retrieve/fusion.py)。之后可选对 top-k 使用交叉编码器重排，默认关闭；详细读取链路见架构报告。

| 位置 | 职责 |
| --- | --- |
| `mnemotrace/__init__.py`、`config.py`、`types.py` | 公开 API、配置和类型化双时间数据 |
| `mnemotrace/memory.py`、`service.py` | Memory 门面；多接入面共享的多租户 MemoryService；弃答门控位于读取路径内 |
| `mnemotrace/ingest/`、`consolidate/` | System-1 写入；System-2 抽取、图谱、冲突、衰减、摘要和分类 |
| `mnemotrace/retrieve/` | 混合检索、BM25、RRF、多跳 / agentic 规划、时间过滤与重排 |
| `mnemotrace/embed/`、`llm/`、`store/` | 可插拔依赖接口与离线实现 |
| `mnemotrace/server/`、`mcp/`、`connectors/` | HTTP API、控制台、OpenAI-memory 兼容接入、MCP、批量导入 |
| `clients/typescript/`、`frontend/`、`deploy/` | JS/TS SDK、管理界面与自托管部署 |
| `eval/`、`tests/`、`examples/` | 统一评测框架及微型合成集、测试和可运行示例 |

后端的已实现与规划状态统一见[后端实现状态](docs/mnemotrace-full-architecture-report.zh-CN.md#backend-status)。
尚未实现的后端必须明确标为规划中，文档与对外文案不得将其描述为当前可用能力。

<a id="evaluation"></a>

## 5. 评测、公开内容与贡献规则

<a id="principle-i"></a>

### I. 可复现性不可妥协

1. **统一框架。** 仓库内只有一套 `eval/` 评测框架：相同的数据摄入、回答提示词和 judge，并声明骨干模型。使用官方 judge 提示和难以投机取巧的强 judge，记录相对基准默认设置的每项偏离；不得把不同评测框架的数字作同条件对比。
2. **完整指标与基线。** 同时报告准确率、总 token 消耗、p50/p95 延迟。每张结果表必须包含同一次运行、相同 answerer + judge 的全上下文基线；未超越时明确说明。平均上下文 token 与总 token 分开标注，检索延迟与端到端回答延迟分开标注。
3. **完整集合与多模型。** 禁止挑选样本，公开数字仅使用全集；运行多个骨干模型（一个小型开放模型 + 一个前沿模型）。探索性切片不作为全集质量结论。
4. **证据与复现。** 公开框架和原始日志，邀请复现。每个公开数字必须追溯到已提交的 `results/*.jsonl`、产生它的精确命令与代码 commit SHA，并可用 `python eval/report.py <file>` 重新推导。运行时若包含未提交改动，必须明确标注，并保存相对于该 commit 的完整 diff，使实际运行代码可恢复。每项 benchmark 声明附复现命令；没有证据就不发布。
5. **完整性校验。** 将运行标为 DONE 或用于论文 / README 证据前，执行 `python eval/validate_results.py --expected-rows <full item count> --require-complete <file>`；LongMemEval_S 全集为 `500`。多系统日志若含未引用的探索性或出错系统，用可重复的 `--system <name>` 标志校验被引用系统，不将整份日志描述为干净。
6. **同步公开入口。** 修改头条数字时，在同一变更中同步 `README.md`、`README.zh-CN.md`、`RESULTS.md`、`demo/index.html` 和 `docs/index.html`；英文与中文内容同步，完成 / 验证中等状态准确。落地页由 `landing/index.html.in` 与 `landing/pages.json` 生成，修改后运行 `python scripts/build_landing_pages.py`，提交源文件与两份输出，并用 `--check` 检查同步；保留两个页面的有意差异。

目标基准顺序：LongMemEval_S → LongMemEval_M → LOCOMO → PersonaMem-v2 → BEAM。

<a id="evidence-artifacts"></a>

### 实验产物与日志提交

- 每次算法变更的每次实验运行，都在 `results/` 保存产物或在 PR 指明已记录的路径；包括 smoke ablation、固定切片、失败的 provider 尝试和完整运行。优先机器可读 JSONL，记录确切命令、数据集 / 切片、系统、日期、指标与状态。
- 失败、中断、部分或探索性运行明确标记 failed / partial / exploratory，不默默丢弃，也不作为公开 benchmark 证明。部分运行、smoke 切片、失败的竞争对手运行、prompt / rerank 重建切片和未校验的跨基准实验，在入库前完成校验或明确记录为探索性产物。
- 结果日志只有明确消费者时才作为证据提交：公开数字，标为 DONE 的论文表格 / 检查清单 / runbook，或测试回归 fixture。规范或发布报告引用的工程发布证据也可保存，但不能作为 benchmark 质量或 accuracy 证据。
- 普通 `.log` 由 `.gitignore` 忽略；除非论文产物明确需要，终端记录保留本地。不同数据集的 schema 和校验命令见[结果目录操作说明](results/README.md)。

<a id="principle-vii"></a>

### VII. 诚实、开放的对外措辞

- 描述 MnemoTrace 自身的能力。对外文案不点名或做 benchmark 对比竞争对手，不包装成“借鉴自某大公司”。
- 不作无已提交 benchmark 日志支撑的声明，例如“1M”“SOTA”“#1”或“历史增长时成本保持不变”。优先描述已验证特性，目标与实测结果明确区分。
- 提交信息平实、具描述性，不添加 `Co-Authored-By: Claude …` 或 `Generated with …` 等 AI 署名 trailer；分支已有此类 trailer 时，合入 `main` 前移除。
- 绝不提交真实个人数据、姓名或密钥。示例使用合成占位符，如 `user123`、`my-app`；保持 `.env` 被 gitignore。发现历史中的 PII 或凭据泄露立即标记，不能把新增一次删除提交当成历史泄露已修复。
- 开放且可自托管，无厂商锁定。采用 AGPL-3.0 开源许可与面向无法满足其源码披露条款的专有 / 闭源或 SaaS 用途的商业许可；刻意采用 copyleft，使衍生作品保持开放。商业使用若不遵守 AGPL，必须获得授权。具体条款以 [LICENSE](LICENSE) 和 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md) 为准。

<a id="reading"></a>

## 6. 按任务读取的文档入口

| 任务 | 先读材料 | 用途 |
| --- | --- | --- |
| 修改算法、检索或数据模型 | [全链路架构报告](docs/mnemotrace-full-architecture-report.zh-CN.md)、[优化地图](docs/architecture-optimization-map.zh-CN.md)、[问题台账](docs/known-issues.zh-CN.md) | 当前实现、取舍证据、未解决边界 |
| 理解算法设计 | [算法架构](docs/algorithm-architecture.md) | 各记忆层和检索机制的详细说明 |
| 运行评测或引用成绩 | [RESULTS.md](RESULTS.md)、[结果目录说明](results/README.md) | 运行设置、版本限制、日志与复现命令 |
| 发布或部署服务 | [部署说明](deploy/README.md)、[交付说明](docs/commercial-release-0.1.0.zh-CN.md) | 环境、命令、升级与交付边界 |
| 使用规格工作流 | [Spec Kit 原则索引](.specify/memory/constitution.md)、对应 `specs/` 计划 | I–VII 对应关系、任务范围与进度 |
| 提交贡献 | [贡献入口](CONTRIBUTING.md) | 开发、验证与落地页操作导航 |

以下受管区块当前指向 0.1.0 单节点自托管商业交付计划，范围是安全、部署与发布，不代表所有任务的最新状态，也不替代本文件的长期规则。后续工具更新指向其他计划时，按该计划的范围和日期理解。

<!-- SPECKIT START -->

当前接入计划：[MnemoTrace 0.1.0 商业交付版](specs/003-commercial-release/plan.md)。

<!-- SPECKIT END -->

## 7. 术语表

| 术语 | 含义 |
| --- | --- |
| System-1 / System-2 | 快速在线写入与较慢异步整合的双过程职责划分 |
| Bi-temporal（双时间） | 有效时间与事务时间；字段见[对象与时间语义](#对象与时间语义) |
| As-of query（时点查询） | 按时间条件查询记忆；两条时间轴与当前 API 边界见[架构概览](#architecture) |
| Salience / decay（显著度 / 衰减） | 按重要性加权保留与强化，未被强化的记忆逐渐淡化 |
| Multi-hop（多跳） | 串联至少两条事实回答问题 |
| Full-context baseline（全上下文基线） | 将全部历史放入提示词的主要准确率比较基线，不是理论上限 |
| The harness（评测框架） | 仓库内统一评测流水线，公开数字的证据来源 |

规则版本：**1.1.1**；首次批准：2026-06-25；最近修订：2026-10-22。此次修订将架构概览中的身份解析澄清为归属标记，与当前 user_id 隔离实现一致，不改变核心原则。
