# watch / hook 安装预检跨平台验收

## 修复范围

`watch_install.preflight_import()` 直接启动指定 Python，通过 subprocess 的 `env` 参数提供最小环境，
不再调用外部 POSIX `env -i`。POSIX 保留原有 HOME/PATH 约束；Windows 设置目标 USERPROFILE，
保留 SystemRoot/WINDIR 与系统 PATH，不继承 PYTHONPATH、PYTHONHOME、venv 激活或供应商凭据。
导入前仍切换到根目录，避免当前仓库或 shell 配置掩盖未安装的模块。

解释器无法启动时返回预检错误，由实际安装流程在创建 key、任务或配置文件之前拒绝，
不再直接抛出 FileNotFoundError。dry-run 继续只显示预检与配置差异，不加载任务。

安装流程单测模拟预检，使用 `sys.executable` 表示当前解释器并验证目标模块；
纯配置渲染测试保留固定样例路径。真实子进程行为由独立回归覆盖，避免用打桩代替预检验证。

## 回归覆盖

- PATH 中没有外部工具、调用方 PYTHONHOME/PYTHONPATH 被污染时，指定解释器仍可导入标准库。
- 预检环境不携带调用方的 Python 配置、venv 激活标记或合成敏感哨兵；Windows 保留必需系统变量。
- 仅存在于当前目录和 PYTHONPATH 的合成模块不能通过预检。
- 指定解释器不存在时，在创建 key 和调度目录之前拒绝安装。
- hook dry-run、目标不匹配提示、无法原样复现 settings 的拒绝行为及 launchd dry-run 均保留原验证目的。
- launchd/systemd 安装流程通过录制式 runner 验证顺序，不调用真实调度器。

## 复现命令与结果

环境：Windows、Python 3.12，使用仓库 `.venv` 中的测试依赖。真实子进程、HTTP 和 junction
回归在沙箱外执行，临时数据位于仓库忽略的 `.mnemotrace/` 目录。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_watch_install.py tests/test_session_hook.py -o addopts= -q -rs
.venv/Scripts/python.exe -m pytest -o addopts= -q -rs
python scripts/check_zero_setup.py
python scripts/check_release.py
python scripts/build_landing_pages.py --check
```

| 验证 | 结果 | 原始输出 |
| --- | --- | --- |
| 修复前新增预检回归 | 3 failed，复现外部 env 启动失败 | [失败输出](watch_preflight_red.txt) |
| watch / hook 定向回归 | 44 passed，4 skipped | [定向输出](watch_preflight_focused_pytest.txt) |
| 完整 pytest | 866 passed，11 skipped，无失败 | [完整输出](watch_preflight_full_pytest.txt) |
| zero-setup | 通过 | [零配置输出](watch_preflight_zero_setup.txt) |

定向跳过项为 macOS plutil 以及 Windows 不支持的 POSIX 权限位/文件符号链接检查。
完整回归另含既有可选依赖分支和符号链接权限跳过，具体原因见原始输出。

## 验证边界

没有安装、卸载或加载真实 launchd/systemd 任务，没有改写用户真实 settings.json。
Windows 上验证了真实的隔离 Python 子进程；macOS/Linux 的真实调度任务启动仍需对应平台验收。
调度器支持范围、记忆算法、存储格式与公开 benchmark 数字不变。
