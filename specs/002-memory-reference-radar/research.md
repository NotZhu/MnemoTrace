# 记忆系统参考雷达

**Feature**: `002-memory-reference-radar`

**Created**: 2026-06-29

**Purpose**: 记录 MnemoTrace 应在架构层面研究的公开系统与算法，将有用模式转化为 MnemoTrace 自身、可复现的工作。
这是内部研究输入；MnemoTrace 的公开声明仍需自有日志，以及准确率 / token 消耗 / 延迟（accuracy/tokens/latency）三项指标。

## Phase 0 决策

### 决策：将雷达用于内部架构治理，而非对外定位

**理由：** MnemoTrace 的可信度来自可复现结果与自主实现。雷达应帮助维护者快速学习，
同时防止无依据的公开比较或照搬声明。

**考虑过的备选：**

- 公开竞争对手矩阵：除非所有数字都已复现，否则容易引入过时比较，并违反项目措辞纪律，因此拒绝。
- 仅保留私下聊天笔记：架构决策需要稳定、可评审的产物，因此拒绝。

### 决策：只通过 MnemoTrace 自有采纳候选推进模式

**理由：** 有用的工作单位应是映射到 MnemoTrace 记忆模块、战略下注、评测假设和回滚标准的候选，而非“复制项目 X”。

**考虑过的备选：**

- 直接从外部系统引入功能：可能造成许可混淆、隐性耦合，并引入不适合 MnemoTrace 双时间模型的功能，因此拒绝。
- 只做文献综述，不维护候选待办：不能推进路线图，因此拒绝。

### 决策：以原始证据 + 整合记忆作为读取路径默认前提

**理由：** MnemoTrace 的 M1 发现表明，仅靠事实问答会损失召回。
除非后续 benchmark 证明更窄的路径更好，否则每个候选必须保留原始会话片段、事实与来源。

**考虑过的备选：**

- 仅事实记忆：丢失细节，与项目现有证据矛盾，因此拒绝。
- 仅原始记忆：缺少冲突处理、时间推理和多跳结构，因此拒绝。

### 决策：每个参考条目均记录独立实现与许可状态

**理由：** MnemoTrace 采用 AGPL + 商业双授权。可以研究架构思想，
但代码、schema、提示词和 benchmark 产物未经明确审查不得复用。

**考虑过的备选：**

- 之后再审查许可：候选推进时就需要知道来源是否仅限架构研究，因此拒绝。
- 完全排除 copyleft 项目：许多思想仍可在架构层面研究，因此拒绝。

### 决策：按 benchmark 类别与战略下注确定候选优先级

**理由：** 目标是在可复现评测下取得算法优势。无法指出目标行为、benchmark 类别和准确率 / token 消耗 / 延迟变化方向的候选，继续保留为仅研究。

**考虑过的备选：**

- 按生态流行度排序：流行不代表记忆质量，因此拒绝。
- 按实现难度排序：目标是构建最强的记忆引擎，而非完成最容易的清单，因此拒绝。

## 工作规则

- 积极研究公开思想；未经明确许可审查和独立实现（clean-room）计划，不复制代码。
- 第三方 benchmark 数字是线索，不是证据。只有在 `eval/` 中复现并提交 `results/*.jsonl` 日志后，才成为 MnemoTrace 证据。
- 将每个借鉴模式转化为 MnemoTrace 自身原语：无损 episode、原子 fact、双时间图谱、来源、supersedes 链、原始片段 + 事实混合检索、显著度、异步整合，以及由评测框架支撑的消融。
- 不将本雷达变为公开的竞争对手定位材料；公开文案应描述 MnemoTrace 自身能力和可复现结果。

## 各来源审查状态

所有条目起初均为仅研究架构的输入。“许可待审”意味着在后续计划审查来源许可并记录独立实现方案前，
不得复用任何代码、提示词、schema 或 benchmark 产物。

