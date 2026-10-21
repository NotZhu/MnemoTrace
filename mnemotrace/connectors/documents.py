"""文档导入（AGENTS.md 「代码导航」 采纳层）—— 把上传的文件转成 忆痕 会话。

文档（PDF/DOCX/文本）被压平为文本，通过与聊天历史相同的 `Memory.import_messages` 路径导入；图片由服务层
（持有视觉模型）生成描述，导入的是描述文本。本模块是纯标准库实现，不做描述生成也不做嵌入：只识别文件
类型并提取文本。PDF/DOCX 提取需要可选依赖（`mnemotrace-memory[documents]`），缺失时抛出清晰、可执行的
ImportError —— 核心/离线路径绝不导入它们。
"""
from __future__ import annotations

import os
from typing import Optional

from .base import ImportMessage, ImportSession

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _as_bytes(data) -> bytes:
    return bytes(data) if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")


def detect_kind(data, filename: Optional[str] = None, content_type: Optional[str] = None) -> str:
    """返回 'image' | 'pdf' | 'docx' | 'text' 之一。依次依据 content-type、文件扩展名、magic bytes
    判断 —— 因此无论调用方知道 MIME 类型还是只有原始字节都能工作。"""
    ct = (content_type or "").lower()
    ext = os.path.splitext(filename or "")[1].lower()
    head = _as_bytes(data)[:8] if data is not None else b""
    if ct.startswith("image/") or ext in _IMAGE_EXT or head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff":
        return "image"
    if ct == "application/pdf" or ext == ".pdf" or head[:4] == b"%PDF":
        return "pdf"
    if ext == ".docx" or "officedocument.wordprocessing" in ct:
        return "docx"
    return "text"


def extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # 可选依赖 —— 不进入零配置核心
        raise ImportError(
            "PDF import needs pypdf — install with `pip install \"mnemotrace-memory[documents]\"` (or `pip install pypdf`)."
        ) from e
    import io

    reader = PdfReader(io.BytesIO(_as_bytes(data)))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def extract_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as e:
        raise ImportError(
            "DOCX import needs python-docx — install with `pip install \"mnemotrace-memory[documents]\"`."
        ) from e
    import io

    doc = docx.Document(io.BytesIO(_as_bytes(data)))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def document_text(data, kind: str) -> str:
    """从非图片文档提取纯文本（PDF/DOCX 缺少可选依赖时抛 ImportError）。"""
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "docx":
        return extract_docx(data)
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace").strip()
    return str(data).strip()


def to_data_url(data, content_type: Optional[str] = None) -> str:
    """把原始图片字节编码成视觉描述模型接受的 data: URL。"""
    import base64

    ct = content_type or "image/png"
    return f"data:{ct};base64,{base64.b64encode(_as_bytes(data)).decode('ascii')}"


def to_session(text: str, *, filename: Optional[str] = None, session_id: str = "document",
               metadata: Optional[dict] = None) -> ImportSession:
    """把提取出的文本（或图片描述）包装成单条消息的 ImportSession，可直接交给 import_messages。"""
    meta = {"source": "document", **(metadata or {})}
    msg = ImportMessage(content=text, speaker="document", metadata=meta)
    return ImportSession(session_id=session_id, messages=[msg], title=filename or "", metadata=meta)
