# 服务、隔离与接入边界

[MemoryService](../../mnemotrace/service.py) 是 HTTP、MCP、控制台和批量导入共用的服务层，负责按授权空间选取 `Memory`、懒加载、热缓存、事务锁和持久化。裸 `Memory` 只提供核心能力，不具备同样的服务并发事务保证。

## 请求与身份

```text
客户端 → Bearer 校验 / Principal → 授权空间
      → 安全摘要目录 → 服务读写作用域 → Memory → 保存或返回副本
```

静态 `key` 通过 `MNEMOTRACE_API_KEYS=tenant:key` 映射，同租户可有多个 `key`，同 `key` 不可映射多个租户。space-aware `remember`/`recall` 额外解析 `home/read/write` 权限；其余管理路由沿 home 路径。见 [HTTP 契约](../reference/http-api.md)。

`Key` 必须是非空可打印 ASCII，合法值原样使用；中文租户名不受此规则限制。非法 Bearer 返回 401，非法服务端凭据配置返回 503 且不回显内容。仅显式开启 `OPEN` 才进入开放模式，匿名还需单独允许。

多空间配置中的无效 JSON/非对象当前仍回退空配置；不能把现有字符验证描述为全部配置错误都严格拒绝。`test`、1 是普通空间，`anonymous` 与 `mnemotrace-test` 受整空间擦除保护。

请求体默认上限 2 MiB，同时核对声明长度和实际流式字节，字段另有限制。`/health` 是存活与诊断，`/ready` 才表示鉴权、目录等就绪条件；线上部署核验与本地测试范围分开。

## 目录与擦除

逻辑 `user`/namespace 经可读前缀和 SHA-256 映射到数据根目录的直接子项。真实路径和最终目录名必须匹配，拒绝同级链接以及 Windows 大小写/尾点别名。读、写、`forget` 只使用该摘要目录，不认领同名旧目录。

`forget` 在稳定目录外服务锁和线程锁内清缓存、重新校验目标、删除数据目录，完成后再释放。锁控制文件保留，等待中的写入随后重新加载并按需创建存储。rmtree 不遍历链接/junction 目标，扫描失败向上传播。

旧目录不会被自动读取、迁移、合并或删除。管理员须停掉共享数据根的进程并备份，确认 namespace 归属后显式复制普通 JSONL 库；pickle 转换和编号关联已退役。操作见[部署说明](../../deploy/README.md)。

## 并发与提交

同一稳定锁路径的服务实例共用进程内可重入线程锁，再按需要取得进程文件锁；所有权按线程管理，最外层作用域负责句柄。缓存元数据锁不覆盖路径解析、manifest 读取和等待。

| 作用域 | 当前保证 |
| --- | --- |
| 写 | 文件锁内重查提交、加载、修改及保存；对象绑定本次操作与基准提交；过期对象拒绝回写，失败不保留脏热缓存 |
| 内存索引热读 | 线程锁保护检索、强化、冷热移动、TTL 清扫及响应；manifest 未变化时可不取文件锁 |
| 冷读或提交变化 | 取文件锁后二次检查，完整加载同代集合再发布缓存 |
| LanceDB 读 | 保守持有文件锁，不套用内存热读规则 |

热读可以返回操作开始时看到的上一份已提交缓存，不保证跨进程写入期间严格实时。嵌套读取固定同一个 `Memory`；响应与可变缓存引用分离，跨空间召回逐空间完成，不同时持有多个用户锁。

读取中的 planner/分解模型调用保留线程锁但不持文件锁；answerer 在退出读取作用域后使用已组装字符串。同步 `remember`/`consolidate` 的模型调用仍在写锁内。直接 `open`/`save` 的并发读改存和保存中途崩溃边界需独立评估。

POSIX flock 与 Windows msvcrt 的竞争、EINTR 和真实错误分别处理；真实故障不能当成“锁竞争”吞掉。共享目录的全部进程需统一升级后重启。验证见 [事务回归](../../tests/test_service_transactions.py)、[工程证据](../../results/engineering/README.md#service-transactions)。

## 凭据安装与诊断

setup、MCP 和 doctor 共用 [credentials.py](../../mnemotrace/credentials.py)。显式 `key`、凭据文件和隐藏输入互斥；已识别文件错误不会回退到环境中的另一身份。本地 namespace 模式不需要远程凭据。

路径在调用时展开用户目录并转为绝对路径，不消解链接以绕过权限检查。新凭据放在用户目录 `.mnemotrace/credentials/<UUID>.key`：POSIX 目录/文件为 0700/0600；Windows 显式设置当前用户 owner 及当前用户/SYSTEM 私有 DACL。权限设置失败在写入前终止，既有不安全引用只读拒绝。

临时文件先施加权限再写入、fsync 和原子替换。配置只引用凭据路径；备份同样私有，卸载不删除凭据，dry-run 不写配置或凭据。它是权限保护的明文，不承诺加密或自动清扫旧终端历史、备份和服务端密钥。

[agent_config.py](../../mnemotrace/agent_config.py) 共用 Codex 配置读取：3.11+ 使用 tomllib；3.10 的标准库读取器支持常见 `command/args` 字符串形式，不覆盖通用 TOML。不支持表达形式只跳过可选复用；doctor 明确诊断失败，已识别引用的安全错误仍阻止安装。

配置按来源分别使用 `JSON/TOML/shell` 编码，TOML 保留合法 Unicode 并转义控制字符，孤立代理项明确失败。诊断只脱敏路径、地址、自由详情等内容，保留固定字段、状态与汇总控制信息；不能对最终配置或整个报告做无差别字符串替换。

相关边界见 [凭据测试](../../tests/test_credentials.py)、[配置测试](../../tests/test_credential_config.py)及[安全证据](../../results/engineering/README.md#agent-credentials)。

## 托管与发布

FastAPI 提供 `/v1/*`、`/health`、`/ready` 和 `/ui/`。控制台未构建时页面返回 503，API 与离线闭环独立可用。静态文件和首页回退共用真实路径包含性与 `StaticFiles` 校验；非法路径、目录外链接和缺失资源不能借 SPA 回退读取其他文件。

服务设置 no-store、nosniff、DENY、no-referrer 与 CSP；脚本仅同源，主题使用同步外置脚本，图谱渲染不放宽动态代码许可。容器以非 root、只读根、持久卷和本机绑定运行，网关与互联网边界见[部署说明](../../deploy/README.md)。

构建和版本验收属于工程发布证据，不代表算法准确率或线上部署完成。
