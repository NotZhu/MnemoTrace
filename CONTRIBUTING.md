# 为 MnemoTrace 做贡献

MnemoTrace 的目标是成为一个一流的、**完全可复现**的开源记忆引擎。使其成真的有两件事：代码，以及我们谈论它时的严谨。以下简短规则对在本仓库工作的**人类贡献者**和 **AI 编码代理**同样适用——请遵守。

## 开发基础

- **零配置不变量。** `python examples/quickstart.py`、已安装的 `mnemotrace-quickstart` 以及 `pytest` 必须在**无 API key、无外部服务**的情况下通过（哈希 embedder + 规则 extractor + 内存 stores）。绝不可破坏这一点。
- **零依赖冒烟检查。** 在发布 benchmark 或声称仓库可从干净检出运行时，先运行 `python scripts/check_zero_setup.py`。它会在不安装可选包的情况下，执行 quickstart、离线 harness、已提交的 evidence-log 校验、论文统计与 stdlib 编译。
- **测试。** 在已安装测试依赖时，推送前运行 `pytest`；为新行为添加测试。保持离线回退的确定性。
- **接口优先。** 每个外部依赖（LLM、embedder、store）都应位于一个带有零依赖离线回退的接口之后。不要在核心模块的顶层导入重型依赖。
- 关于架构与约定，请参阅 [`CLAUDE.md`](CLAUDE.md)。

## 数字与 benchmark 声明（不可妥协）

**一个我们无法复现的数字不存在。**

- 每个已发布的数字都必须可追溯到一份**已提交的原始日志**（`results/*.jsonl`），并可用 `python eval/report.py <file>` 重新推导。如果你无法指向该日志，就不要发布这个数字。
- 在将某次运行标记为 DONE 或将其用作论文/README 证据之前，先用 `python eval/validate_results.py --expected-rows <full item count> --require-complete <file>` 校验（对于 LongMemEval_S 全集，`<full item count>` 为 `500`）。如果某个多系统日志包含一个未被引用的探索性或显式出错的系统，请用可重复的 `--system <name>` 标志来校验被引用的系统，而不要假装整个日志都是干净的。
- 报告**三项**：accuracy **+** tokens **+** latency——绝不仅仅是 accuracy。始终包含来自*同一次运行*的**全上下文基线**（same answerer + judge），如果我们没有超越它，要直白说明。
- **禁止挑选样本。** 仅使用全集数字——小样本在这里屡屡过于乐观。使用官方 judge 提示；任何偏差都要记录。
- 如果你修改了头条数字，请**在同一处变更中**更新它出现的**每一个**地方，以保持一致：`README.md`、`README.zh-CN.md`、`RESULTS.md`，以及落地页（`demo/index.html` + `docs/index.html`）。

## 对外措辞与定位

MnemoTrace 自成一体。要自信，但要诚实。

- **不要在对外的文案中点名或做 benchmark 对比竞争对手**，也不要把功能包装成"借鉴自 <大公司>"。描述 *MnemoTrace* 自身做了什么，凭自身的价值。
- **不做无 benchmark 支撑的声明。** 不要写"scales to 1M tokens"、"SOTA"、"#1"、"cost stays flat as history grows"或类似表述，除非有已提交的日志支撑。优先采用已被证明的特性（例如"从检索到的切片作答，而非重放完整历史"），而非愿景式的扩展声明。
- **不要有过时状态。** 不要在已完成的事项上留下"in progress / 验证中 / coming soon"（反之亦然）。保持 **English 与 中文** 文案同步（README ×2、落地页）。

两个落地页由 `landing/index.html.in` 与 `landing/pages.json` 生成。修改共享内容或页面差异后运行
`python scripts/build_landing_pages.py`，提交源文件和两份输出；用 `--check` 检查是否同步。
生成器保留 demo / docs 的有意差异，不替代上面的 benchmark 证据与多处一致性要求。

## 提交

- **提交信息中不要包含 AI 署名。** 不要添加 `Co-Authored-By: Claude …` 或 `Generated with …` 这类 trailer。写一条平实、具描述性的信息。（如果某分支带有此类 trailer，在合入 `main` 之前将其去除。）

## 隐私

- **绝不要提交真实的个人数据、姓名或密钥。** 在示例中使用合成的占位符（例如 `李雷`、`user123`、`my-app`）。保持 `.env` 被 gitignore。如果你在提交历史中发现泄露的 PII 或凭据，请立即标记——真正的修复通常意味着重写历史，而不仅仅是一条新提交。
