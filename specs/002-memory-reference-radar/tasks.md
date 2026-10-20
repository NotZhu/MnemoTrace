# 任务清单：记忆系统参考雷达

**Input**: `specs/002-memory-reference-radar/` 中的设计文档

**Prerequisites**: [plan.md](plan.md)、[spec.md](spec.md)、[research.md](research.md)、
[data-model.md](data-model.md)、[contracts/](contracts/)、[quickstart.md](quickstart.md)

**Tests**: 本功能属于文档 / 治理。测试任务使用 [quickstart.md](quickstart.md)、链接检查和契约完整性检查进行验证，而非运行时单元测试。

**Organization**: 按用户故事分组，让每个故事可独立实施和验证。

## 格式：`[ID] [P?] [Story] Description`

- **[P]**：可并行执行，涉及不同文件且无依赖。
- **[Story]**：任务所属用户故事（US1、US2、US3）。
- 描述中包含精确文件路径。

## Phase 1：准备（共享基础设施）

**目的：** 准备 Spec-Kit 功能目录和当前功能指针。

- [x] T001 确认 `.specify/feature.json` 指向 `specs/002-memory-reference-radar`
- [x] T002 根据 Spec-Kit 计划模板创建 `specs/002-memory-reference-radar/plan.md`
- [x] T003 [P] 创建 `specs/002-memory-reference-radar/contracts/radar-entry.md`
- [x] T004 [P] 创建 `specs/002-memory-reference-radar/contracts/adoption-candidate.md`
- [x] T005 [P] 创建 `specs/002-memory-reference-radar/data-model.md`
- [x] T006 [P] 创建 `specs/002-memory-reference-radar/quickstart.md`
- [x] T007 更新 `AGENTS.md` 的 Spec-Kit 托管区块，指向 `specs/002-memory-reference-radar/plan.md`
- [x] T008 更新 `CLAUDE.md` 的 Spec-Kit 托管区块，指向 `specs/002-memory-reference-radar/plan.md`

---

## Phase 2：基础能力（阻塞性前置条件）

**目的：** 定义每个用户故事依赖的治理规则。

**关键约束：** 本阶段完成前，不应推进任何用户故事工作。

- [x] T009 在 `specs/002-memory-reference-radar/research.md` 中添加 Phase 0 决策，覆盖内部使用范围、MnemoTrace 自有候选、原始证据 + 整合记忆、独立实现状态，以及以 benchmark 为优先依据的推进规则
- [x] T010 在 `specs/002-memory-reference-radar/data-model.md` 中定义参考来源、能力模式、采纳候选、证据记录和措辞约束实体
- [x] T011 在 `specs/002-memory-reference-radar/contracts/radar-entry.md` 中定义雷达条目必填字段与验收检查
- [x] T012 在 `specs/002-memory-reference-radar/contracts/adoption-candidate.md` 中定义采纳候选必填字段与推进规则
- [x] T013 在 `specs/002-memory-reference-radar/quickstart.md` 中添加产物存在性、占位符扫描、治理关键词和链接检查命令

**检查点：** 基础已就绪，可以用一致的门禁填充雷达并推进候选。

---

## Phase 3：用户故事 1 — 维护持续更新的架构雷达（优先级：P1）MVP

**目标：** 贡献者打开功能目录后，无需聊天背景即可理解最有价值的记忆、图谱、知识工作区、学习和 benchmark 参考。

**独立测试：** 阅读 `research.md`，在 10 分钟内识别至少五个高优先级参考来源，以及它们的架构信号、证据状态、许可 / 独立实现状态和 MnemoTrace 学习点。

### 用户故事 1 的验证

- [x] T014 [P] [US1] 使用 `specs/002-memory-reference-radar/quickstart.md` 中的链接检查命令，核验 `specs/002-memory-reference-radar/research.md` 的所有公开参考链接
- [x] T015 [P] [US1] 核验 `specs/002-memory-reference-radar/research.md` 中每个来源的证据与独立实现 / 许可状态，符合 `specs/002-memory-reference-radar/contracts/radar-entry.md`

### 用户故事 1 的实现

