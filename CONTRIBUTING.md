# 为 MnemoTrace 做贡献

本页说明开发环境与验证入口。共用开发规则维护在 [AGENTS.md](AGENTS.md)，使用方式见[文档导航](docs/README.md)。

## 开发环境

Python 3.10 或更高版本。在仓库根目录创建独立环境：

```sh
python -m venv .venv
```

Bash 使用 `source .venv/bin/activate`，PowerShell 使用 `.venv\Scripts\Activate.ps1`。随后安装当前开发和接口依赖：

```sh
python -m pip install -e ".[dev,server,mcp]" build twine
```

核心库没有第三方运行时依赖，开发依赖和可选后端不应进入核心的硬依赖。零配置示例、已安装命令与离线测试的权威契约见 [AGENTS.md](AGENTS.md#principle-ii)。

## 定位变更


| 工作           | 入口                                                                               |
| ---------------- | ------------------------------------------------------------------------------------ |
| 记忆与存储     | [架构总览](docs/architecture/README.md)、[持久化契约](docs/architecture/memory.md) |
| HTTP / MCP     | [协议参考](docs/reference/http-api.md)、[工具参考](docs/reference/mcp.md)          |
| 控制台与 SDK   | 下方开发步骤和对应接口测试                                                         |
| 算法与性能     | [评测说明](eval/README.md)                                                         |
| 文档           | [导航](docs/README.md)及对应权威页面                                               |
| 两个静态展示页 | [来源说明](landing/README.md)                                                      |

先检查相关实现、调用方和已有测试。新行为与缺陷需要能区分正确和错误结果的回归；文案和排版修改按影响选择静态检查。重大取舍按 AGENTS 的条件保留设计说明，普通修复直接实施。

## Python 验证

在根目录运行目标测试，再按影响扩大范围：

```sh
python -m pytest tests/test_conflict.py
python -m pytest
```

测试依赖已安装后，默认套件应不需要 API key 或外部服务。测试入口会隔离模型相关环境，并禁止通常的本地 dotenv 自动加载；只有专门验证 dotenv 的 fixture 才显式恢复该行为。可选后端未安装时，相关测试可能跳过。

运行安装与默认路径检查：

```sh
python scripts/check_zero_setup.py
```

使用合成数据或独立临时目录。不要把现有个人记忆库、凭据或生产数据作为自动测试 fixture。

## 控制台开发

CI 使用 Node.js 22 和前端 package.json 声明的 pnpm 版本。先启动本地后端，再在 `frontend/` 执行：

```sh
pnpm install --frozen-lockfile
pnpm dev
```

开发服务默认端口 5173，`/v1` 和 `/health` 代理到 localhost:8000，具体 base 与路径见 [vite.config.ts](frontend/vite.config.ts)。生产模式构建后由 Python 服务的 `/ui/` 提供资源。

```sh
pnpm test
pnpm run build
```

build 已包含 TypeScript 检查。前端测试覆盖状态、查询与关键交互；涉及真实用户交互变化时，再按范围运行实际页面验证。UI 操作说明见[使用指南](docs/guides/usage.md)。

## TypeScript SDK 开发

在 `clients/typescript/` 执行：

```sh
npm ci
npm run typecheck
npm test
```

test 通过 pretest 先构建。发布包的使用和 API 参考维护在 [SDK README](clients/typescript/README.md)，公共导出来自 src/index.ts，运行测试位于该目录的 test/。

## 文档与生成内容

当前使用仓库 Markdown。docs/README.md 是完整导航；新增独立页面需有明确读者和职责，并接入导航。示例优先复用已有可运行入口，参考页描述契约与限制，架构页解释机制和理由。

```sh
python scripts/check_repository.py
python scripts/build_landing_pages.py --check
git diff --check
```

移动或退出页面时更新链接、锚点、检查器要求和必要的引用。迁移问题时保留仍需引用的依据与适用范围；生成页修改来源后再生成，不直接修改输出。

## 打包与交付

```sh
python scripts/check_release.py
python -m build
python -m twine check dist/*
```

发布检查核对发行元数据、文件与部署安全默认值；不能替代文档、算法或产品验收。完整 CI 范围见 [ci.yml](.github/workflows/ci.yml)。

提交或 PR 说明问题、最终行为、验证结果与限制。验证失败或中断时明确区分本次引入的问题与已有问题。普通测试结果在 PR/CI 交付；临时输出写系统临时目录或 .artifacts/ 下的任务目录，按 AGENTS 的生命周期处理。

缺陷和需求使用对应 Issue；漏洞按 [SECURITY.md](SECURITY.md)报告。许可证及商业条款见 [LICENSE](LICENSE) 和 [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)。
