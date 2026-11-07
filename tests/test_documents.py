"""多模态导入测试（AGENTS.md 「代码导航」）：文档文本抽取 + 图片生成说明，全部离线。

文档展平为文本，图片变成它的说明文字——两者都走普通的导入管道，所以真正的断言是"它已成为可检索
的记忆"。说明生成用一个 stub captioner（一个只暴露 `.caption` 的小对象）驱动，不需要真实的视觉
模型；无 captioner 的路径必须降级为占位符而不是崩溃（零配置不变量）。"""
from __future__ import annotations

from io import BytesIO

import pytest



from mnemotrace.connectors import documents as D
from mnemotrace.llm import vision
from mnemotrace.server import openai_compat as oc

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 魔数检测所需的最低限度文件头


class _StubCaptioner:
    """替代视觉模型：任何带 `.caption(ref, prompt)` 方法的对象都算 captioner。"""
    def caption(self, image_ref, prompt=None):  # noqa: ARG002
        return "a red bicycle leaning on a wall"


# --- 纯辅助函数（不涉及 service） ----------------------------------------------


def test_detect_kind():
    assert D.detect_kind(b"%PDF-1.7 ...", "a.pdf", "application/pdf") == "pdf"
    assert D.detect_kind(_PNG, "a.png", "image/png") == "image"
    assert D.detect_kind(b"\xff\xd8\xff\xe0", None, None) == "image"  # JPEG 魔数，无其他提示
    assert D.detect_kind("plain notes", "n.txt", "text/plain") == "text"
    assert D.detect_kind(b"PK\x03\x04", "a.docx", None) == "docx"


def test_docx_parser_uses_optional_dependency():
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Synthetic project note")
    stream = BytesIO()
    document.save(stream)
    assert D.document_text(stream.getvalue(), "docx") == "Synthetic project note"


def test_vision_caption_image_guards():
    assert vision.caption_image(None, "x") == ""           # 没有 captioner
    assert vision.caption_image(object(), "x") == ""        # 不具备视觉能力（没有 .caption）
    assert vision.caption_image(_StubCaptioner(), "x") == "a red bicycle leaning on a wall"


def test_vision_image_refs_and_content():
    content = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "u1"}}]
    assert vision.image_refs(content) == ["u1"]
    assert vision.caption_content(_StubCaptioner(), content) == ["[image] a red bicycle leaning on a wall"]
    assert vision.caption_content(None, content) == ["[image]"]  # 无 captioner 时返回占位符


# --- 经由 service 的文档导入 ----------------------------------------------------


# --- OpenAI 兼容层：经代理发送的图片会被以说明文字的形式记住 --------------------


def test_remembered_text_plain_is_unchanged():
    msgs = [{"role": "user", "content": "hello world"}]
    assert oc.remembered_text(None, msgs) == "hello world" == oc.latest_user_text(msgs)


def test_remembered_text_appends_image_caption():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]}]
    out = oc.remembered_text(_StubCaptioner(), msgs)
    assert "what is this?" in out and "[image] a red bicycle leaning on a wall" in out


def test_remembered_text_image_only_without_captioner():
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/i.png"}}]}]
    assert oc.remembered_text(None, msgs) == "[image]"
