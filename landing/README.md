# 静态网页来源与发布

`index.html.in` 保存共用 `HTML/CSS`，`pages.json` 保存 `demo/docs` 的标题、文案和导航差异。插槽只接受仓库内可信源码片段，不接收用户输入；缺失、多余或非字符串值明确失败。

```bash
python scripts/build_landing_pages.py
python scripts/build_landing_pages.py --check
```

两份 HTML 继续提交到 `demo/index.html` 和 `docs/index.html`，保持现有发布路径。共同内容修改模板，差异修改配置；--check 不写文件。

共享资源以 `demo/assets` 的 brain-`circuit.svg`、`github.svg` 和 fonts 下的 `OFL.txt`、`README.txt`、两个 woff2 为来源。生成器同步 `docs/assets` 对应副本并检查字节一致。两份 memory-`graph.js` 保留页面变体，不合并。字体和第三方许可证随资源保留。

修改后提交来源和生成输出，检查两页链接、资源和有意差异。公开成绩要求见[项目规则](../AGENTS.md#principle-i)，对应数据只由 [RESULTS](../RESULTS.md)及原始日志解释。
