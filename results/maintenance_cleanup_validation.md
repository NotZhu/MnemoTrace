# M-01～M-08 维护收束验收

日期：2026-10-19。按所有者“都修改，最后两个不保留”执行。

## 变更结果

| 项目 | 最终处理 |
| --- | --- |
| M-01 | `forget` 改用 `shutil.rmtree`，保留整个擦除写锁与顶层路径保护；修复已复现的 junction 目标文件误删 |
| M-02 | memory/service 的 `display_of` 统一模块导入，移除六处局部导入 |
| M-03 | 完整陈述编辑合并相邻条件，内容、检索文本与 display 在同一分支更新 |
| M-04 | facility 扫描只剥离一次 MCP 前缀，保留公共完整名称判别入口 |
| M-05 | 沿用已完成的 key / 查询错误内存态；本次复核登录、刷新和隔离测试 |
| M-06 | 共享 HTML 模板 + 31 个页面差异插槽，生成 demo/docs，接入 pytest 与 CI 同步检查 |
| M-07 | 删除英文旧偏好标题分类与消融兼容，旧标题归入 `other`，不删除存量内容 |
| M-08 | 删除 `_VIEWER_HTML` 简易控制台；未构建时仅返回 503 构建提示，API 独立使用 |

## 先复现，再修改

新增边界回归并调整两项明确取消的功能预期，在原实现得到 **5 failed, 5 passed, 3 skipped**。
失败覆盖：真实 Windows junction 导致外部目标文件被删、扫描错误被替换为目录非空错误、
英文旧标题仍被专用分类/消融接受、未构建时仍返回可操作的简易页面。
输出见 [修复前日志](maintenance_cleanup_red.txt)。测试只使用 `.pytest_cache` 下的合成目录。

其中 junction 由 Windows `_winapi.CreateJunction` 创建，目标与链接均位于测试工作区；
原实现删除了目标的 `keep.txt`，标准库实现保留该文件。普通文件、目录、悬空符号链接的
创建在本机需要额外 Windows 权限，这三项明确跳过，没有伪称已覆盖。

## 验证命令与结果

在仓库根目录：

```powershell
New-Item -ItemType Directory -Force .pytest_cache/maintenance-runtime | Out-Null
$env:TEMP = (Resolve-Path .pytest_cache/maintenance-runtime).Path
$env:TMP = $env:TEMP
$env:PYTHONIOENCODING='utf-8'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_forget_links.py tests/test_forget_concurrency.py tests/test_forget_lock_cleanup.py tests/test_service_paths.py tests/test_flock.py tests/test_prompt_language.py tests/test_eval_ablation.py tests/test_facility.py tests/test_server.py tests/test_lean.py tests/test_product_fixes.py tests/test_landing_pages.py tests/test_release.py -v --basetemp .pytest_cache/maintenance-final -o addopts='' -o faulthandler_timeout=60 --durations=8 --tb=short
python scripts/check_zero_setup.py
python scripts/build_landing_pages.py --check
python scripts/check_release.py
```

- **302 passed, 3 skipped**。包含实际 junction、安全扫描失败、同实例/跨实例/跨进程清空互斥、
  旧目录和空间路径隔离、完整陈述编辑、当前/旧标题分类、facility 日志统计、构建/未构建控制台
  路由与独立 API、落地页差异/本地资源/未同步检测等。
- zero-setup、落地页同步与商业发布静态检查通过，零依赖核心导入继续有效。
- 两项 asyncio unknown-option 警告来自禁用第三方 pytest 插件，不影响上述同步回归。

在 `frontend` 目录：

```powershell
node --test --test-reporter=tap tests/demoAuthMigration.test.cjs tests/apiKeyValidation.test.cjs tests/queryIsolation.test.cjs
```

**18 passed**，复核 M-05 的 key 内存态、旧缓存清理、刷新和账号/查询隔离。

原始输出：[定向 pytest](maintenance_cleanup_pytest.txt)、[前端认证](maintenance_cleanup_auth_tests.txt)、
[zero-setup](maintenance_cleanup_zero_setup.txt)、[发布检查](maintenance_cleanup_release.txt)。
日志为 UTF-8，个人用户目录前缀替换为 `<USER_HOME>`。HTTP/本地服务测试在获准的沙箱外运行，
未读取真实凭证或个人会话数据。

## 落地页迁移核对

共享模板位于 `landing/index.html.in`，配置位于 `landing/pages.json`。生成时逐页去掉新增的
生成提示注释，与修改前全文比较完全相同，包括 CSS、脚本、资源路径和既有 benchmark 内容。
迁移前及去注释后的 SHA-256 一致：

| 页面 | SHA-256 |
| --- | --- |
| demo | `b35d65430fc49b3c6d55cc1089e4cb7c308d76be3182eccc58931790c7b968a4` |
| docs | `4370633698bde6204209b7f2e996344f7a0eb4bdf3f17aa11ae883715e444100` |

后续编辑模板/配置，运行生成器并提交两份输出；`--check` 不改写文件，漂移即失败。
没有改动公开 benchmark 数字、历史 system ID 或原始结果日志，没有新模型评测。

## 兼容边界

英文旧偏好块仍作为文本存在，但不再获得 `preference` 类型的专用预算；自定义旧上下文
需要改成当前标题。内联简易编辑页已移除，未构建 React 时必须使用 API 或先构建前端。
这两项是明确授权的功能取消，不作为无行为变化的清理。

本机 Windows / Python 3.12；没有原生 Linux 实测或对恶意并发替换的完整安全证明。
未运行全仓 pytest、重新构建未修改的 React 前端或进行真实浏览器视觉回归；落地页内容逐字
等价核对及已有构建产物的 HTTP 路由回归不能替代完整视觉测试。CSP 仍有 `unsafe-inline`，
主题初始化脚本的进一步处理属于原先保留的独立加固事项。
