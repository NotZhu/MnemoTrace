# MnemoTrace 新后端

`mnemotrace-next` 是为重构准备的独立 Python 项目，临时导入名为 `mnemotrace_next`。

本地与 CI 都用 `.python-version` 选择 Python。uv 会为后端创建虚拟环境，并按 `uv.lock` 安装项目及开发依赖。默认 `dev` 组包含运行推理测试所需的 `inference` extra。先安装 uv，再从仓库根目录运行下面的命令。 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)

```sh
cd backend
uv run --locked python scripts/check.py
```

同一条命令在 CI 中依次执行 Ruff lint、Ruff 格式检查、mypy 和 pytest。可以用 `uv run --locked ruff format .` 应用格式化。

运行 `uv run --locked python -m mnemotrace_next` 进行冒烟检查，预期输出 `ok`。

PostgreSQL 的迁移、角色配置、表结构及真库验证见 [PostgreSQL 存储适配器](docs/postgres.md)。

补全和嵌入的端点配置、向量身份及真实端点验收见 [推理适配器](docs/inference.md)。

发行包随附仓库的[开源许可](LICENSE)和[商业授权条款](COMMERCIAL-LICENSE.md)。
