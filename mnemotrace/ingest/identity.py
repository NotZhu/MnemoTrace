"""身份解析 —— 一等公民原语，而不是事后补丁。

稳定的 `user_id` 是所有竞品都在默默假设、又在多设备/匿名会话场景下纷纷打破的前提（这是业界
公开承认的缺口；见 CLAUDE.md §1.4）。我们把它建模为别名上的 union-find：两个句柄
（"u1"、"wei@example.com"）可以合并成一个规范身份，而无需重写任何历史。
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
