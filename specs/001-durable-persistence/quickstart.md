# 快速上手：验证持久化存储

以下可运行场景用于端到端验证本功能。**本文不声称任何性能数字**；所有延迟 / 规模数字都由独立任务
通过评测框架测量，并提交日志（宪章 I、V）。

## 前置条件

- US1 / US3：除仓库之外无需其他配置（零配置 — 宪章 II）。
- US2: `pip install -e '.[lancedb]'`.

## 场景 A — 重启后零丢失（US1 · SC-001 · INV-1..3）
```bash
python3 - <<'PY'
from mnemotrace import Memory
m = Memory.open("data/qs/alice")                 # 创建 / 打开 JSONL+manifest 存储目录
m.add("My flight is AA100 to Boston on Friday.")
m.add("Actually the flight changed to AA200.")   # 创建 supersedes 链
m.consolidate()
m.save()
PY
python3 - <<'PY'                                  # 在新进程中重新打开
from mnemotrace import Memory
m = Memory.open("data/qs/alice")
print(m.search("which flight?").answer())         # AA200；AA100 仍被保存，但已失效
PY
```
**预期：** 所有 episode / fact 重新加载；被替代的 AA100 事实仍保存且保持失效；来源与双时间戳完整。

## 场景 B — 零配置默认行为不变（宪章 II · SC-004）
```bash
pip uninstall -y lancedb 2>/dev/null; pytest -q   # 完整离线套件：无 extras 时通过
python3 examples/quickstart.py                    # 哈希 embedder + 内存存储
```
**预期：** 全部通过。`tests/test_zero_setup_default.py` 断言默认导入图绝不导入 `lancedb`。

## 场景 C — 安全加载与版本门禁（SC-002 · FR-003 · FR-004）
```bash
pytest -q tests/test_persist_safety.py
```
**预期：** 特意构造的存储文件在打开时不能触发代码执行；带有未来 `schema_version` 的 manifest
抛出 `IncompatibleStoreError`；嵌入维度 / 模型 id 不匹配时明确报错，而非在错误向量空间中打开存储。

## 场景 D — 崩溃恢复（SC-003 · FR-005）
```bash
pytest -q tests/test_persist_crash.py
```
**预期：** JSONL 文件中，manifest 数量之后的撕裂字节只会导致未提交尾部被丢弃；
manifest 已提交前缀内部缺失记录或格式错误时，抛出 `StoreFormatError`。

## 场景 E — LanceDB 后端持久化与结果一致性（US2 · SC-005 · FR-009）
```bash
pip install -e '.[lancedb]'
pytest -q tests/test_lancedb_store.py
```
**预期：** 向量在重启后保留；重新加载后的 top-k 在容差范围内与内存存储一致。

最小直接使用示例：
```bash
python3 - <<'PY'
from mnemotrace import Memory
from mnemotrace.config import Config

cfg = Config(storage="lancedb", data_path="data/qs/alice-vectors")
m = Memory.open("data/qs/alice-lancedb", config=cfg)
m.add_fact("user", "works_at", "Moonshot AI", user_id="alice")
m.save()

reopened = Memory.open("data/qs/alice-lancedb", config=cfg)
print(reopened.search("Where do I work?", user_id="alice").answer())
PY
```
**预期：** 输出 `Moonshot AI`；JSONL manifest 对应可移植快照，LanceDB 将向量表保存在 `data_path` 下。

## 场景 F — 迁出 pickle（US3 · FR-008）
```bash
python3 -m mnemotrace.store.migrate --from data/legacy/alice.pkl --to data/qs/alice --dry-run  # 报告数量，不写入
python3 -m mnemotrace.store.migrate --from data/legacy/alice.pkl --to data/qs/alice            # 写入 JSONL
# 安装包后的等价命令：
#   mnemotrace-migrate-pickle --from data/legacy/alice.pkl --to data/qs/alice
```
**预期：** dry-run 报告各实体类型的数量且不写入；实际迁移后实体与 pickle 一致。
仅对可信旧快照运行此命令；正常的 `Memory.open()` 不加载 pickle。
