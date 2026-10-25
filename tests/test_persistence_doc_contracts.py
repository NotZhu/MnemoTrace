"""Durable storage's documented failure boundaries must stay explicit."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_durable_persistence_documents_both_crash_edges():
    contract = (ROOT / "docs/architecture/memory.md").read_text(encoding="utf-8")
    assert "manifest 声明的已提交前缀" in contract
    assert "前缀内部缺失记录或 JSON 格式错误时抛出 `StoreFormatError`" in contract
    assert "manifest 数量之后的撕裂字节作为未提交尾部丢弃" in contract


def test_durable_persistence_documents_model_identity_separately_from_dimension():
    contract = (ROOT / "docs/architecture/memory.md").read_text(encoding="utf-8")
    assert "嵌入维度不符抛出 `DimensionMismatchError`" in contract
    assert "即使维度相同，模型 id 不同也抛出 `EmbedderMismatchError`" in contract
    assert "错误向量空间的近邻" in contract
