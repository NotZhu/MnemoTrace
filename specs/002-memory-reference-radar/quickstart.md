# 快速上手：验证记忆系统参考雷达

本指南验证 `002-memory-reference-radar` 的规划产物。本功能属于文档与架构治理，
因此不运行 MnemoTrace 运行时或 benchmark 评测框架。

## 前置条件

- 已检出仓库。
- Shell 中可使用 `rg`、`curl` 和 `git`。

## 1. 确认当前功能

```bash
cat .specify/feature.json
```

预期结果：文件指向 `specs/002-memory-reference-radar`。

## 2. 检查必需产物

```bash
test -f specs/002-memory-reference-radar/spec.md
test -f specs/002-memory-reference-radar/plan.md
test -f specs/002-memory-reference-radar/research.md
test -f specs/002-memory-reference-radar/technical-report.zh-CN.md
test -f specs/002-memory-reference-radar/data-model.md
test -f specs/002-memory-reference-radar/quickstart.md
test -f specs/002-memory-reference-radar/contracts/radar-entry.md
test -f specs/002-memory-reference-radar/contracts/adoption-candidate.md
test -f specs/002-memory-reference-radar/checklists/requirements.md
```

预期结果：所有命令成功退出。

## 3. 检查未解决的占位符

```bash
rg -n "NEEDS CLARIFICATION|\\[FEATURE\\]|\\[DATE\\]|\\[###|ACTION REQUIRED|REMOVE IF UNUSED" \
  specs/002-memory-reference-radar/spec.md \
  specs/002-memory-reference-radar/plan.md \
  specs/002-memory-reference-radar/research.md \
  specs/002-memory-reference-radar/data-model.md \
  specs/002-memory-reference-radar/contracts
```

预期结果：没有匹配项。

## 4. 检查关键治理术语

```bash
rg -n "external-only|clean-room|accuracy/tokens/latency|raw evidence|supersedes|zero-setup" \
  specs/002-memory-reference-radar
```

预期结果：计划、研究、数据模型或契约中出现匹配项，表明雷达保留了 MnemoTrace 的宪章门禁。

## 5. 检查雷达中的公开链接

```bash
perl -ne 'print "$1\n" while /\\((https?:\\/\\/[^)]+)\\)/g' \
  specs/002-memory-reference-radar/research.md | sort -u | \
while read url; do
  code=$(curl -L -s -o /dev/null -w '%{http_code}' "$url")
  printf '%s %s\n' "$code" "$url"
done
```

预期结果：当前有效链接返回成功的 HTTP 状态码。若来源之后迁移或失效，
应更新条目，注明链接失效，并记录 `last_verified` 日期。

## 6. 审查是否可进入下一阶段

打开 [contracts/adoption-candidate.md](contracts/adoption-candidate.md)，确认 `research.md` 的
Initial Priority Queue（初始优先队列）中每个条目均能明确描述以下内容：

- MnemoTrace 自身的能力形式。
- 服务的战略下注。
- 受影响的模块或接口。
- Benchmark 目标。
- 准确率 / token 消耗 / 延迟假设。
- 独立实现（clean-room）说明。
- 回滚标准。

预期结果：首选候选可进入 `/speckit-tasks`；缺失这些字段的候选仍保持仅研究状态。
