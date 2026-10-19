# 运行时仅使用摘要目录：验收记录

日期：2026-10-19。按所有者明确决定取消旧目录兼容，替代同日的保留名排除方案。
范围为 MemoryService 存储路径与擦除生命周期，不改变记忆算法、摘要生成规则、JSONL/manifest 格式或公开 benchmark 数字。

## 最终行为

- 移除 `_legacy_safe_user` / `_legacy_paths`。`_path(user)` 始终返回 `_secure_path(user)`。
- 读取、写入、重启加载和 forget 均使用同一摘要目录。目录不存在时从空开始，不能因旧目录存在而回退。
- 账号名目录、`.pkl` 命名的 JSONL 目录及真实 `.pkl` 文件不被服务读写、迁移、合并或删除。
- 保留 `_contained_child` 的真实路径/同级链接/文件名别名保护，以及覆盖整个擦除过程的线程锁和稳定文件锁。
- 普通 `Memory.open(source).save(destination)` 和可信 pickle 显式迁移工具仍可由管理员指定路径使用；不属于服务的自动兼容。

## 先失败再修复

更新旧目录行为回归后，在仍保留回退逻辑的实现上执行：

```powershell
python -m pytest tests/test_namespace_ownership.py -k legacy_directories_are_never -x -o addopts= -q
```

首项失败：预期旧目录不可见、结果为空，实际返回 `{'SYNTHETIC_OWNER_ONLY'}`。
随后取消回退并同步擦除和并发回归，测试通过。

## 验证与原始输出

```powershell
python -m pytest tests/test_namespace_ownership.py tests/test_service_paths.py tests/test_forget_concurrency.py tests/test_forget_links.py tests/test_forget_lock_cleanup.py tests/test_server.py tests/test_openai_compat.py tests/test_release.py -o addopts= -q -rs
python scripts/check_zero_setup.py
python scripts/check_release.py
```

- 相关回归 **137 passed，3 skipped**，见[原始日志](digest_only_namespace_pytest.txt)。
- 零配置检查通过，见[原始日志](digest_only_namespace_zero_setup.txt)。
- 文档更新后重新执行发布检查通过，见[原始日志](digest_only_namespace_release.txt)。

回归覆盖：旧目录存在但读取为空；新写入只进入摘要目录；重启只恢复新数据；清空后摘要目录消失而
旧目录内容逐字节保持不变；B 的账号名等于 A 的目录名时不串读/写/删；B 已有独立存储；旧 `.pkl`
候选为目录或实际文件；同/异服务实例并发、跨进程等待写入、清空后重建；真实 Windows junction；
大小写/尾点逻辑 ID 各自使用摘要目录；已鉴权 HTTP 隔离；显式复制确认归属的旧库并保留源库。

## 本地旧目录检查

仅检查当前 shell 和本地 `.env` 所选数据根目录（未显式设置时为 `~/.mnemotrace/data`）。
按目录名和 manifest 文件存在性分类，不读取 manifest 正文、事实、情节、凭据或租户名称，不跟随链接。

```json
{"configured_root":false,"root_exists":true,"digest_shaped_jsonl_directories":1,"legacy_named_jsonl_directories":0,"legacy_pkl_files":0,"linked_entries_not_followed":0}
```

本次检查未发现该目录下需要迁移的旧命名数据目录；未执行真实数据迁移或清理。
这是名称形状与文件存在性的检查，不证明每个库的历史归属，也不代表所有运行中进程、其他磁盘、
容器或远程服务的启动配置均已检查。

## 升级与验证边界

- 停止并升级共享数据目录的全部 HTTP/MCP/后台进程，重启后生效；本次未重启正在运行的服务。
- 其他位置如仍有需要保留的旧库，先备份并核验实际归属，再显式复制到该 namespace 尚不存在的摘要目录；
  不直接覆盖已有目标，不以旧名字判断归属，确认新库后再人工处理旧来源。
- Windows / Python 3.12。3 项既有符号链接回归因系统权限跳过，真实 Windows junction 测试已执行。
  pytest-asyncio 未安装，两个既有 asyncio 配置产生警告；执行的同步、线程、进程和 HTTP 回归通过。
- 未运行全量 pytest、真实 MCP 工具或线上部署验收。测试使用临时合成数据并在沙箱外运行，日志本机用户路径已脱敏。
- [初始归属修复记录](namespace_ownership_validation.md)保留为历史证据，不代表当前仍有自动兼容分支。
