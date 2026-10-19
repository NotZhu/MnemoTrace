# ISSUE-001：控制台静态文件目录边界验收

日期：2026-10-19。范围为 HTTP 控制台文件服务，不涉及记忆算法或公开 benchmark 数字。

## 原因与修复

原 `/ui/{path:path}` 处理器将解码后的请求路径直接拼入构建目录，只检查文件存在性便返回
FileResponse；编码父目录、Windows 路径及目录外链接可能让未鉴权请求下载公开目录外的文件。

修复前运行新增回归的首项，得到 `200 != 404`：

```text
python -m pytest tests/test_ui_static.py -x -q
FAILED test_console_rejects_invalid_paths[%2e%2e/outside.txt]
E assert 200 == 404
```

这是 HTTP TestClient 对临时合成文件的实际响应断言，不是仅检查处理器的返回路径。

修复将 assets 与其他 `/ui` 文件统一交给 StaticFiles，不再单独挂载 assets 或直接返回拼接路径：

- ASGI 完成 URL 解码后，拒绝父目录段、绝对路径、反斜杠、冒号（Windows 盘符/流）和空字节；不二次解码。
- realpath + commonpath 校验真实目录包含关系，让目录外链接直接返回 404，而不是回退到首页。
- StaticFiles 保持 `follow_symlink=False`，实际文件响应及首页回退均再次经过其内置目录检查。
- 合法且缺失的页面深路径返回 index.html；缺失 assets 返回 404。首页自身为外部链接时不能绕过检查。
- 构建状态按请求检查，未构建的 `/ui`、页面及 assets 都返回原有 503 构建提示。

## 验证

```powershell
python -m pytest tests/test_ui_static.py tests/test_server.py tests/test_release.py -o addopts= -q
python scripts/check_zero_setup.py
```

- 静态目录、HTTP 服务与发布回归：**81 passed，3 skipped**。
- 真实 Uvicorn 本机 HTTP socket：无需凭据，请求编码父目录、反斜杠路径及 HEAD 越界均返回 404；正常页面和 JavaScript 文件可完整传输，安全响应头有效。
- 编码路径、现存绝对路径、Windows 盘符/UNC/流、空字节、正常资源、许可文件、GET/HEAD、SPA 深路径、缓存/CSP、安全头和未构建提示均有回归。
- 真实 Windows junction 指向同级 `dist-private` 目录，验证不能用字符串前缀混淆 `dist` 的目录边界；链接目标的合成标记不可读取。
- 零配置脚本通过：source/module quickstart、离线 harness、既有 evidence-log 校验、论文统计及 stdlib 编译。

原始输出：[HTTP/发布回归](ui_static_boundary_pytest.txt)、[零配置检查](ui_static_boundary_zero_setup.txt)。
日志中本机 Python 安装路径已脱敏。

## 验证边界

- 环境为 Windows / Python 3.12。3 项文件符号链接回归因系统缺少创建权限跳过；支持文件符号链接的平台会执行这些测试。目录链接使用不需该权限的真实 junction，已执行。
- HTTP TestClient 在当前受限沙箱内挂起；上述测试改在沙箱外运行，使用的仍是临时合成文件和本机回环端口。
- 当前测试环境未安装 pytest-asyncio，pytest 提示两个既有 asyncio 配置项未知；Uvicorn 的 WebSocket 依赖另有两项弃用警告，均未导致测试失败。
- 本次未执行全量 pytest，未核验线上部署、代理转发行为或完成生产升级。运行中的服务需要更新代码并重启才能使用修复。
- 没有读取真实凭据或租户正文；没有推断历史上是否发生过数据泄露。