| 来源 | 证据状态 | 独立实现 / 许可状态 |
| --- | --- | --- |
| Hy-Memory / Tencent Hunyuan Memory | 仅外部声明，未在 MnemoTrace 评测框架复现 | 仅研究架构；许可 / 源码可用性待确认 |
| Mem0 | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Graphiti / Zep | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Letta / MemGPT | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| LangMem | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Cognee | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| LLM Wiki | 公开仓库与文档；仅外部声明 | GPLv3；仅研究架构并独立实现，除非后续计划接受许可影响 |
| Supermemory | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Hindsight | 候选公开仓库；仅外部声明 | 需审查许可并核验仓库 |
| MemPalace | 候选公开仓库；仅外部声明 | 需审查许可并核验仓库 |
| SimpleMem | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| A-Mem | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| HippoRAG | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| LightRAG | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Microsoft GraphRAG | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| RAPTOR | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| Generative Agents | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| Reflexion | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| ExpeL | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| MemoryBank | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| MemOS | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| MemoryOS | 候选公开仓库 / 论文；仅外部声明 | 许可审查前仅研究架构 |
| memU | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| Memobase | 公开仓库与文档；仅外部声明 | 复用任何代码前需审查许可 |
| agentmemory | 候选公开仓库；仅外部声明 | 需审查许可并核验仓库 |
| LongMemEval-V2 | 公开 benchmark 页面；仅外部声明 | 复用前需审查 benchmark 条款与数据集许可 |
| BEAM | 公开 benchmark 仓库；仅外部声明 | 复用 benchmark 产物前需审查许可 |
| LOCOMO | 公开 benchmark 页面与数据集；仅外部声明 | 复用前需审查数据集条款与许可 |
| Agent Memory Benchmark | 公开排行榜；仅外部声明 | 仅研究，需审查排行榜条款 |
| mem0 memory-benchmarks | 公开 benchmark 仓库；仅外部声明 | 许可与评测框架审查前仅研究架构 |
| Awesome Agent Memory | 公开文献雷达；仅外部声明 | 仅研究架构；所链接来源需分别审查许可 |
| Awesome GraphMemory | 公开图记忆雷达；仅外部声明 | 仅研究架构；所链接来源需分别审查许可 |
| Awesome Memory for Agents | 公开文献雷达；仅外部声明 | 仅研究架构；所链接来源需分别审查许可 |

## P0 参考：优先研究