- [x] T016 [US1] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新“各来源审查状态”表
- [x] T017 [US1] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新面向直接记忆系统的 P0 参考表
- [x] T018 [US1] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新面向算法、图谱和知识编译来源的 P1 参考表
- [x] T019 [US1] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新面向产品接入面与部署模式的 P2 参考表
- [x] T020 [US1] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新“Benchmark 与雷达来源”表
- [x] T021 [US1] 确保 `specs/002-memory-reference-radar/spec.md` 的雷达范围包含知识编译系统

**检查点：** 雷达保持更新、完成分类和链接检查，并标注证据与许可状态后，用户故事 1 完成。

---

## Phase 4：用户故事 2 — 将参考模式转化为 MnemoTrace 自身的能力候选（优先级：P2）

**目标：** 维护者可用雷达将公开模式推进为 MnemoTrace 自有候选，明确范围、受影响模块、战略下注、benchmark 目标和消融计划。

**独立测试：** 选择任意首选模式，核验其采纳候选形式明确指出 MnemoTrace 模块、benchmark 类别、准确率 / token 消耗 / 延迟假设、独立实现边界及回滚标准。

### 用户故事 2 的验证

- [x] T022 [P] [US2] 核验 `specs/002-memory-reference-radar/research.md` 的能力模式表中每一行，至少映射到 `specs/002-memory-reference-radar/plan.md` 中的一项战略下注
- [x] T023 [P] [US2] 核验 `specs/002-memory-reference-radar/research.md` 初始优先队列中的每个条目，均能满足 `specs/002-memory-reference-radar/contracts/adoption-candidate.md`

### 用户故事 2 的实现

- [x] T024 [US2] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新“待吸收的能力模式”表
- [x] T025 [US2] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新“分阶段吸收路径”
- [x] T026 [US2] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新“候选推进检查清单”
- [x] T027 [US2] 在 `specs/002-memory-reference-radar/research.md` 中添加或更新初始优先队列，包含链感知检索、原始证据融合加强、派生记忆层、记忆工作区诊断、图邻近检索、反思 / 经验记忆和运行配置
- [x] T028 [US2] 在 `specs/002-memory-reference-radar/contracts/adoption-candidate.md` 中，将链感知检索作为首个可进入计划阶段的候选示例
- [x] T029 [US2] 确保 `specs/002-memory-reference-radar/plan.md` 列明首批可进入计划阶段的候选及其高层评测门禁

**检查点：** 每个优先候选都能在不依赖外部代码或无依据 benchmark 声明的情况下进入后续 Spec-Kit 功能后，用户故事 2 完成。

---

## Phase 5：用户故事 3 — 积极学习，同时保持对外定位诚实（优先级：P3）

**目标：** 贡献者可在内部使用雷达，同时让 README、落地页和 benchmark 文案绑定 MnemoTrace 自有、可复现的证据。

**独立测试：** 审阅拟议公开措辞，确认没有未经复现的竞争对手比较、隐瞒借用的声明，或无 benchmark 支撑的“世界最佳”表述。

### 用户故事 3 的验证

- [x] T030 [P] [US3] 核验 `specs/002-memory-reference-radar/research.md` 将第三方声明标为仅外部声明（external-only）
- [x] T031 [P] [US3] 核验 `specs/002-memory-reference-radar/spec.md` 的成功标准禁止未经复现的竞争对手比较，以及无 benchmark 支撑的“SOTA”“#1”或“世界最佳”声明

### 用户故事 3 的实现

- [x] T032 [US3] 在 `specs/002-memory-reference-radar/data-model.md` 中添加或更新措辞约束规则
- [x] T033 [US3] 在 `specs/002-memory-reference-radar/contracts/radar-entry.md` 中添加对外措辞验收检查
- [x] T034 [US3] 在 `specs/002-memory-reference-radar/contracts/adoption-candidate.md` 中添加对外措辞影响要求
- [x] T035 [US3] 确保 `specs/002-memory-reference-radar/plan.md` 的宪章检查为可复现性和诚实对外措辞记录 PASS 状态

**检查点：** 雷达可用于内部学习，且不削弱 MnemoTrace 的可复现性和措辞纪律时，用户故事 3 完成。

---

## Phase 6：完善与跨模块事项

**目的：** 验证格式、一致性，以及是否可进入下游 `/speckit-analyze` 或具体功能的实施规格。

