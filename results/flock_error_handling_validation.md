# 文件锁错误分类验收

日期：2026-10-18。对应审查项 R-01。

## 修复

原 Windows 阻塞获取锁时把所有 `OSError` 当作锁竞争，坏 fd 等真实故障会反复等待；
两平台非阻塞路径还会吞掉真实故障并返回 `False`。

现将 `os.lseek` 与加锁错误处理分开，定位失败直接抛出。Windows `LK_NBLCK` 的
`EACCES` 按锁竞争处理，阻塞等待 10ms、非阻塞返回 `False`；本机两个独立句柄锁同一
字节区域实际返回 `PermissionError(errno=13, winerror=None)`。POSIX 非阻塞只对
`EACCES` / `EAGAIN` 返回 `False`。两平台加锁 `EINTR` 均立即重试，其他异常直接传播。
hook/watch 加锁失败时关闭文件，再传播异常。

未增加任意等待超时：其他进程确实持锁时，阻塞获取仍等待其释放。锁文件路径、数据格式和
服务/API 保持原样；未改变 unlock 的行为。

## 回归与命令

先增加回归再运行旧实现：`tests/test_flock.py` 为 **17 failed, 8 passed**，能捕获坏 fd
重试、非阻塞吞错误和中断处理问题。测试把不应发生的 sleep 直接转成失败，避免挂起。
之后补充 hook/watch 异常关闭回归，旧调用方为 **2 failed, 25 deselected**。

修复后在仓库根目录执行：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_flock.py tests/test_forget_concurrency.py tests/test_forget_lock_cleanup.py tests/test_persist_safety.py tests/test_persist_roundtrip.py tests/test_persist_crash.py -q --basetemp .pytest_cache/flock-final -o addopts='' --tb=short
New-Item -ItemType Directory -Force .pytest_cache/flock-runtime | Out-Null
$env:TEMP = (Resolve-Path .pytest_cache/flock-runtime).Path
$env:TMP = $env:TEMP
python scripts/check_zero_setup.py
```

- **47 passed**，覆盖两平台错误分类、Windows 真实双句柄竞争及释放后重新获取、hook/watch
  失败关闭文件、清空与线程/跨进程等待写入、持久化安全和往返/崩溃恢复。
- zero-setup 验证通过。脚本核验的是已有公开结果，不产生新模型 benchmark。
- 日志见 [pytest 输出](flock_error_handling_pytest.txt) 和 [零配置输出](flock_error_handling_zero_setup.txt)。
  日志为 UTF-8，个人用户目录前缀替换为 `<USER_HOME>`。

## 边界

宿主为 Windows / Python 3.12，原生竞争及并发回归在此平台执行；POSIX 分支使用 `fcntl`
替身验证错误处理，没有运行原生 Linux 全套测试。未运行完整后端 pytest 或真实模型评测。
禁用第三方 pytest 插件后，两项 asyncio 配置产生 unknown-option 警告，不影响上述同步测试。
