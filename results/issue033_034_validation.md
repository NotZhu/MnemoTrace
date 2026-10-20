# ISSUE-033 / 034 统一记忆访问协议验收

日期：2026-10-20。状态：实现完成，本地 Windows 原生验证；Ubuntu/POSIX CI 待确认。

## 根因与实施

旧服务将锁外加载的内容与随后取得的 manifest 指纹配对，可能发布“旧内容 + 新版本”，也可能
跨集合加载不同保存代次。写入入口的自动重嵌入重新获取外层已持有的文件锁，形成自我等待。
读路径还会强化权重、搬迁冷热事实和清扫工作记忆，因此线程锁必须覆盖完整读取操作。

实现按稳定锁路径共享进程内可重入线程锁及线程所属文件锁状态，保留各服务实例的缓存。
写作用域持锁读改存；默认内存索引热读只持线程锁，冷加载持文件锁覆盖全部集合及发布。
嵌套读取固定本次 Memory，响应与缓存引用分离，answerer 使用组装好的字符串在读作用域外调用。
迁移复用当前文件锁，保存校验对象归属与基准令牌，失败时移除脏缓存。

manifest 新增可选 UUID `snapshot_id`，令牌从实际读取或成功提交的 manifest 取得。
旧格式用 manifest 原文摘要兼容，下一次保存升级。Windows 的共享删除读取句柄与 ReplaceFileW
保证热版本检查的旧句柄不会阻止新 manifest 原子提交；普通 MoveFileEx/os.replace 的在用目标
替换失败已单独复现并转为回归。

## 先失败再修复的检查

在实现前已执行以下回归并观察到失败：

```powershell
python -B -m pytest -q tests/test_service_transactions.py -k loading_cannot -x
# 修复前失败：写入或清空绕过了快照加载（AssertionError）。
python -B -m pytest -q tests/test_service_transactions.py::test_write_migration_does_not_reacquire_service_file
# 修复前失败：服务文件锁在释放前被获取了两次（AssertionError）。
```

Windows manifest 读句柄回归在补充原生替换实现前也失败：

```powershell
python -B -m pytest -q tests/test_service_transactions.py::test_live_manifest_reader_does_not_prevent_atomic_commit
# 修复前失败：os.replace 无法替换仍有读句柄的 manifest（PermissionError，[WinError 5]）。
```

## 复验命令与原始日志

原始输出、退出码、Python 版本与每个检查的完整命令保存在
[issue033_034_validation.jsonl](issue033_034_validation.jsonl)。它记录最终通过的验证；上面的
修复前失败说明为本轮执行记录的中文摘要。日志中的 `<REPO>`、`<HOME>` 为路径脱敏占位符。

| 检查 | 本地 Windows / Python 3.12.6 结果 |
| --- | --- |
| 访问协议及相关既有回归 | 159 passed；包含 44 项新增访问协议用例 |
| 完整 pytest | 910 passed，11 skipped |
| 零配置验证 | 通过：源码/模块 quickstart、CLI help、离线/持久化 harness、既有证据校验与 stdlib 编译 |
| 既有测试的取锁观测点收紧 | 完整测试后将 CLI/forget 的事件定位至实际线程锁获取入口，7 项专项再次通过；生产实现未再变化 |

```powershell
python -B -m pytest -o addopts= -q tests/test_service_transactions.py tests/test_namespace_ownership.py tests/test_service_paths.py tests/test_import_cli.py tests/test_forget_concurrency.py tests/test_reembed.py tests/test_persist_safety.py tests/test_persist_roundtrip.py tests/test_persist_crash.py tests/test_flock.py
python -B -m pytest -o addopts= -q
python -B scripts/check_zero_setup.py
```

测试不使用 API key 或网络模型。临时数据为合成记录；本地验证在允许创建 Windows junction 的
原生执行环境运行，临时文件集中于仓库 `.pytest_cache`。新增测试已由现有 CI 的
`windows-latest` / `ubuntu-latest`、Python 3.10 / 3.12 矩阵自动收集，pytest 步骤设整体超时。
另已通过 `scripts/check_release.py`、`scripts/build_landing_pages.py --check`、改动 Python 文件的
导入/语法静态检查及 `git diff --check`；新的锁协调模块与并发回归文件通过完整 Ruff 检查。

## 覆盖与边界

- 首次加载、版本变化重载与写入/forget 的固定交错：同实例、不同实例；再次保存与全新实例读取。
- 跨集合读取期间阻止保存；manifest 时间戳和长度相同时仍识别新提交；旧格式升级与旧对象拒写。
- 原生子进程迁移：维度/模型身份不匹配分别覆盖 read、remember、add_fact、import、async/flush；
  开关关闭仍拒绝不兼容写入，并发迁移保留两次写入。检查磁盘事实、原文、向量与重启。
- 嵌套状态读取、跨实例同线程借锁、LRU 淘汰期间固定对象、响应 provenance 引用分离。
- recall/remember 的向量迭代交错、冷热搬迁中途与工作记忆清扫中途阻止同空间写入。
- 跨进程写事务期间热读可完成、冷加载等待提交；planner 不持文件锁，answerer 退出读作用域后运行。
- 保存/迁移失败不发布半成品缓存；子进程超时后终止并回收。

普通热读允许上一份已提交视图，读取副作用维持原有持久化语义。直接 Memory 调用方的并发
读改存、保存中途崩溃后的多文件恢复、线上部署状态均不在本轮保证中。共享目录的服务需统一升级。
Ubuntu/POSIX 原生锁验证完成前，[ISSUE-033](../docs/known-issues.zh-CN.md#issue-033) 与
[ISSUE-034](../docs/known-issues.zh-CN.md#issue-034) 均保持待验收。
