# 实施计划：持久化存储后端

> 原规格中的用户编号关联状态与旧 pickle 转换支持，已由[接口退役规格](../006-retire-identity-linking/spec.md)替代；其余历史设计保留。

**Branch**: `001-durable-persistence` | **Date**: 2026-06-25 | **Spec**: [spec.md](./spec.md)

**Input**: 功能规格 `specs/001-durable-persistence/spec.md`

## 概要

将整库 **pickle** 快照（`mnemotrace/memory.py:178-223` 中的 `Memory.save`/`Memory.open`）
替换为**安全、带 schema 版本、可承受崩溃的磁盘格式**：每个命名空间使用一个目录，包含 JSON Lines
记录文件和 `manifest.json`。同时，在现有 `mnemotrace/store/base.py` 接口之后加入 **LanceDB**，
作为首个真实、持久的 `VectorStore`。内存存储（`mnemotrace/store/memory_store.py`）仍为零配置默认实现
和正确性参照；持久化格式与 LanceDB 均为可选、通过配置选择的后端。通过一次性 `pickle → JSONL` 迁移保护现有用户。

两个层次均位于已有接口之后：

1. **持久快照格式**：`mnemotrace/store/persist.py` 中的序列化器，将各实体类型流式写入各自仅追加的 `.jsonl`，并用 `manifest.json`（schema 版本、embedder id、维度、数量）控制加载。这一层负责替换 pickle（US1）。
2. **持久向量后端**：`mnemotrace/store/lancedb_store.py` 中的 `LanceDBVectorStore` 实现 `VectorStore` 抽象基类，延迟导入 `lancedb`，让向量持久保存，并扩展到 RAM 之外（US2）。

## 技术背景（Technical Context）

**Language/Version**: Python ≥ 3.10；`mnemotrace/` 核心仅使用标准库（宪章 III）。

**Primary Dependencies**: US1 仅用标准库（`json`、`os`、`tempfile`、`io`；用建议锁文件实现单写者）。
`lancedb` 及其依赖 `pyarrow` 是**可选 extra**，仅由 `lancedb_store.py` 使用（US2）。

**Storage**: 每个命名空间一个目录，包含 `manifest.json`、`episodes.jsonl`、`facts.jsonl`、
`entities.jsonl`、`relations.jsonl`；启用 LanceDB 后，其表也位于同一目录下。

**Testing**: pytest。US1/US3 测试完全离线，**不依赖 extras**；US2 测试仅在可导入 `lancedb` 时执行，否则跳过。

**Target Platform**: 自托管 Linux/macOS，单进程。

**Project Type**: 库（`mnemotrace` 包）+ 仓库内评测框架。

**Performance Goals**: 每条记录追加为 O(1) + fsync；重启加载为 O(n) **流式处理**，绝不一次性加载整个数据块。
LanceDB 的搜索延迟 / 规模目标**通过评测框架测量，不在此断言**（宪章 V）。章程中的读取路径 <100ms 目标不得退化。

**Constraints**: 保持零配置不变量（宪章 II）；加载绝不执行存储代码（不用 pickle/`eval`）；
写入具备崩溃安全性，可恢复撕裂尾部；v1 **每个命名空间仅允许一个写者**。

**Scale/Scope**: US2 面向无法轻松放入 RAM 的语料；具体 N 和 p50/p95 读取延迟作为评测任务确定，不在本计划中作成绩声明。

## 宪章检查

*门禁：Phase 0 研究前必须通过；Phase 1 设计后复查。*

| 原则 | 门禁 | 状态 |
|---|---|---|
| **I. 可复现性** | 本计划不声称任何性能数字；延迟 / 规模声明交由带已提交日志的评测任务验证。 | ✅ PASS |
| **II. 零配置（不可妥协）** | 内存默认实现不变；持久化格式与 LanceDB 为配置选择的可选项；测试断言默认路径不导入重型依赖；无 extras 时 `quickstart.py` 和离线 `pytest` 通过。 | ✅ PASS |
| **III. 接口优先** | `LanceDBVectorStore` 实现 `VectorStore` 抽象基类；`lancedb` 仅在 `lancedb_store.py` 内延迟导入；`mnemotrace/` 核心模块顶层绝不导入它。 | ✅ PASS |
| **IV. 不允许静默损坏** | 序列化器往返保留每个双时间戳、来源列表和 supersedes 指针；失效事实仍以失效状态保存，不硬删除。往返一致性测试作为发布门禁。 | ✅ PASS |
| **V. 先测量** | 采用最简单的安全格式（JSONL + manifest），不提前加入索引 / 缓存。性能优化作为独立、经测量的任务。 | ✅ PASS |
| **VI. 组合，而非单选** | 持久化后端在现有接口之后组合，不替换或分叉内存参照实现。 | ✅ PASS |
| **VII. 诚实措辞** | 明确记录迁移与单写者限制，不夸大能力。 | ✅ PASS |

**无违规 → 复杂度跟踪留空。**

## 项目结构

### 本功能的文档

```text
specs/001-durable-persistence/
├── plan.md              # 本文件
├── research.md          # Phase 0：格式 / 后端 / 崩溃安全性决策
├── data-model.md        # Phase 1：持久记录与 manifest schema
├── quickstart.md        # Phase 1：可运行的验证场景
├── contracts/           # Phase 1：存储后端与磁盘格式契约
└── tasks.md             # Phase 2：由 /speckit-tasks 创建，不在本阶段生成
```

### 源代码（仓库根目录）

```text
mnemotrace/store/
├── base.py            # 已有：VectorStore / DocStore / GraphStore 抽象基类，不变
├── memory_store.py    # 已有：内存参照实现，不变，继续作为默认实现
├── persist.py         # 新增：安全的 JSONL+manifest 序列化 / 反序列化；原子写入、撕裂尾部恢复
├── lancedb_store.py   # 新增：可选、延迟导入的 LanceDBVectorStore，实现 VectorStore
└── migrate.py         # 新增：一次性 pickle -> JSONL 迁移（先 --dry-run 报告数量，再执行）

mnemotrace/memory.py   # 修改：Memory.save/open 委托 store.persist；移除 pickle 路径
mnemotrace/config.py   # 修改：存储后端选择（memory | durable | lancedb）与数据路径
pyproject.toml        # 修改：[project.optional-dependencies] lancedb = ["lancedb"]

tests/
├── test_persist_roundtrip.py    # US1 / SC-001 / FR-002：add->save->open 所有字段一致
├── test_persist_safety.py       # US1 / SC-002 / FR-003,FR-004：加载不执行代码；版本不兼容报错
├── test_persist_crash.py        # US1 / SC-003 / FR-005：撕裂写入恢复保留已提交前缀
├── test_zero_setup_default.py   # II / SC-004 / FR-006：默认路径无重型导入；quickstart 可运行
├── test_lancedb_store.py        # US2 / SC-005 / FR-009：重启持久化与内存结果一致，按依赖门控
└── test_migrate_pickle.py       # US3 / FR-008：dry-run 数量与迁移后实体一致

eval/                  # 测量 LanceDB 读取延迟与规模（SC-006）；提交日志，不凭空断言数字
```

**结构决策：** 沿用单项目库布局。功能几乎全部位于 `mnemotrace/store/`（现有抽象基类之后的三个新模块），
仅对 `Memory.save/open` 和 `config.py` 做集中修改。不新增顶层项目，不改变读写路径的公开 API；
调用者仍使用 `Memory.open(path)` / `mem.save()`，只有磁盘表示和可选后端改变。

## 复杂度跟踪

> 无宪章违规，本节有意留空。