- [x] T036 [P] 按 `specs/002-memory-reference-radar/quickstart.md` 的说明，对 `specs/002-memory-reference-radar/spec.md`、`specs/002-memory-reference-radar/plan.md`、`specs/002-memory-reference-radar/research.md`、`specs/002-memory-reference-radar/data-model.md` 和 `specs/002-memory-reference-radar/contracts/` 执行占位符扫描
- [x] T037 [P] 按 `specs/002-memory-reference-radar/quickstart.md` 的说明，对 `specs/002-memory-reference-radar/` 执行治理关键词检查
- [x] T038 [P] 运行 `git diff --check -- AGENTS.md CLAUDE.md .specify/feature.json specs/002-memory-reference-radar`
- [x] T039 确认 `AGENTS.md` 与 `CLAUDE.md` 的 Spec-Kit 托管区块一致，指向 `specs/002-memory-reference-radar/plan.md`
- [x] T040 最终验证后更新 `specs/002-memory-reference-radar/checklists/requirements.md` 的说明
- [x] T041 审阅 `specs/002-memory-reference-radar/tasks.md` 的严格清单格式、连续 ID、故事标签和精确文件路径
- [x] T042 [P] 审阅 `specs/002-memory-reference-radar/technical-report.zh-CN.md`，确认内部文档中文优先、遵循可复现纪律和独立实现边界，并与 `specs/002-memory-reference-radar/research.md` 一致

---

## 依赖与执行顺序

### 阶段依赖

- **准备（Phase 1）：** 无依赖。
- **基础能力（Phase 2）：** 依赖准备阶段完成，阻塞全部用户故事。
- **用户故事 1（Phase 3）：** 依赖基础能力，构成 MVP。
- **用户故事 2（Phase 4）：** 依赖基础能力，US1 建立来源分类后可继续推进。
- **用户故事 3（Phase 5）：** 依赖基础能力，措辞相关实体存在后可与 US1/US2 并行。
- **完善（Phase 6）：** 依赖所选用户故事完成。

### 用户故事依赖

- **US1：** Phase 2 后可独立推进，产出持续更新的雷达。
- **US2：** Phase 2 后可独立推进，但受益于 US1 的来源覆盖。
- **US3：** Phase 2 后可独立推进，验证对外纪律。

### 可并行事项

- T003-T006 创建不同文件，可并行。
- T014 与 T015 验证 `research.md` 的不同方面，可并行。
- T022 与 T023 验证不同候选门禁，可并行。
- T030 与 T031 检查不同文件，可并行。
- T036-T038 是独立验证命令，可并行。

---

## 并行示例：用户故事 1

```text
Task: "使用 specs/002-memory-reference-radar/quickstart.md 中的链接检查命令，核验 specs/002-memory-reference-radar/research.md 的所有公开参考链接"
Task: "核验 specs/002-memory-reference-radar/research.md 中每个来源的证据与独立实现 / 许可状态，符合 specs/002-memory-reference-radar/contracts/radar-entry.md"
```

---

## 并行示例：用户故事 2

```text
Task: "核验 specs/002-memory-reference-radar/research.md 能力模式表中的每一行，至少映射到 specs/002-memory-reference-radar/plan.md 的一项战略下注"
Task: "核验 specs/002-memory-reference-radar/research.md 初始优先队列中的每个条目，均能满足 specs/002-memory-reference-radar/contracts/adoption-candidate.md"
```

---

## 实施策略

### MVP 优先（仅用户故事 1）

1. 完成 Phase 1 准备。
2. 完成 Phase 2 基础契约与治理规则。
3. 完成 Phase 3 架构雷达。
4. 通过链接和雷达条目契约检查，独立验证 US1。

### 增量交付

1. 交付 US1，让贡献者获得持续更新、已分类的雷达。
2. 交付 US2，让雷达模式转为可进入计划阶段的 MnemoTrace 候选。
3. 交付 US3，让对外措辞继续绑定可复现的 MnemoTrace 证据。
4. 进入 `/speckit-analyze` 或具体功能的实施规格前，执行 Phase 6 验证。

### 后续实施规格

本功能验证后，为以下事项分别创建实施规格：

1. 基于 `supersedes` 演化链的链感知检索。
2. 上下文组装中的原始证据融合加强。
3. 使用轻量 n 跳 / PPR 思路扩展的图邻近检索。

这些规格刻意与雷达功能分开，使算法变更可独立进行 benchmark、消融和回滚。
