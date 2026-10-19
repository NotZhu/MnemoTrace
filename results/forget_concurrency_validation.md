# 清空命名空间的并发隔离验收（2026-10-18）

## 范围与根因

仅处理审查第 4 项：`MemoryService.forget()` 在退出 `write_lock` 后继续删除锁文件和目录。
另一笔写入可在这段窗口成功，随后目录删除失败，甚至把新写入使用的锁文件删除。

进程锁改为数据根目录中的 `@<安全命名空间>.service.lock`，与存储目录生命周期分离，
保留空锁文件以供等待者及重建后的写入使用。清理文件、子目录和最终目录全部在两层写锁内完成。
`@` 不属于旧目录名允许的字符，避免锁文件被其他命名空间的旧目录回退路径删除。
没有调整鉴权、前端、事实编辑、消融判据或检索算法，没有重跑真实模型 benchmark。

## 复现与回归

先添加 `tests/test_forget_concurrency.py` 的真实服务回归，再执行修复前版本：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_forget_concurrency.py -x -q --tb=short --basetemp .pytest_cache/forget-red-20261018
```

首项稳定失败：`AssertionError: write completed before forget removed the directory`。
测试在最终目录删除前启动等待写入，验证清理尚未完成时写入不能结束；结束后重新打开存储，
断言只保留清空后写入的数据。

修复后的回归包含同服务的线程锁、独立服务实例的文件锁、独立 spawn 进程、新安全目录、
旧目录、旧 `.pkl` 命名目录，以及清空/重建前后锁文件身份不变和控制文件保护。
存量路径安全测试改为显式建立待清理目录，因为取得新的服务锁不再创建存储目录。

```powershell
New-Item -ItemType Directory -Path .pytest_cache/forget-runtime-temp -Force | Out-Null
# 当前沙箱的系统临时目录会拒绝子目录访问，测试临时数据统一放在工作区。
$env:TEMP = (Resolve-Path .pytest_cache/forget-runtime-temp).Path
$env:TMP = $env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -m pytest tests/test_forget_concurrency.py tests/test_service_paths.py tests/test_reembed.py tests/test_async_consolidation.py tests/test_persist_roundtrip.py tests/test_persist_safety.py -o addopts='' -q --disable-warnings --tb=short --basetemp .pytest_cache/forget-validation-final-20261018
python -X utf8 examples/quickstart.py
```

45 项测试通过；两条警告来自关闭第三方插件后未注册的现有 asyncio 配置项。
原始输出见 [pytest 输出](forget_concurrency_pytest.txt)。零配置 quickstart 通过。
验证环境为 Windows / Python 3.12，未运行完整 pytest、Linux 实机或容器验收。

## 升级边界

锁文件位置发生变化，升级时须停用并重启共享同一数据目录的全部服务/MCP 进程；
不支持使用目录内锁的旧进程与新进程混跑。JSONL/manifest 和业务接口格式保持原样，
清空后保留的根目录锁文件不包含记忆正文。