| 来源 | 类别 | 架构信号 | MnemoTrace 学习点 | 采纳候选 |
| --- | --- | --- | --- | --- |
| [Hy-Memory](https://hy-memory.com/) / [Tencent Hunyuan Memory](https://memory.hunyuan.tencent.com/) | 分层记忆产品 / 研究系统 | 六层记忆、System-1/System-2 划分、演化链、OpenClaw 插件定位 | 印证 MnemoTrace 在分层记忆、异步整合、画像 / 心智模型层及 supersedes 链上的方向 | 链感知检索；会话摘要、心智模型和意图模型等派生层；运行配置 |
| [Mem0](https://github.com/mem0ai/mem0) | 通用 AI 记忆层 | 产品级 API、用户 / 会话 / 智能体记忆、多信号检索、OpenMemory/MCP 生态 | 研究 API 易用性、实体链接、仅追加式提取变体和 benchmark 组织方式 | 改进记忆服务 API；实体关联检索加权；OpenAI/MCP 兼容接入面 |
| [Graphiti](https://github.com/getzep/graphiti) / Zep | 时间知识图谱 | 面向智能体的实时时间知识图谱，从 episode 构建实体 / 关系，时间更新 | 时间图谱读取和便于更新的图谱构建的重要参考 | 双时间图谱检索；时间路径扩展；图谱写入契约 |
| [Letta](https://github.com/letta-ai/letta) / MemGPT | 有状态智能体平台 | 核心 / 归档 / 召回记忆，智能体可见的记忆编辑，有状态智能体循环 | 将记忆视为智能体可控制的能力，而不只是检索 | 程序性记忆控制；带来源与审计的记忆编辑操作 |
| [LangMem](https://github.com/langchain-ai/langmem) | 智能体记忆原语 | 与智能体图集成的语义、情景和程序性记忆原语 | 记忆类型与智能体工作流集成的术语参考 | 类型化记忆 API 命名，以及图式智能体集成模式 |
| [Cognee](https://github.com/topoteretes/cognee) | 知识图谱记忆平台 | 自托管知识图谱记忆、摄入流水线、面向 Kuzu 的图存储、MCP 接入面 | 研究图谱摄入形态与自托管部署易用性 | 图后端评估；知识图谱摄入流水线比较 |
| [Supermemory](https://github.com/supermemoryai/supermemory) | 上下文与记忆引擎 | 快速的本地 / 托管上下文引擎、应用 / API 封装、跨应用记忆 | 加强对上下文引擎产品接入面和原始证据召回的认识 | 上下文组装 API；本地优先的服务封装 |
| [Hindsight](https://github.com/vectorize-io/hindsight) | 面向智能体的学习型记忆 | “会学习的智能体记忆”定位，反馈驱动的记忆改进 | 从行为改善，而不仅是召回的角度理解记忆 | 反馈到记忆的闭环；从失败派生的记忆候选 |
| [MemPalace](https://github.com/MemPalace/mempalace) | 开放记忆系统 | 重视经过 benchmark 验证的记忆与原始内容召回接口 | 印证 MnemoTrace 的 M1 发现：原始证据检索必须保持一等地位 | 原文证据保留；片段来源评分 |

## P1 参考：算法与架构原语

| 来源 | 类别 | 架构信号 | MnemoTrace 学习点 | 采纳候选 |
| --- | --- | --- | --- | --- |
| [SimpleMem](https://github.com/aiming-lab/SimpleMem) | 终身记忆压缩 | 语义无损压缩、多模态记忆、自演化变体 | 研究递归整合与查询感知的压缩记忆单元 | 休眠期语义压缩；摘要 / 事实 / 片段融合消融 |
| [A-Mem](https://github.com/agiresearch/a-mem) | 自主式记忆 | 受笔记网络启发、动态且互联的记忆组织方式 | 将记忆视为相互连接单元组成的演化图，而非静态 top-k 记录 | 记忆链接演化；从已检索单元进行关联扩展 |
| [HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG) | 图 RAG | 知识图谱 + 个性化 PageRank，用于多跳检索 | 图邻近评分与多跳实体扩展的重要参考 | 图邻近检索器；PPR 式候选扩展 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | 轻量 GraphRAG | 简单快速的图谱 + 向量检索 | 无需重型全局索引的精简图检索基线 | 轻量图谱 / BM25 / 向量融合基线 |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | GraphRAG 流水线 | 实体 / 关系提取、社区摘要、局部 / 全局查询模式 | 分层图谱摘要，以及全局 / 局部读取模式的参考 | 社区摘要；全局 / 局部 / 混合图查询模式 |
| [LLM Wiki](https://github.com/nashsu/llm_wiki/blob/main/README_CN.md) | 知识编译工作区 | 不可变来源 → 生成的 Wiki → schema / 用途；两步摄入、有来源支撑的页面、四信号图关联、Louvain 社区、图谱洞察、混合检索 | 研究便于人阅读的记忆工作区、用途引导的整合、可追溯来源的派生页、图谱健康诊断及图扩展检索 | 记忆工作区导出；用途感知整合；稀疏 / 桥接记忆的图谱诊断 |
| [RAPTOR](https://github.com/parthsarthi03/raptor) | 分层检索 | 递归抽象、树形组织的检索 | 超越会话摘要的分层抽象参考 | episode 聚类摘要树；按查询选择抽象层级 |
| [Generative Agents](https://github.com/joonspk-research/generative_agents) | 记忆流与反思 | 时近性、相关性、重要性、反思、规划 | 显著度与反思权衡的经典基础 | 反思队列；显著度 / 相关性 / 时近性校准 |
| [Reflexion](https://github.com/noahshinn/reflexion) | 语言强化记忆 | 将失败反馈存为可复用的文字经验 | 程序性记忆与智能体自我纠错的参考 | 失败到程序性记忆的转换；任务重试反思 |
| [ExpeL](https://github.com/LeapLabTHU/ExpeL) | 经验学习 | 从先前任务提取经验，在新任务中召回 | 跨任务程序性与技能记忆的参考 | 经验提取流水线；技能 / 经验检索 |
| [MemoryBank](https://github.com/zhongwanjun/MemoryBank-SiliconFriend) | 长期陪伴记忆 | 用户 / 画像适应与长期对话记忆 | 画像持久化与用户专属记忆的早期参考 | 画像记忆更新与衰减基线 |

## P2 参考：产品接入面与部署模式

| 来源 | 类别 | 架构信号 | MnemoTrace 学习点 | 采纳候选 |
| --- | --- | --- | --- | --- |
| [MemOS](https://github.com/MemTensor/MemOS) | 记忆操作系统 | 统一存储 / 检索 / 管理、记忆立方体、异步调度、反馈纠正、本地插件 | 多租户记忆资源的产品抽象参考 | 记忆命名空间 / 立方体模型；反馈纠正体验 |
| [MemoryOS](https://github.com/BAI-LAB/MemoryOS) | 个性化记忆操作系统 | 为个性化智能体组织短期 / 中期 / 长期记忆 | 研究记忆生命周期和个性化边界 | 画像 / 情景 / 程序性记忆的生命周期策略 |
| [memU](https://github.com/NevaMind-AI/memU) | 智能体个人记忆 | 快速检索、自演化技能、低成本个人记忆 | 研究本地个人记忆易用性与技能演化 | 个人记忆配置；技能记忆接入面 |
| [Memobase](https://github.com/memodb-io/memobase) | 画像记忆 | 面向聊天应用、基于用户画像的记忆 | 画像提取 / 更新接口的专门参考 | 画像 / 身份 schema 与更新策略 |
| [agentmemory](https://github.com/rohitg00/agentmemory) | 编程智能体记忆服务器 | MCP 优先，围绕生命周期 / 置信度 / 知识图谱 / 混合搜索组织能力 | 研究编程智能体安装流程、MCP 工具和仓库记忆体验 | MCP server 易用性；编程智能体记忆工作流 |

## Benchmark 与雷达来源

| 来源 | 角色 | MnemoTrace 应如何使用 |
| --- | --- | --- |
| [LongMemEval-V2](https://xiaowu0162.github.io/longmemeval-v2/) | 长历史智能体记忆 benchmark | 作为未来规模目标跟踪；可用后仅通过仓库内评测框架复现 |
| [BEAM](https://github.com/mohammadtavakoli78/BEAM) | 百万 token 长期记忆 benchmark | M2/M3 读取路径就绪后，用于规模与干扰鲁棒性评估 |
| [LOCOMO](https://snap-research.github.io/locomo/) | 长对话记忆 benchmark | 用于跨会话对话和画像记忆评测 |
| [Agent Memory Benchmark](https://agentmemorybenchmark.ai/) | 外部排行榜 / 雷达 | 作为生态线索，不将其结果导入为 MnemoTrace 声明 |
| [mem0 memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) | Benchmark 接口参考 | 研究评测框架形态与可复现性缺口，保持 MnemoTrace 框架中立 |
| [Awesome Agent Memory](https://github.com/TeleAI-UAGI/Awesome-Agent-Memory) | 文献雷达 | 定期扫描新系统和论文的来源 |
| [Awesome GraphMemory](https://github.com/DEEP-PolyU/Awesome-GraphMemory) | 图记忆雷达 | 定期扫描基于图的记忆技术的来源 |
| [Awesome Memory for Agents](https://github.com/TsinghuaC3I/Awesome-Memory-for-Agents) | 智能体记忆雷达 | 定期扫描记忆智能体新论文的来源 |

## 待吸收的能力模式

| 模式 | 当前主要参考 | MnemoTrace 自身形式 | 评测假设 |
| --- | --- | --- | --- |
| 将原始证据作为一等记忆 | MemPalace, Supermemory, Mem0, agentmemory | 每条问答读取路径都保留检索到的原始会话片段，与事实和图谱证据共同使用 | 改善细节召回，减少 LongMemEval/LOCOMO 上仅事实问答的漏答 |
| 演化链检索 | Hy-Memory, Graphiti, MemOS | 事实与画像记录暴露并检索带来源和有效时间过滤的 `supersedes` 链 | 改善知识更新和时间问题，不增加提示词噪声 |
| 时间图谱扩展 | Graphiti, HippoRAG, LightRAG, GraphRAG | 以查询实体为种子，在双时间关系上进行 n 跳 / PPR 式图扩展 | 改善多跳 / 跨会话类别，同时使 token 预算低于全上下文 |
| 分层整合 | RAPTOR, GraphRAG, SimpleMem, Hy-Memory | 会话摘要、画像摘要、心智模型和社区摘要均为带来源链接的派生产物 | 降低 token 消耗，同时保留长历史中可回答问题的证据 |
| 知识工作区与诊断 | LLM Wiki, GraphRAG, Cognee | 可选的、便于人阅读的记忆工作区，包含用途、有来源支撑的派生页、图社区、稀疏区域、桥接节点和缺失链接诊断 | 改善可维护性、可审计性与整合质量，不把工作区页面当成不可追溯的事实来源 |
| 智能体反思与经验记忆 | Generative Agents, Reflexion, ExpeL, A-Mem, Hindsight | 从失败、重复任务和高显著度 episode 中形成程序性经验与关联记忆更新 | 改善重复任务行为和程序性召回，不污染事实记忆 |
| 运行配置 | Hy-Memory, MemOS, Mem0, Supermemory | 提供 `lite`、`standard`、`graph`、`consolidated` 等模式，分别测量权衡 | 用户可选择延迟 / 成本 / 准确率权衡，评测框架可公平比较消融 |
| 记忆反馈 / 纠正 | MemOS, memU, Letta, Mem0 | 用户或智能体纠正通过来源与失效处理，非破坏性地更新记忆 | 改善纠正处理，同时保留可审计性 |
| 画像 / 身份记忆 | Memobase, MemoryBank, Mem0, Hy-Memory | 身份 / 画像记录作为独立类型化记忆，具有更新、冲突与衰减规则 | 改善个性化问题，不将偏好事实混入原始情景召回 |

## 分阶段吸收路径

### Stage 0 — 治理与雷达

- 每项重要记忆功能开始前，更新并审阅本雷达。
- 每个获准推进的候选都要求来源链接、许可状态、独立实现说明及评测假设。
- 不直接依据雷达实施；先将候选推进为正常 Spec-Kit 计划。

### Stage 1 — 证据优先的读取路径

- 将原始证据检索与片段来源提升为不可妥协的读取路径不变量。
- 为事实 / 画像记录加入链感知检索，让更新可解释。
- 目标 benchmark 行为：细节召回、知识更新、当前与过去的时间问题。

### Stage 2 — 派生记忆层

- 添加或正式定义会话摘要、画像摘要、心智模型和意图 / 程序层，作为派生产物。
- 每个派生产物必须回指原始 episode / fact；任何摘要都不能成为不可追溯的事实来源。
- 研究 LLM Wiki 式用途文件和有来源支撑的工作区页面，将其作为 MnemoTrace 记忆之上可选、便于人阅读的视图，不替代类型化记忆。
- 目标 benchmark 行为：长会话压缩、画像一致性、重复偏好召回。

### Stage 3 — 图多跳检索

- 添加带受控 n 跳扩展和时间过滤的图邻近检索。
- 加入较重的社区摘要机制之前，先测试轻量图谱 / 向量 / BM25 融合。
- 目标 benchmark 行为：多跳、跨会话、实体链问题。

### Stage 4 — 反思与经验记忆

- 证据足够充分时，将失败、重复操作和被强化的事实转为程序性经验。
- 程序性经验与事实声明分开，并要求来源。
- 目标 benchmark 行为：智能体任务改善和重复工作流一致性。

### Stage 5 — 运行配置与成绩记录

- 定义用户可选择的配置，并测量准确率 / token 消耗 / 延迟权衡。
- 仅发布带原始日志和全上下文基线的 MnemoTrace 自有数字。
- 目标结果：可信、开放的成绩记录，加上可本地运行、检查和复现的记忆引擎。

## 候选推进检查清单

- 来源公开，链接已记录。
- 许可已审查，或候选仅研究架构并采用独立实现。
- 至少映射到一项 MnemoTrace 战略下注。
- 保留原始证据 + 事实检索。
- 涉及事实时，保留双时间来源与非破坏性失效处理。
- 有 benchmark 类别及准确率 / token 消耗 / 延迟假设。
- 有消融计划与回滚标准。
- 对外措辞影响为无影响，或与 MnemoTrace 自有结果绑定。

## 初始优先队列（Initial Priority Queue）

1. **链感知检索：** 检索事实 / 画像记录时，可选在 token 预算内附带其 `supersedes` 链与来源。
2. **原始证据融合加强：** 在上下文组装中，明确区分原始片段、事实与图路径三类证据。
3. **派生记忆层：** 将会话摘要、画像摘要、心智模型和意图或程序记录定义为类型化派生产物。
4. **记忆工作区诊断：** 为维护者定义可选的有来源支撑页面、用途背景、稀疏区域警告、桥接节点与缺失链接建议。
5. **图邻近检索器：** 在图存储接口之后添加轻量、借鉴 n 跳 / PPR 思路的检索组件。
6. **反思 / 经验记忆：** 为失败、重复任务和用户纠正建立程序性记忆路径。
7. **运行配置：** 定义经过测量、权衡延迟、token 消耗与召回的模式。

## 留待后续计划回答的问题

- 链感知检索的首个门禁应使用哪个 benchmark 子集：LongMemEval 知识更新、LOCOMO 时间问题，还是小型合成更新集？
- 心智模型和意图模型应是独立类型化实体，还是采用更严格 schema 的派生画像记录？
- 不增加重型依赖、不违反零配置不变量的前提下，能改善多跳召回的最小图扩展算法是什么？
- 便于人阅读的工作区应生成 Markdown 页面、JSON 产物，还是两者都生成？如何保证这些页面不会成为不可追溯的权威记录？
- 如何表示用户纠正，使其能失效化先前声明，又不把每次纠正都变成高显著度的永久事实？
