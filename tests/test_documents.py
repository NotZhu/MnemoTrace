"""多模态导入测试（CLAUDE.md §6）：文档文本抽取 + 图片生成说明，全部离线。

文档展平为文本，图片变成它的说明文字——两者都走普通的导入管道，所以真正的断言是"它已成为可检索
的记忆"。说明生成用一个 stub captioner（一个只暴露 `.caption` 的小对象）驱动，不需要真实的视觉
模型；无 captioner 的路径必须降级为占位符而不是崩溃（零配置不变量）。"""
from __future__ import annotations

import importlib.util
import shutil
import tempfile

import pytest

from mnemotrace.connectors import documents as D
from mnemotrace.llm import vision
from mnemotrace.server import openai_compat as oc
from mnemotrace.service import MemoryService

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 魔数检测所需的最低限度文件头


class _StubCaptioner:
    """替代视觉模型：任何带 `.caption(ref, prompt)` 方法的对象都算 captioner。"""
    def caption(self, image_ref, prompt=None):  # noqa: ARG002
        return "a red bicycle leaning on a wall"


@pytest.fixture()
def svc():
    d = tempfile.mkdtemp(prefix="mnemotrace_doc_")
    try:
        yield MemoryService(data_dir=d, embedder_name="hashing", llm_name="")  # captioner=None，离线
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- 纯辅助函数（不涉及 service） ----------------------------------------------


def test_detect_kind():
    assert D.detect_kind(b"%PDF-1.7 ...", "a.pdf", "application/pdf") == "pdf"
    assert D.detect_kind(_PNG, "a.png", "image/png") == "image"
    assert D.detect_kind(b"\xff\xd8\xff\xe0", None, None) == "image"  # JPEG 魔数，无其他提示
    assert D.detect_kind("plain notes", "n.txt", "text/plain") == "text"
    assert D.detect_kind(b"PK\x03\x04", "a.docx", None) == "docx"


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


def test_text_document_is_ingested_and_searchable(svc):
    res = svc.import_document("u", "Apollo program notes. Lead engineer: Wei Zhang. Launched 1969.",
                              filename="notes.txt", content_type="text/plain")
    assert res["ok"] is True and res["kind"] == "text"
    mem = svc.memories("u", include_sensitive=True)
    assert mem["counts"]["episodes"] >= 1
    assert any("Apollo" in ep["content"] for ep in mem["episodes"])
    # 而且可以作为 lean context 检索到
    ctx = svc.recall("u", "Apollo program", lean=True)["context"]
    assert "Apollo" in ctx


def test_image_is_captioned_into_memory(svc):
    svc.captioner = _StubCaptioner()  # 假装配置了视觉模型
    res = svc.import_document("u", _PNG, filename="bike.png", content_type="image/png")
    assert res["ok"] is True and res["kind"] == "image"
    mem = svc.memories("u", include_sensitive=True)
    assert any("red bicycle" in ep["content"] for ep in mem["episodes"])


def test_image_without_captioner_stores_placeholder(svc):
    # captioner 为 None（离线）-> 存入占位符，图片依然留下痕迹，且不崩溃
    res = svc.import_document("u", _PNG, filename="x.png", content_type="image/png")
    assert res["ok"] is True and res["kind"] == "image"
    mem = svc.memories("u", include_sensitive=True)
    assert any("[image]" in ep["content"] for ep in mem["episodes"])


def test_pdf_without_pypdf_returns_actionable_error(svc):
    if importlib.util.find_spec("pypdf") is not None:
        pytest.skip("pypdf installed — the missing-dep error path isn't exercised")
    res = svc.import_document("u", b"%PDF-1.4 not-really", filename="a.pdf", content_type="application/pdf")
    assert res["ok"] is False and "pypdf" in res["error"]


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
