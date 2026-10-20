# 任务清单：持久化存储后端

**Input**: `/specs/001-durable-persistence/` 中的设计文档

**Prerequisites**: `plan.md`、`spec.md`、`research.md`、`data-model.md`、`contracts/`

**Tests**: 功能规格的成功标准要求测试覆盖往返一致性、安全、崩溃恢复、零配置、后端结果一致性及迁移。

**Organization**: 按用户故事分组，让每个故事可独立实施和验证。

## Phase 1：准备（共享基础设施）

**目的：** 准备存储模块和依赖元数据，不改变默认运行时行为。

- [x] T001 在 `pyproject.toml` 中添加可选 `lancedb` extra
- [x] T002 [P] 在 `mnemotrace/store/persist.py` 中创建持久化错误类型与 schema 常量
- [x] T003 [P] 从 `mnemotrace/store/__init__.py` 导出持久化辅助函数

---

## Phase 2：基础能力（阻塞性前置条件）

**目的：** 构建 US1、US2、US3 共用的、可安全表示为 JSON 的 dataclass 序列化工具。

**关键约束：** 本阶段完成前，不得开始任何用户故事的工作。

- [x] T004 在 `mnemotrace/store/persist.py` 中，为 `Episode`、`Fact`、`Entity`、`Relation`、`WorkingMemory` 和 `Conflict` 实现显式 dataclass → 记录序列化
- [x] T005 在 `mnemotrace/store/persist.py` 中，实现显式记录 → dataclass 重建，并容忍较新版本字段
- [x] T006 在 `mnemotrace/store/persist.py` 中，实现 manifest 创建、解析、schema 校验及嵌入维度校验
- [x] T007 [P] 在 `tests/test_persist_roundtrip.py` 中添加共享持久化测试夹具辅助函数

**检查点：** 序列化器无需 pickle 或 `eval` 即可转换所有持久化 dataclass。

---

## Phase 3：用户故事 1 — 重启后零数据丢失（优先级：P1）MVP

**目标：** 用安全的 JSONL+manifest 存储替换 pickle 快照，无损重载当前所有记忆集合。

**独立测试：** 添加固定语料、整合、保存到存储目录，再重新打开；断言所有 episode、fact、图谱实体、关系、工作记忆、冲突、策略、关注点、身份、别名及 persona 缓存均被保留。

### 用户故事 1 的测试

- [x] T008 [P] [US1] 在 `tests/test_persist_roundtrip.py` 中，为所有持久化集合添加往返一致性测试
- [x] T009 [P] [US1] 在 `tests/test_persist_safety.py` 中，添加加载不执行代码及版本不兼容测试
- [x] T010 [P] [US1] 在 `tests/test_persist_crash.py` 中添加撕裂尾部恢复测试
- [x] T011 [P] [US1] 在 `tests/test_zero_setup_default.py` 中添加零配置导入与 quickstart 守卫测试

### 用户故事 1 的实现

- [x] T012 [US1] 在 `mnemotrace/store/persist.py` 中实现带建议锁的原子 JSONL+manifest 保存协议
- [x] T013 [US1] 在 `mnemotrace/store/persist.py` 中实现流式 JSONL 加载协议，支持格式异常尾行恢复
- [x] T014 [US1] 在 `mnemotrace/memory.py` 中，将 `Memory.save()` 和 `Memory.open()` 接入 JSONL+manifest 持久化
- [x] T015 [US1] 在 `mnemotrace/memory.py` 中，仅通过显式迁移路径保留向后兼容的旧 pickle 读取，不用于正常 `Memory.open()`
- [x] T016 [US1] 更新 `specs/001-durable-persistence/quickstart.md` 中的持久化文档

**检查点：** 用户故事 1 功能完整，无可选 extras 时也可独立测试。

---

## Phase 4：用户故事 2 — LanceDB 作为首个真实向量后端（优先级：P2）

**目标：** 在 `VectorStore` 接口之后添加可选的持久化 LanceDB 向量存储，保持内存实现为默认。

**独立测试：** 用相同事实填充内存存储与 LanceDB，重启 LanceDB，断言 top-k 在已记录的容差范围内一致。

### 用户故事 2 的测试

- [x] T017 [P] [US2] 在 `tests/test_lancedb_store.py` 中添加持久化和重启测试，缺少依赖时跳过
- [x] T018 [P] [US2] 在 `tests/test_lancedb_store.py` 中添加内存存储与 LanceDB 的 top-k 一致性测试

### 用户故事 2 的实现

