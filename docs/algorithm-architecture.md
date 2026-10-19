# MnemoTrace 算法架构

中文全链路架构报告见
[`docs/mnemotrace-full-architecture-report.zh-CN.md`](mnemotrace-full-architecture-report.zh-CN.md)。中文架构优化地图见
[`docs/architecture-optimization-map.zh-CN.md`](architecture-optimization-map.zh-CN.md)。前者说明完整数据流、流程、
模块边界和验收入口；后者记录 AI/人类每次算法改动落在架构的哪个模块、优化了什么、验收日志在哪里。

MnemoTrace 的算法赌注是架构性的，而非单一的检索器技巧。系统的构建使得每一记忆层贡献不同类型的证据，且每个声明都可在同一评测框架中核查。

## 北极星目标

读路径应当通过移除干扰项来胜过全上下文，同时保留回答所需的 fact、原始明细、时间顺序与溯源。成本与延迟固然重要，但首要测试是在困难的多会话、多跳与知识更新问题上的准确率。

## 平面

1. 热摄入平面
   - 以事件时间与事务时间追加无损 episode。
   - 尽早解析身份，使第一人称 fact 跨会话落到同一主体。
   - 默认保持关键写路径确定性且无需 LLM。
   - 第二个入口 `mnemotrace/connectors/watch.py` 批量摄入磁盘上已有的 agent 转录（Claude Code、Codex）：解析、丢弃工具噪声、遮蔽密钥、以 episode 存储，然后关闭会话以便蒸馏。基于内容指纹幂等；可调度（`--install`）。

2. 睡眠期整合平面
   - 抽取原子 fact。
   - 构建双时间轴图。
   - 先检测低成本冲突，再以非破坏方式废止旧 fact。
   - 为显著度打分，并让未强化 incidental 记忆衰减。
   - 在会话关闭时，用一次 LLM 调用（`mnemotrace/consolidate/outcomes.py`）将整个会话蒸馏为 decision / finding / lesson / open_question fact。逐轮抽取产出属性；一次会话产出结论。它们是普通 fact（subject = session id），因此继承 supersession、溯源与检索，且不进入实体图。

3. 类型化记忆平面
   - Episodic memory 保留原始明细。
   - Semantic memory 存储 fact 与图证据。
   - Profile 与 identity memory 提供稳定的用户级状态。
   - Working memory 保持会话作用域，绝不悄然变为持久。
   - 会话结论是 working session 的记忆单元，与属性一并保留。

4. 证据检索平面
   - 检索前先理解问题形态：lookup、current-state、temporal、aggregation、preference、multi-hop 或拒答敏感。
   - 从稠密向量、BM25 词法证据、图 n-hop 证据、近因与显著度并行检索。
   - 仅融合来自语义、词法与图信号的正向证据。近因与显著度是先验，而非证据。零词法或图得分不得获得伪造的 RRF 贡献。
   - 在 fact 之后添加原始 chunk，因为 fact 提供结构，而 chunk 找回抽取可能丢失的明细。

5. 评测反馈平面
   - 每个公开数字都必须来自已提交的 JSONL 日志。
   - 每个表格都同时报告准确率、token 与 p50/p95 延迟。
   - 每次基准比较都包含全上下文基线。
   - 在加入路由前，跨日志比较在共享已评分 qid 上衡量骨干互补性。

## 读路径契约

对于给定的查询 `q`，MnemoTrace 按以下顺序组装上下文：

1. 对 `q` 分类证据需求；
2. 按需展开多跳子查询，包括"my colleague's company"或"where does my sister live"之类的桥接实体问题；
3. 用 as-of 过滤检索 live fact；
4. 对单值谓词应用 slot-head 过滤；
5. 融合正向证据信号与先验；
6. 仅在热检索未命中时回分页 cold fact；
7. 依据证据预案添加图路径、current-state 表、supersession 历史、时间线或偏好记录；
8. 在 token 预算内添加摘要与原始 chunk；
9. 在最终上下文中保留溯源与日期；
10. 当记忆未包含足够证据时拒答。

## 架构不变量

- 矛盾时不硬删除。被取代的 fact 仍可追溯审计。
- 零配置正确性不依赖 LLM。LLM 可改进抽取或评分，但引擎仍必须离线确定性运行。
- 不容许会损坏的回退。零依赖回退可能降级，但绝不悄然产生错误记忆：没有 LLM 时，agent 转录会被存储但不做规则抽取；当哈希嵌入器无法对 namespace 持有内容做 tokenize 时，审计会如实说明并指明迁移。
- 一次 store 级删除，三重防护。`clear-slot` 是写侧唯一的硬删除；它需要所有者批准的计数，仅触及 live fact，且绝不移除所有者键入的内容。
- 不容许纯 fact 问答。默认读路径将整合后的 fact 与原始会话 chunk 结合。
- 不容许历史遗忘。历史值与更新问题应呈现 supersession 链，而非仅当前的 live slot head。
- 不容许先验泄漏为证据。近因与显著度可打破平局，但无法将零证据的命中变成相关 fact。
- 不容许仅用于基准的分支。查询规划依据证据形态，而非数据集标签。
- 未经原始日志与验证，不容许公开声明。

## 承重参数

评测框架证明承重的融合权重、阈值与预算，连同各自所属的算法，已列表于
[`architecture-optimization-map.zh-CN.md` § 算法流程与承重参数](architecture-optimization-map.zh-CN.md)。
该表为事实来源；本文档陈述契约。

## 后续改进方向

- 面向含多个桥接实体或嵌套约束的关系链，更广泛的确定性多跳规划。
- 针对显式 invalid_at 跨度与持续时间证据，更好的时间间隔推理。
- 按证据形态学习或由评测框架调优的融合权重，配合固定的验证切分。
- 面向约束、目标与过程记忆的、向后兼容的更丰富 fact 谓词。
- 当记忆增长到超出模型上下文窗口时仍能保持检索的 cold 层索引。
