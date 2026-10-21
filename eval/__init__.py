"""MnemoTrace 的可复现评测 harness（AGENTS.md 「评测、公开内容与贡献规则」，Bet D）。

M0 自带一个内置的小型合成数据集，使 `python eval/harness.py` 无需下载、无需 LLM 就能产出
真实的分类别数字——它的意义在于锁定 harness 的*形态*（分类别准确率 + tokens + 延迟、多系统、
完全可复现）。M1 再把 LOCOMO / LongMemEval 数据集和 LLM judge 接进同一个 harness。
"""
