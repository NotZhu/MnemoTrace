"""图片描述（caption）辅助函数（CLAUDE.md §6 —— 多模态写入）。

忆痕 的检索基于文本，所以一张图片进入记忆的方式与 Mem0/Supermemory 相同：由视觉模型写出
一段密集的 caption，我们存储、嵌入、检索的都是这段文本。这些辅助函数只用标准库 —— 它们作用于
由服务层传入的 *captioner* 对象（任何暴露 `.caption(image_ref, prompt)` 的对象，如
`LiteLLMClient`），因此本模块从不导入模型后端；零配置路径（无 captioner）退化为占位符而不是报错。
"""
from __future__ import annotations

from typing import Any, Optional


def caption_image(captioner: Any, image_ref: str, prompt: Optional[str] = None) -> str:
    """为一张图片（data: URL 或 http(s) URL）生成 caption。当没有具备视觉能力的 captioner 或
    调用失败时返回 "" —— 调用方会替换成占位符，因此坏掉/缺失的模型绝不会中断写入。"""
    if captioner is None or not image_ref:
        return ""
    fn = getattr(captioner, "caption", None)
    if not callable(fn):  # 没有视觉路径的纯文本 LLM
        return ""
    try:
        return (fn(image_ref, prompt) or "").strip()
    except Exception:  # noqa: BLE001 -- 非视觉模型 / 瞬时错误 -> 占位符，绝不崩溃
        return ""


def image_refs(content: Any) -> list[str]:
    """从 OpenAI 多模态 `content` 数组中抽取图片引用 —— 是只取文本的 `connectors.extract_text`
    （会丢掉图片）的逆操作。每个引用都是可直接传给 caption_image 的 data:/http(s) URL。"""
    refs: list[str] = []
    if isinstance(content, (list, tuple)):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                u = part.get("image_url")
                if isinstance(u, dict):
                    u = u.get("url")
                if isinstance(u, str) and u:
                    refs.append(u)
    return refs


def caption_content(captioner: Any, content: Any, placeholder: str = "[image]") -> list[str]:
    """为多模态 content 数组里的每个图片部分生成 caption。每张图返回一行 —— 有 caption 时是
    caption 本身，否则是裸占位符，让图片至少留下一条可检索的来源痕迹。"""
    lines: list[str] = []
    for ref in image_refs(content):
        cap = caption_image(captioner, ref)
        lines.append(f"[image] {cap}" if cap else placeholder)
    return lines
