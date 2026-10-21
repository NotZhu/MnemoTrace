"""通过别名上的 union-find 解析规范身份。

当前只合并别名映射，不迁移已有记忆的 user_id。应在首次写入前关联句柄；写入后合并可能
导致非规范身份下的历史记忆无法检索。详见 docs/known-issues.zh-CN.md 的 ISSUE-039。
"""
from __future__ import annotations


class IdentityResolver:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def _find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # 路径压缩
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def resolve(self, user_id: str) -> str:
        """返回句柄对应的规范 id（在建立关联之前就是句柄本身）。"""
        return self._find(user_id)

    def link(self, a: str, b: str) -> str:
        """声明两个句柄属于同一个人；返回规范 id。"""
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            # 固定取字典序更小的根作为规范 id，保证确定性
            root, child = sorted((ra, rb))
            self._parent[child] = root
        return self._find(a)
