# 两个落地页的共享来源

`index.html.in` 保存共同 HTML、CSS 和脚本；`pages.json` 保存 demo / docs 两个页面各自的
标题、文案、导航样式等差异。`<!-- landing:名称 -->` 是整段替换位置，配置值是原样输出的
可信源码片段，不能用于用户输入。所有插槽必须完整配置，生成时拒绝缺失或多余字段。

修改模板或差异配置后，在仓库根目录执行：

```powershell
python scripts/build_landing_pages.py
python scripts/build_landing_pages.py --check
```

提交模板、配置及生成的 `demo/index.html` / `docs/index.html`。不要直接编辑生成文件。
CI 与 pytest 会检查生成文件是否最新；`--check` 发现不同会失败，不改写文件。

两处输出保留各自原有内容和相对资源路径。benchmark 数字、模型、命令及原始日志链接的
调整仍需遵守 CONTRIBUTING.md，同步检查两份 README 和 RESULTS.md，不因使用模板而省略。