- [x] T019 [US2] 在 `mnemotrace/store/lancedb_store.py` 中实现延迟导入的 `LanceDBVectorStore`
- [x] T020 [US2] 在 `mnemotrace/config.py` 中添加后端选择字段 `storage` 和 `data_path`
- [x] T021 [US2] 在 `mnemotrace/memory.py` 中接入 LanceDB 向量存储工厂选择，默认路径不导入 LanceDB
- [x] T022 [US2] 在 `README.md` 和 `specs/001-durable-persistence/quickstart.md` 中说明 LanceDB 的安装与选择方式

**检查点：** 安装可选 extra 后，用户故事 2 可用；对零配置用户无影响。

---

## Phase 5：用户故事 3 — 一次性安全迁出 pickle（优先级：P3）

**目标：** 为现有 pickle 快照提供显式迁移工具，支持 dry-run 数量报告和一致性检查。

**独立测试：** 创建已知旧 pickle 快照，先执行迁移 dry-run，再实际迁移，加载新存储并断言一致。

### 用户故事 3 的测试

- [x] T023 [P] [US3] 在 `tests/test_migrate_pickle.py` 中添加 dry-run 和实际迁移测试

### 用户故事 3 的实现

- [x] T024 [US3] 在 `mnemotrace/store/migrate.py` 中实现旧 pickle 加载和 JSONL 写入
- [x] T025 [US3] 在 `pyproject.toml` 中添加迁移 CLI 入口
- [x] T026 [US3] 在 `README.md` 和 `specs/001-durable-persistence/quickstart.md` 中说明迁移警告与命令

**检查点：** 现有用户可主动迁移，正常运行不再加载 pickle。

---

## Phase 6：完善与跨模块事项

**目的：** 按项目不变量与 benchmark 纪律验证功能。

- [x] T027 无可选服务时运行 `pytest -q` 和 `python3 examples/quickstart.py`
- [x] T028 [P] 在 `eval/` 中添加持久化后端评测冒烟场景
- [x] T029 [P] 仅在新的持久化性能声明有已提交原始日志时更新 `RESULTS.md`
- [x] T030 审阅 `README.md`、`README.zh-CN.md` 和 `docs/index.html` 的公开文案，确保措辞诚实，不作无 benchmark 支撑的规模或延迟声明

---

## 依赖与执行顺序

### 阶段依赖

- **准备（Phase 1）：** 无依赖。
- **基础能力（Phase 2）：** 依赖准备阶段，阻塞全部用户故事。
- **用户故事 1（Phase 3）：** 依赖基础能力，构成 MVP 范围。
- **用户故事 2（Phase 4）：** 依赖基础能力，可复用 US1 持久化元数据。
- **用户故事 3（Phase 5）：** 依赖 US1 序列化器。
- **完善（Phase 6）：** 依赖所选发布范围内的用户故事完成实现。

### 用户故事依赖

- **US1（P1）：** 不依赖 US2 或 US3，交付安全持久格式的 MVP。
- **US2（P2）：** 基础能力完成后即可开始，但最终集成应遵循 US1 manifest 校验。
- **US3（P3）：** 需要 US1 保存 / 加载辅助函数来写入新格式。

### 可并行事项

- T002 与 T003 可并行。
- 测试夹具形态确定后，T007 可与 T004-T006 并行。
- T008-T011 分别面向不同测试文件，可并行。
- `LanceDBVectorStore` 形态确定后，T017 与 T018 可并行。
- 实现完成后，T028-T030 可并行。

---

## 并行示例：用户故事 1

```bash
Task: "在 tests/test_persist_roundtrip.py 中，为所有持久化集合添加往返一致性测试"
Task: "在 tests/test_persist_safety.py 中，添加加载不执行代码及版本不兼容测试"
Task: "在 tests/test_persist_crash.py 中添加撕裂尾部恢复测试"
Task: "在 tests/test_zero_setup_default.py 中添加零配置导入与 quickstart 守卫测试"
```

---

## 实施策略

### MVP 优先（仅用户故事 1）

1. 完成 Phase 1 和 Phase 2。
2. 在现有 `Memory.save()` 和 `Memory.open()` API 之后实现 US1 保存 / 加载。
3. 用 US1 测试、完整离线 `pytest -q` 和 `python3 examples/quickstart.py` 验证。
4. 添加 LanceDB 或迁移功能前，暂停并审阅。

### 增量交付

1. US1 以零可选依赖提供安全、可重启的持久化。
2. US2 以可选后端提供面向扩展规模的向量持久化。
3. US3 通过显式迁移保护现有 pickle 用户。
4. 完善阶段仅发布由原始日志支撑、经过测量的声明。

### 说明

- `[P]` 任务涉及不同文件，或不依赖尚未完成的实现细节即可完成。
- 每个用户故事任务均包含精确文件路径，便于追踪。
- 没有已提交评测日志时，不得宣称持久化延迟、规模或 benchmark 改善。
