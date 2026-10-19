# ISSUE-002：旧目录兼容的命名空间归属验收

历史记录：本节的保留名排除兼容方案已被所有者同日决定替代。当前运行时已完全取消旧目录回退，
读取、写入和擦除都只使用摘要目录；最终实现与新日志见[仅摘要目录验收](digest_only_namespace_validation.md)。

日期：2026-10-19。范围为 MemoryService 的存储路径、兼容回退和擦除，不改变记忆算法或公开 benchmark 数字。

## 原因与修复

原实现允许 B 的逻辑 ID 等于 A 的摘要目录名：B 的旧目录候选因此与 A 的实际目录重合。
`_path(B)` 可串读/写入 A，`forget(B)` 也会将该目录加入删除候选；只检查目录位于数据根目录内不够。
`owner.pkl` 作为逻辑 ID 时还可能冒认 owner 的旧 `.pkl` 候选，顶层同级链接及 Windows 文件名别名也能指向其他目录。

新增回归先在原实现失败：

```text
python -m pytest tests/test_namespace_ownership.py -x -o addopts= -q
FAILED test_reserved_legacy_name_cannot_read_write_or_erase_owner[False-secure]
E AssertionError: assert {'SYNTHETIC_OWNER_ONLY'} == set()
```

修复选择保留名排除策略，不增加新的存储元数据或格式：

- `_legacy_safe_user` 排除系统摘要目录名形式（含大小写变体），以及 `.pkl` 结尾的逻辑 ID。
  这些逻辑 ID 仍能使用自己的摘要目录，但不再认领任何旧候选。
- `_path` 与 `forget` 共用 `_legacy_paths`，即使 B 已有独立摘要目录，清空时也不会碰 A 的目录。
- `_contained_child` 除直接子项边界外，还要求真实目录项名称完全一致，拒绝同级链接、大小写/尾点别名。
- 保留原有跨线程/跨进程写锁，既有摘要目录映射和 JSONL/manifest 格式不变。

## 验证与命令

```powershell
python -m pytest tests/test_namespace_ownership.py tests/test_service_paths.py tests/test_forget_concurrency.py tests/test_forget_links.py tests/test_forget_lock_cleanup.py tests/test_server.py tests/test_release.py tests/test_openai_compat.py tests/test_mcp.py -o addopts= -q
python scripts/check_zero_setup.py
```

- 相关核心、HTTP、OpenAI 兼容、发布及并发回归：**135 passed，4 skipped**，见[主回归日志](namespace_ownership_pytest.txt)。
- 随后补充 `.pkl` 候选为实际文件的两个用例，专项运行 **2 passed，29 deselected**，见[旧快照候选文件日志](namespace_ownership_pickle_pytest.txt)。因此重新运行上面第一条完整命令时会包含这两项新增用例。
- 零配置检查通过，见[原始输出](namespace_ownership_zero_setup.txt)。覆盖 source/module quickstart、离线 harness、既有 evidence-log 校验、论文统计和 stdlib 编译。

覆盖 B 等于 A 的摘要目录名、B 已有独立存储、B 为旧 `.pkl` 候选名、读取/写入/擦除、
同/异服务实例的并发操作、重启后结果、合法旧目录继续使用、保留名旧库显式复制。
对 A 存储的字节比较确认 B 的写入没有改动 A；真实 Windows junction 与大小写/尾点别名拒绝也通过。
已鉴权 HTTP 回归分别配置 A/B 的合成 key，B 的读取、写入和清空不影响 A；这不是鉴权绕过测试。

## 保留名旧库的迁移与升级

历史目录若使用保留名，无法仅凭文件名证明它属于哪一个逻辑 namespace；服务不自动认领、合并、
移动、改写或删除它。此类旧内容可能暂时不在该 namespace 的查询结果中，且不会由该 namespace 的
forget 自动擦除，需要管理员确认归属后处理。

1. 停止所有共享数据目录的 HTTP、MCP 和后台写入进程，备份数据。
2. 依据历史配置、来源和租户记录确认源库的真实归属，不能只看目录名；如果无法确认，保留源库并停止迁移。
3. 对确认归属的 JSONL 目录，用匹配的 embedder/config 显式 `Memory.open(source).save(destination)`，
   destination 必须是该 namespace 自己的摘要目录且尚不存在。已存在目标时拒绝直接覆盖或合并。
4. 重启后核验事实、来源、时间历史和隔离，再人工处理旧来源；源库在复制过程中保持原样。
   真正的可信 pickle 文件只使用 `mnemotrace-migrate-pickle`，不经普通 Memory.open。

全部共享进程应升级并重启；不混跑仍使用旧回退规则的版本。本次没有对真实数据执行迁移或删除。

## 验证边界

- Windows / Python 3.12；3 项既有文件/目录/悬空符号链接回归因权限不足跳过，真实 junction 已执行。
  MCP 可选依赖未安装，整个 MCP 测试模块跳过；不能据此声明真实 MCP 工具调用已通过。
- pytest-asyncio 未安装，两个既有 asyncio 配置项产生警告；已执行的同步、线程、进程及 HTTP 用例均通过。
- 测试在沙箱外运行以避开当前沙箱的 pytest/线程挂起问题，仅使用临时合成数据。日志中的本机用户路径已脱敏。
- 未运行全量 pytest，未核验线上部署或历史真实数据的归属，也未证明过去是否发生过串读/误删。
