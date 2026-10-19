# 本地导入 CLI 统一摘要目录验收

日期：2026-10-19。对应旧兼容清单第 1 项。本次只修复该入口，其余收束项仅记录于
[问题台账 ISSUE-023～032](../docs/known-issues.zh-CN.md#issue-023)，未修改其代码或接口。

## 问题与修复

原 `connectors.__main__._import_local` 自行删除 namespace 中的部分字符，将其作为文件夹名，
并在目录缺失时尝试 `.pkl` 候选。这与 MemoryService 的摘要目录规则不一致：本地导入成功后，
服务可能看不到数据；特殊 namespace 还可能指向非预期位置。

新增 CLI 入口回归在修复前失败，返回目录为 `synthetic-user`，预期为服务的
`synthetic-user--14b2e141bf7cc394`。复现命令：

```powershell
python -m pytest tests/test_import_cli.py -x -o addopts= -q
```

修复后 `_import_local` 创建 MemoryService 并调用其 `import_()`：

- 路径解析、完整读-改-存锁、读取已有内容、导入去重和持久化由共享服务负责。
- CLI 不再保留自己的 namespace 清洗器或旧路径选择分支；返回的 `store` 为实际摘要路径。
- 保留 CLI 默认 hashing/规则抽取，以及命令行指定嵌入/LLM、跳过整理和跳过摘要的参数。
- 使用 `finally` 关闭此次导入的服务实例；远程 HTTP POST、解析和 dry-run 路径未修改。

## 验证

```powershell
python -m pytest tests/test_import_cli.py tests/test_connectors.py tests/test_import_dedupe.py tests/test_namespace_ownership.py tests/test_service_paths.py tests/test_forget_concurrency.py -o addopts= -q -rs
python scripts/check_zero_setup.py
python scripts/check_release.py
```

- 相关回归：**93 passed**，见[测试原始输出](import_cli_digest_pytest.txt)。
- 零配置检查通过，见[原始输出](import_cli_digest_zero_setup.txt)。
- 发布检查通过，见[原始输出](import_cli_digest_release.txt)。

新增回归覆盖实际 CLI main 入口、账号名旧目录、`.pkl` 命名的 JSONL 目录和惰性标记文件，
确认导入目标等于服务路径、服务可读取导入内容、旧来源内容逐字节不变；另覆盖点目录、父目录、
分隔符、Unicode namespace，以及保留服务已有事实、重启后幂等去重、CLI 等待已有服务事务锁、
默认离线提取/摘要和跳过整理/摘要参数。被保存的原文按现行格式保留 `user:` 角色前缀。

## 边界

- Windows / Python 3.12；测试使用临时合成数据，不读取或迁移真实记忆。
- 测试在沙箱外运行以避开已知 pytest/线程沙箱挂起；日志本机用户路径已脱敏。
- pytest-asyncio 未安装，产生两个既有配置项警告；本次已执行的测试全部通过。
- 未运行全量 pytest、真实嵌入/LLM、远程服务或真实 MCP 工具；未对运行中的服务执行重启。
- 不改变记忆算法、存储字段、schema、画像 count、浏览器偏好、pickle 迁移器或 API 参数别名，
  不产生新的公开 benchmark 数字。
