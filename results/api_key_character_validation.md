# 服务端 key 字符校验验收

日期：2026-10-18。对应审查项 R-02。

## 改动与根因

`hmac.compare_digest(str, str)` 不支持非 ASCII 字符串；原单空间配置接受中文 key，鉴权
遍历到它时抛出 `TypeError`，即使先前匹配了正常 key。多空间配置存在同类问题，且匹配成功
会提前返回，导致后续非法 key 被掩盖。

- `spaces.py` 提供共享的 key 校验与 `AuthConfigError`；`server.app` 保留导入后的同名异常入口。
- 单空间和多空间配置要求 key 非空、可打印 ASCII；多空间在匹配前校验整个配置。
- Bearer 请求在原有解析前拒绝非 ASCII / 控制字符，返回 401。配置错误返回通用 503，
  `/health` 保持 200 并标记 `auth_mode=invalid` / `ready=false`，`/ready` 返回 503。
- 错误不回显密钥；配置校验异常只指明配置变量、条目序号与字符要求。
- 中文租户/空间名称、合法 key 轮换、单空间逐项比较、ACL、普通空间 `1` 和显式匿名开关
  保持有效。不编码 key，不迁移配置，不修改记忆存储。

## 复现与验证

先添加 `tests/test_api_key_validation.py`，旧实现为 **28 failed, 18 passed**。回归捕获中文
配置的 `TypeError`、非法开放模式凭证被接受，以及坏配置仍显示就绪。
原输出见 [修复前回归](api_key_validation_red.txt)。环境变量不能包含 NUL，因此 NUL 配置
使用可编码该字符的 JSON 多空间配置覆盖；请求直接调用同样覆盖 NUL。

新增 HTTP 回归通过原始 Latin-1 header 字节测试实际 ASGI 边界，验证非法请求返回 401、
正常请求继续成功，以及两类坏配置返回 503 且响应不含 key。完整命令在仓库根目录执行：

```powershell
New-Item -ItemType Directory -Force .pytest_cache/key-validation-runtime | Out-Null
$env:TEMP = (Resolve-Path .pytest_cache/key-validation-runtime).Path
$env:TMP = $env:TEMP
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_api_key_validation.py tests/test_server.py tests/test_spaces.py -q --basetemp .pytest_cache/key-validation-http-final -o addopts='' --tb=short
python scripts/check_zero_setup.py
python scripts/check_release.py
```

- **103 passed**，包含 46 项新直接调用回归、5 项新 HTTP 参数化回归及现有服务/空间回归。
- zero-setup 和商业发布静态检查均通过。
- 输出：[pytest](api_key_validation_pytest.txt)、[zero-setup](api_key_validation_zero_setup.txt)、
  [发布检查](api_key_validation_release.txt)。日志为 UTF-8，个人用户目录前缀替换为 `<USER_HOME>`。

## 验证边界

本机 Windows / Python 3.12。沙箱内 asyncio 事件循环无法完成，HTTP 测试经批准在沙箱外
使用 FastAPI TestClient 执行，全部配置和 key 均为合成值；未调用外部模型或读取真实凭证。
禁用第三方 pytest 插件后有两条 asyncio 配置 unknown-option 警告。

未运行完整 pytest、原生 Linux 或真实模型 benchmark；未改前端。保留原有配置首尾空白处理
和无效多空间 JSON 的忽略语义；本次校验针对凭证字符，不扩大为配置解析器的全面重写。
非法配置需要运营者修正；503 提供明确失败状态，不会自动替换 key，含非法 key 的配置也不会
只取其中合法项继续鉴权。
